"""Modelo de microestrutura: LightGBM e regressao logistica de referencia, com calibracao isotonica.

Saida (por lado, compra/venda): P(alvo), P(stop), P(tempo) e E[R$ liquido]. Implementa o Protocolo
ModeloPreditivo. Parametros FIXOS (sem busca de hiperparametros na v1).

Honestidade de status: o modelo nasce `nao_treinado`, passa a `em_validacao` ao treinar e so vira
`validado_oos` por `avaliar_oos`, com criterio pre-registrado (hash), dados estritamente posteriores ao
treino, holdout usado uma unica vez e dados que NAO sejam sinteticos/contaminados.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from robowin.core.tipos import VetorFeatures
from robowin.features.registro import validar_nomes

from . import rotulagem as rot
from .calibracao import CalibradorIsotonico, ece, log_loss_multiclasse

LADOS = ("compra", "venda")
STATUS = ("nao_treinado", "em_validacao", "validado_oos", "reprovado")
ORIGENS_SEM_SELO = ("sintetico", "contaminado")
NS = 1_000_000_000

PARAMS_LIGHTGBM = {  # FIXOS: nenhuma busca na v1
    "objective": "multiclass", "n_estimators": 60, "learning_rate": 0.05, "num_leaves": 7,
    "min_child_samples": 150, "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8,
    "reg_lambda": 5.0, "n_jobs": 1, "verbose": -1, "deterministic": True, "force_row_wise": True,
}


class ErroModelo(RuntimeError):
    pass


class ModeloNaoTreinado(ErroModelo):
    pass


class CriterioNaoPreRegistrado(ErroModelo):
    pass


class CriterioAusente(CriterioNaoPreRegistrado):
    pass


class VazamentoOOS(ErroModelo):
    pass


class HoldoutJaUsado(ErroModelo):
    pass


# ---------------------------------------------------------------------------------------------
# Amostras
# ---------------------------------------------------------------------------------------------
@dataclass
class Amostras:
    t_ns: np.ndarray  # instante da decisao
    t_fim_ns: np.ndarray  # instante em que ambos os rotulos ficam conhecidos
    X: pd.DataFrame
    y_compra: np.ndarray
    y_venda: np.ndarray
    pnl_compra: np.ndarray
    pnl_venda: np.ndarray

    def __len__(self) -> int:
        return len(self.t_ns)

    def subconjunto(self, idx: np.ndarray) -> Amostras:
        return Amostras(self.t_ns[idx], self.t_fim_ns[idx], self.X.iloc[idx].reset_index(drop=True),
                        self.y_compra[idx], self.y_venda[idx], self.pnl_compra[idx], self.pnl_venda[idx])

    def y(self, lado: str) -> np.ndarray:
        return self.y_compra if lado == "compra" else self.y_venda

    def pnl(self, lado: str) -> np.ndarray:
        return self.pnl_compra if lado == "compra" else self.pnl_venda

    def hash_dados(self) -> str:
        h = hashlib.sha256()
        for a in (self.t_ns, self.t_fim_ns, self.y_compra, self.y_venda, self.pnl_compra, self.pnl_venda):
            h.update(np.ascontiguousarray(a).tobytes())
        h.update(np.ascontiguousarray(self.X.to_numpy(dtype=float)).tobytes())
        h.update(",".join(self.X.columns).encode())
        return h.hexdigest()


def construir_amostras(vetores: list[VetorFeatures], q: rot.Cotacoes, cfg: rot.ConfigBarreiras,
                       custos: rot.Custos, t_min_ns: int | None = None) -> Amostras:
    """Une vetores de features (um por instante de decisao) aos rotulos de tripla barreira."""
    instantes = [v.t_ns for v in vetores]
    idx, rc, rv = rot.rotular(q, instantes, cfg, custos)
    if t_min_ns is not None:
        keep = [k for k, i in enumerate(idx) if instantes[i] >= t_min_ns]
        idx, rc, rv = [idx[k] for k in keep], [rc[k] for k in keep], [rv[k] for k in keep]
    linhas = [vetores[i].valores for i in idx]
    X = pd.DataFrame(linhas).astype(float)
    return Amostras(
        t_ns=np.array([instantes[i] for i in idx], dtype=np.int64),
        t_fim_ns=np.array([max(a.t_fim_ns, b.t_fim_ns) for a, b in zip(rc, rv, strict=True)], dtype=np.int64),
        X=X.reset_index(drop=True),
        y_compra=np.array([r.classe for r in rc]), y_venda=np.array([r.classe for r in rv]),
        pnl_compra=np.array([r.pnl_liquido_brl for r in rc]), pnl_venda=np.array([r.pnl_liquido_brl for r in rv]),
    )


# ---------------------------------------------------------------------------------------------
# Criterio OOS pre-registrado
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CriterioOOS:
    """Criterio de promocao fixado ANTES de olhar o holdout; o hash identifica o pre-registro."""
    min_amostras: int = 500
    ganho_log_loss_min: float = 0.002  # log-loss do prior menos log-loss do modelo
    ece_alvo_max: float = 0.05
    min_selecionadas: int = 100
    limiar_e_brl: float = 0.0  # so opera quando E[R$ liq] prevista > limiar
    ic_inf_min_brl: float = 0.0  # limite inferior do IC95 da expectativa realizada > 0
    cluster_s: int = 600  # erros-padrao robustos a agrupamento temporal (amostras sobrepostas)

    def hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()

    def aprova(self, m: dict[str, float]) -> tuple[bool, list[str]]:
        motivos: list[str] = []
        if m.get("n", 0) < self.min_amostras:
            motivos.append("amostras insuficientes")
        if not m.get("ganho_log_loss", -1.0) >= self.ganho_log_loss_min:
            motivos.append("sem ganho de log-loss sobre o prior")
        if not m.get("ece_alvo", 1.0) <= self.ece_alvo_max:
            motivos.append("calibracao ruim")
        if m.get("n_selecionadas", 0) < self.min_selecionadas:
            motivos.append("poucas operacoes selecionadas")
        if not m.get("ic_inf_exp_brl", -math.inf) > self.ic_inf_min_brl:
            motivos.append("IC da expectativa liquida inclui zero ou menos")
        return (not motivos), motivos


def metricas_oos(pred: pd.DataFrame, am: Amostras, prior: dict[str, np.ndarray], criterio: CriterioOOS
                 ) -> dict[str, float]:
    """Metricas fora da amostra. `pred` alinhado a `am` (mesma ordem)."""
    n = len(am)
    ll_m, ll_r, p_alvo, y_alvo = [], [], [], []
    for lado in LADOS:
        p = pred[[f"p_{c}_{lado}" for c in rot.NOMES_CLASSES]].to_numpy()
        y = am.y(lado)
        ll_m.append(log_loss_multiclasse(p, y))
        ll_r.append(log_loss_multiclasse(np.tile(prior[lado], (n, 1)), y))
        p_alvo.append(p[:, rot.ALVO])
        y_alvo.append((y == rot.ALVO).astype(float))
    ec, ev = pred["e_brl_compra"].to_numpy(), pred["e_brl_venda"].to_numpy()
    melhor_compra = ec >= ev
    e_best = np.where(melhor_compra, ec, ev)
    pnl_sel = np.where(melhor_compra, am.pnl_compra, am.pnl_venda)
    sel = e_best > criterio.limiar_e_brl
    out = {
        "n": float(n),
        "log_loss": float(np.mean(ll_m)),
        "log_loss_prior": float(np.mean(ll_r)),
        "ganho_log_loss": float(np.mean(ll_r) - np.mean(ll_m)),
        "ece_alvo": float(ece(np.concatenate(p_alvo), np.concatenate(y_alvo))),
        "exp_base_brl": float(np.mean(np.concatenate([am.pnl_compra, am.pnl_venda]))),
        "n_selecionadas": float(sel.sum()),
    }
    if sel.sum() >= 2:
        x = pnl_sel[sel]
        cl = (am.t_ns[sel] // (criterio.cluster_s * NS)).astype(np.int64)
        m = x.mean()
        _, inv = np.unique(cl, return_inverse=True)
        soma = np.bincount(inv, weights=x - m)
        ep = math.sqrt(float(np.sum(soma ** 2))) / len(x)
        out["exp_sel_brl"] = float(m)
        out["ic_inf_exp_brl"] = float(m - 1.96 * ep)
    else:
        out["exp_sel_brl"] = float("nan")
        out["ic_inf_exp_brl"] = -math.inf
    return out


def hash_codigo() -> str:
    """Hash do codigo do pipeline (features + modelos) que produz o artefato."""
    raiz = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for sub in ("features", "modelos"):
        for f in sorted((raiz / sub).glob("*.py")):
            h.update(f.name.encode())
            h.update(f.read_bytes())
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------------------------
def _classificador(tipo: str, seed: int):
    if tipo == "lightgbm":
        from lightgbm import LGBMClassifier
        return LGBMClassifier(random_state=seed, **PARAMS_LIGHTGBM)
    if tipo == "logistica":
        return make_pipeline(SimpleImputer(strategy="median", keep_empty_features=True), StandardScaler(),
                             LogisticRegression(C=1.0, max_iter=500))
    raise ValueError(f"tipo de modelo desconhecido: {tipo}")


def _proba3(clf, X: pd.DataFrame) -> np.ndarray:
    p = clf.predict_proba(X)
    classes = clf.classes_ if hasattr(clf, "classes_") else clf[-1].classes_
    out = np.zeros((len(X), 3))
    for j, c in enumerate(classes):
        out[:, int(c)] = p[:, j]
    return out


class ModeloMicroestrutura:
    """Implementa o Protocolo `ModeloPreditivo` (nome, status, prever)."""

    def __init__(self, tipo: str = "lightgbm", nome: str | None = None, seed: int = 7,
                 fracao_calibracao: float = 0.25):
        self.tipo = tipo
        self.nome = nome or f"microestrutura_{tipo}"
        self.seed = seed
        self.fracao_calibracao = fracao_calibracao
        self._status = "nao_treinado"
        self.colunas: list[str] = []
        self.treinado_ate_ns = 0
        self.origem_dados = ""
        self.hash_dados = ""
        self.hash_criterio_pre_registrado = ""
        self.metricas_oos: dict[str, float] = {}
        self._clf: dict[str, object] = {}
        self._cal: dict[str, CalibradorIsotonico] = {}
        self._payoff: dict[str, np.ndarray] = {}
        self.prior: dict[str, np.ndarray] = {}
        self._holdout_usado = False

    @property
    def status(self) -> str:
        return self._status

    # -- treino ------------------------------------------------------------------------------
    def treinar(self, am: Amostras, origem: str = "desconhecida", criterio: CriterioOOS | None = None) -> None:
        """Treina com `am` (ja purgado pelo chamador). `criterio` e registrado ANTES de qualquer holdout."""
        validar_nomes(am.X.columns)  # recusa feature fora do registro permitido
        if len(am) < 50:
            raise ValueError("amostras insuficientes para treinar")
        ordem = np.argsort(am.t_ns, kind="stable")
        am = am.subconjunto(ordem)
        n_cal = int(len(am) * self.fracao_calibracao)
        t_cal = int(am.t_ns[len(am) - n_cal]) if n_cal else None
        # purga entre o ajuste e a calibracao: o ajuste so usa rotulos conhecidos antes da calibracao
        ajuste = np.flatnonzero(am.t_fim_ns <= t_cal) if t_cal is not None else np.arange(len(am))
        calib = np.arange(len(am) - n_cal, len(am))
        self.colunas = list(am.X.columns)
        for lado in LADOS:
            y = am.y(lado)
            clf = _classificador(self.tipo, self.seed)
            clf.fit(am.X.iloc[ajuste], y[ajuste])
            cal = CalibradorIsotonico(3)
            if n_cal:
                cal.ajustar(_proba3(clf, am.X.iloc[calib]), y[calib])
            self._clf[lado], self._cal[lado] = clf, cal
            pnl = am.pnl(lado)
            self._payoff[lado] = np.array(
                [pnl[ajuste][y[ajuste] == c].mean() if (y[ajuste] == c).any() else 0.0 for c in range(3)])
            self.prior[lado] = np.bincount(y[ajuste], minlength=3) / len(ajuste)
        self.treinado_ate_ns = int(am.t_fim_ns.max())
        self.origem_dados = origem
        self.hash_dados = am.hash_dados()
        self.hash_criterio_pre_registrado = criterio.hash() if criterio else ""
        self._status = "em_validacao"
        self._holdout_usado = False
        self.metricas_oos = {}

    # -- previsao ----------------------------------------------------------------------------
    def prever_lote(self, X: pd.DataFrame) -> pd.DataFrame:
        if self._status == "nao_treinado":
            raise ModeloNaoTreinado(self.nome)
        validar_nomes(X.columns)
        Xr = X.reindex(columns=self.colunas)
        cols: dict[str, np.ndarray] = {}
        for lado in LADOS:
            p = self._cal[lado].transformar(_proba3(self._clf[lado], Xr))
            for j, c in enumerate(rot.NOMES_CLASSES):
                cols[f"p_{c}_{lado}"] = p[:, j]
            cols[f"e_brl_{lado}"] = p @ self._payoff[lado]
        return pd.DataFrame(cols, index=X.index)

    def prever(self, f: VetorFeatures) -> dict[str, float]:
        """P(alvo), P(stop), P(tempo) e E[R$ liq] por lado. `validado`=0.0 enquanto for candidato."""
        df = self.prever_lote(pd.DataFrame([f.valores]))
        out = {k: float(v) for k, v in df.iloc[0].items()}
        out["validado"] = 1.0 if self._status == "validado_oos" else 0.0
        return out

    # -- validacao fora da amostra -------------------------------------------------------------
    def avaliar_oos(self, am_oos: Amostras, criterio: CriterioOOS) -> dict[str, float]:
        """Avalia o holdout UMA vez, com o criterio pre-registrado. E o unico caminho para `validado_oos`."""
        if self._status == "nao_treinado":
            raise ModeloNaoTreinado(self.nome)
        if not self.hash_criterio_pre_registrado:
            raise CriterioAusente("nenhum criterio OOS foi pre-registrado no treino")
        if criterio.hash() != self.hash_criterio_pre_registrado:
            raise CriterioNaoPreRegistrado("criterio diferente do registrado no treino")
        if self._holdout_usado:
            raise HoldoutJaUsado("o holdout so pode ser avaliado uma vez por versao")
        if len(am_oos) and int(am_oos.t_ns.min()) <= self.treinado_ate_ns:
            raise VazamentoOOS("dados OOS nao sao estritamente posteriores ao fim do treino")
        self._holdout_usado = True
        pred = self.prever_lote(am_oos.X)
        m = metricas_oos(pred, am_oos, self.prior, criterio)
        self.metricas_oos = m
        aprovado, _ = criterio.aprova(m)
        if not aprovado:
            self._status = "reprovado"
        elif self.origem_dados in ORIGENS_SEM_SELO or self.origem_dados in ("", "desconhecida"):
            self._status = "em_validacao"  # selo recusado: dados sinteticos/contaminados/sem origem
        else:
            self._status = "validado_oos"
        return m
