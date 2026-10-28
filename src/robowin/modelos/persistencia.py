"""Artefato persistente do modelo de microestrutura (SIMULACAO): salvar/carregar em formato SEGURO e versionado.

Formato (sem pickle, sem codigo executavel):
  * um unico arquivo JSON ``{"formato", "versao_formato", "sha256", "payload"}``;
  * LightGBM no formato nativo de texto (``Booster.model_to_string``), embutido como string;
  * regressao logistica / imputador / padronizador e calibracao isotonica como listas de numeros (JSON,
    ``repr`` de float e exato: ida e volta devolve previsoes IDENTICAS);
  * o ``sha256`` cobre o payload canonico e e conferido ANTES de qualquer uso do conteudo; o registro
    (``RegistroModelos``) guarda o sha256 do arquivo, entao reescrever payload + hash tambem e recusado.

Regras de seguranca na carga (qualquer falha => ``ErroArtefato``/``AtivoDivergente``; os atalhos
``carregar_*_seguro`` nunca levantam e devolvem o motivo, e o chamador segue ``nao_treinado``):
  * hash do arquivo/payload divergente => recusa; hash de dados/codigo divergente do registro => recusa;
  * ativo diferente do pedido (ou ausente) => recusa;
  * origem sintetica/contaminada/desconhecida NUNCA e ``validado_oos`` (nem no arquivo, nem no registro);
  * features fora do registro permitido => recusa.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from robowin.core.instrumentos import AtivoDivergente, exigir_mesmo_ativo
from robowin.features.registro import validar_nomes

from .calibracao import CalibradorIsotonico
from .microestrutura import (
    LADOS,
    STATUS,
    ModeloMicroestrutura,
    _classificador,
    hash_codigo,
)
from .registro import ErroRegistro, RegistroModelos

FORMATO = "robowin-modelo-microestrutura"
VERSAO_FORMATO = 1
ORIGENS = ("sintetico", "historico_real", "contaminado", "desconhecida")
DIR_MODELOS_PADRAO = Path("dados_locais/modelos")


class ErroArtefato(RuntimeError):
    """Artefato ausente, corrompido, adulterado ou incompativel: NUNCA e usado."""


# ---------------------------------------------------------------------------------------------
# JSON canonico e hash
# ---------------------------------------------------------------------------------------------
def _canonico(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_arquivo(caminho: str | Path) -> str:
    return _sha(Path(caminho).read_bytes())


def _arr(a: Any) -> list[float]:
    return np.asarray(a, dtype=float).tolist()


# ---------------------------------------------------------------------------------------------
# Classificadores (serializacao por tipo)
# ---------------------------------------------------------------------------------------------
class _ClfLightGBM:
    """Classificador reconstruido a partir do texto nativo do LightGBM (so predicao)."""

    def __init__(self, texto: str, classes: list[int]):
        import lightgbm as lgb
        self.booster = lgb.Booster(model_str=texto)
        self.classes_ = np.asarray(classes)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        p = np.asarray(self.booster.predict(X))
        return p if p.ndim == 2 else np.column_stack([1 - p, p])


def _ser_clf(tipo: str, clf: Any) -> dict[str, Any]:
    if tipo == "lightgbm":
        booster = clf.booster_ if hasattr(clf, "booster_") else clf.booster
        return {"texto": booster.model_to_string(), "classes": [int(c) for c in clf.classes_]}
    imp, esc, lr = clf.steps[0][1], clf.steps[1][1], clf.steps[2][1]
    return {
        "n_features": int(imp.n_features_in_), "imp_statistics": _arr(imp.statistics_),
        "esc_mean": _arr(esc.mean_), "esc_scale": _arr(esc.scale_), "esc_var": _arr(esc.var_),
        "esc_n": int(np.asarray(esc.n_samples_seen_).reshape(-1)[0]),
        "lr_coef": np.asarray(lr.coef_, float).tolist(), "lr_intercept": _arr(lr.intercept_),
        "lr_classes": [int(c) for c in lr.classes_], "lr_n_iter": [int(i) for i in np.asarray(lr.n_iter_).reshape(-1)],
    }


def _des_clf(tipo: str, d: dict[str, Any], seed: int, colunas: list[str]) -> Any:
    if tipo == "lightgbm":
        return _ClfLightGBM(str(d["texto"]), list(d["classes"]))
    clf = _classificador("logistica", seed)
    imp, esc, lr = clf.steps[0][1], clf.steps[1][1], clf.steps[2][1]
    nomes = np.asarray(colunas, dtype=object)
    imp.n_features_in_ = esc.n_features_in_ = lr.n_features_in_ = int(d["n_features"])
    imp.feature_names_in_ = nomes  # so o imputador ve o DataFrame; os demais recebem ndarray
    imp.statistics_ = np.asarray(d["imp_statistics"], float)
    imp._fit_dtype = imp._fill_dtype = np.dtype(float)
    imp.indicator_ = None
    esc.mean_ = np.asarray(d["esc_mean"], float)
    esc.scale_ = np.asarray(d["esc_scale"], float)
    esc.var_ = np.asarray(d["esc_var"], float)
    esc.n_samples_seen_ = np.int64(d["esc_n"])
    lr.coef_ = np.asarray(d["lr_coef"], float)
    lr.intercept_ = np.asarray(d["lr_intercept"], float)
    lr.classes_ = np.asarray(d["lr_classes"])
    lr.n_iter_ = np.asarray(d["lr_n_iter"], dtype=np.int32)
    return clf


def _ser_cal(cal: CalibradorIsotonico) -> dict[str, Any]:
    isos: list[Any] = []
    for iso in cal._iso:
        if iso is None:
            isos.append(None)
        else:
            isos.append({"x": _arr(iso.X_thresholds_), "y": _arr(iso.y_thresholds_), "xmin": float(iso.X_min_),
                         "xmax": float(iso.X_max_), "crescente": bool(iso.increasing_)})
    return {"n_classes": cal.n_classes, "ajustado": bool(cal.ajustado), "iso": isos}


def _des_cal(d: dict[str, Any]) -> CalibradorIsotonico:
    from sklearn.isotonic import IsotonicRegression
    cal = CalibradorIsotonico(int(d["n_classes"]))
    cal.ajustado = bool(d["ajustado"])
    for c, e in enumerate(d["iso"]):
        if e is None:
            continue
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.X_thresholds_ = np.asarray(e["x"], float)
        iso.y_thresholds_ = np.asarray(e["y"], float)
        iso.X_min_, iso.X_max_ = float(e["xmin"]), float(e["xmax"])
        iso.increasing_ = bool(e["crescente"])
        iso._build_f(iso.X_thresholds_, iso.y_thresholds_)
        cal._iso[c] = iso
    return cal


# ---------------------------------------------------------------------------------------------
# Salvar
# ---------------------------------------------------------------------------------------------
def _como_dict(x: Any) -> dict[str, Any]:
    if x is None:
        return {}
    if is_dataclass(x) and not isinstance(x, type):
        return asdict(x)
    return dict(x)


def _validar_origem_status(origem: str, status: str) -> None:
    if origem not in ORIGENS:
        raise ErroArtefato(f"origem de dados invalida: {origem!r} (esperado: {', '.join(ORIGENS)})")
    if status not in STATUS:
        raise ErroArtefato(f"status invalido: {status!r}")
    if status == "validado_oos" and origem != "historico_real":
        raise ErroArtefato(f"origem {origem!r} nunca recebe o selo validado_oos")


def salvar_artefato(modelo: ModeloMicroestrutura, caminho: str | Path, *, versao: str, barreiras: Any = None,
                    custos: Any = None) -> str:
    """Grava o artefato completo de forma atomica. Devolve o sha256 do ARQUIVO (para o registro)."""
    if modelo.status == "nao_treinado":
        raise ErroArtefato("modelo nao treinado: nada a salvar")
    if not modelo.ativo:
        raise ErroArtefato("modelo sem ativo declarado: recusado (um artefato serve a um unico ativo)")
    origem = modelo.origem_dados or "desconhecida"
    _validar_origem_status(origem, modelo.status)
    validar_nomes(modelo.colunas)
    payload = {
        "meta": {
            "nome": modelo.nome, "versao": versao, "tipo": modelo.tipo, "seed": modelo.seed,
            "fracao_calibracao": modelo.fracao_calibracao, "ativo": modelo.ativo, "status": modelo.status,
            "origem_dados": origem, "hash_dados": modelo.hash_dados, "hash_codigo": hash_codigo(),
            "janela_treino_ns": list(modelo.janela_treino_ns), "treinado_ate_ns": int(modelo.treinado_ate_ns),
            "criterio_oos_hash": modelo.hash_criterio_pre_registrado,
            "metricas_oos": {k: float(v) for k, v in modelo.metricas_oos.items()},
            "barreiras": _como_dict(barreiras or modelo.config_barreiras),
            "custos": _como_dict(custos or modelo.custos),
            "features": list(modelo.colunas),
        },
        "estado": {
            "payoff": {lado: _arr(modelo._payoff[lado]) for lado in LADOS},
            "prior": {lado: _arr(modelo.prior[lado]) for lado in LADOS},
            "clf": {lado: _ser_clf(modelo.tipo, modelo._clf[lado]) for lado in LADOS},
            "cal": {lado: _ser_cal(modelo._cal[lado]) for lado in LADOS},
            "holdout_usado": bool(modelo._holdout_usado),
        },
    }
    doc = {"formato": FORMATO, "versao_formato": VERSAO_FORMATO, "sha256": _sha(_canonico(payload)),
           "payload": payload}
    dados = json.dumps(doc, sort_keys=True, allow_nan=False).encode("utf-8")
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=caminho.parent, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(dados)
    os.replace(tmp, caminho)
    return _sha(dados)


# ---------------------------------------------------------------------------------------------
# Carregar
# ---------------------------------------------------------------------------------------------
def _ler(caminho: Path, sha_arquivo_esperado: str | None) -> dict[str, Any]:
    try:
        bruto = caminho.read_bytes()
    except OSError as e:
        raise ErroArtefato(f"artefato ilegivel/ausente: {caminho} ({e.__class__.__name__})") from None
    if sha_arquivo_esperado is not None and _sha(bruto) != sha_arquivo_esperado:
        raise ErroArtefato("hash do arquivo diverge do registrado: artefato adulterado ou substituido")
    try:
        doc = json.loads(bruto.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ErroArtefato("artefato corrompido (JSON invalido)") from None
    if not isinstance(doc, dict) or doc.get("formato") != FORMATO or not isinstance(doc.get("payload"), dict):
        raise ErroArtefato("artefato com formato desconhecido")
    if doc.get("versao_formato") != VERSAO_FORMATO:
        raise ErroArtefato(f"versao de formato nao suportada: {doc.get('versao_formato')!r}")
    try:
        ok = _sha(_canonico(doc["payload"])) == doc.get("sha256")
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise ErroArtefato("hash do conteudo diverge: artefato adulterado ou corrompido")
    return doc["payload"]


def carregar_artefato(caminho: str | Path, *, ativo: str, sha_arquivo: str | None = None,
                      entrada: Any = None, origem_registro: str | None = None, status_registro: str | None = None,
                      conferir_codigo: bool = True) -> ModeloMicroestrutura:
    """Carrega e valida o artefato. `entrada` (EntradaRegistroModelo) e `origem_registro`/`status_registro`
    cruzam o arquivo com o registro. Levanta `ErroArtefato` ou `AtivoDivergente`; nunca devolve modelo duvidoso."""
    payload = _ler(Path(caminho), sha_arquivo)
    try:
        return _montar(payload, ativo, entrada, origem_registro, status_registro, conferir_codigo)
    except (ErroArtefato, AtivoDivergente):
        raise
    except Exception as e:  # estrutura inesperada: nunca uma excecao crua para o chamador
        raise ErroArtefato(f"artefato invalido ({e.__class__.__name__}: {e})") from None


def _montar(payload: dict[str, Any], ativo: str, entrada: Any, origem_registro: str | None,
            status_registro: str | None, conferir_codigo: bool) -> ModeloMicroestrutura:
    meta, est = payload["meta"], payload["estado"]
    exigir_mesmo_ativo(ativo, str(meta.get("ativo", "")), f"artefato {meta.get('nome')}@{meta.get('versao')}")
    origem, status = str(meta["origem_dados"]), str(meta["status"])
    if entrada is not None:
        if entrada.nome != meta["nome"] or entrada.versao != meta["versao"]:
            raise ErroArtefato("artefato nao e a versao registrada (nome/versao divergem)")
        exigir_mesmo_ativo(ativo, entrada.ativo, f"registro {entrada.nome}@{entrada.versao}")
        if entrada.hash_dados != meta["hash_dados"]:
            raise ErroArtefato("hash dos dados diverge do registro")
        if entrada.hash_codigo != meta["hash_codigo"]:
            raise ErroArtefato("hash do codigo diverge do registro")
        status = entrada.status  # a fonte de verdade do status e o registro
    if origem_registro is not None and origem_registro != origem:
        raise ErroArtefato("origem dos dados diverge do registro")
    if status_registro is not None:
        status = status_registro
    _validar_origem_status(origem, status)
    if conferir_codigo and meta["hash_codigo"] != hash_codigo():
        raise ErroArtefato("hash do codigo diverge do codigo atual: modelo treinado com outra versao do pipeline")
    validar_nomes(meta["features"])
    tipo = str(meta["tipo"])
    if tipo not in ("lightgbm", "logistica"):
        raise ErroArtefato(f"tipo de modelo desconhecido: {tipo!r}")
    m = ModeloMicroestrutura(tipo, nome=str(meta["nome"]), seed=int(meta["seed"]),
                             fracao_calibracao=float(meta["fracao_calibracao"]))
    m.colunas = [str(c) for c in meta["features"]]
    for lado in LADOS:
        m._clf[lado] = _des_clf(tipo, est["clf"][lado], m.seed, m.colunas)
        m._cal[lado] = _des_cal(est["cal"][lado])
        m._payoff[lado] = np.asarray(est["payoff"][lado], float)
        m.prior[lado] = np.asarray(est["prior"][lado], float)
    m.ativo = str(meta["ativo"])
    m.origem_dados, m.hash_dados = origem, str(meta["hash_dados"])
    m.hash_criterio_pre_registrado = str(meta["criterio_oos_hash"])
    m.treinado_ate_ns = int(meta["treinado_ate_ns"])
    m.janela_treino_ns = (int(meta["janela_treino_ns"][0]), int(meta["janela_treino_ns"][1]))
    m.metricas_oos = {k: float(v) for k, v in meta["metricas_oos"].items()}
    m.config_barreiras, m.custos = dict(meta["barreiras"]), dict(meta["custos"])
    m._holdout_usado = bool(est["holdout_usado"])
    m._status = status
    m.versao = str(meta["versao"])
    return m


# ---------------------------------------------------------------------------------------------
# Integracao com o RegistroModelos (um registro por ATIVO)
# ---------------------------------------------------------------------------------------------
def caminho_registro_ativo(ativo: str, raiz: str | Path = DIR_MODELOS_PADRAO) -> Path:
    """Registro de modelos do ATIVO: ``<raiz>/<ATIVO>/registro.json`` (artefatos ao lado, em ``artefatos/``)."""
    if not ativo or "/" in ativo or "\\" in ativo or ativo.startswith("."):
        raise ErroArtefato(f"ativo invalido para caminho: {ativo!r}")
    return Path(raiz) / ativo.upper() / "registro.json"


def _dir_artefatos(reg: RegistroModelos) -> Path:
    if reg.caminho is None:
        raise ErroArtefato("registro sem arquivo: nao ha onde guardar artefatos")
    return reg.caminho.parent / "artefatos"


def _nome_arquivo(nome: str, versao: str) -> str:
    seguro = "".join(c if c.isalnum() or c in "-_." else "_" for c in f"{nome}__{versao}")
    return f"{seguro}.json"


def registrar_e_salvar(reg: RegistroModelos, modelo: ModeloMicroestrutura, versao: str, *, barreiras: Any = None,
                       custos: Any = None) -> Path:
    """Registra a versao (em_validacao) no registro do ativo e grava o artefato ao lado."""
    reg.registrar_modelo(modelo, versao)
    caminho = _dir_artefatos(reg) / _nome_arquivo(modelo.nome, versao)
    sha = salvar_artefato(modelo, caminho, versao=versao, barreiras=barreiras, custos=custos)
    reg.registrar_artefato(modelo.nome, versao, caminho.name, sha)
    return caminho


def carregar_versao(reg: RegistroModelos, nome: str, versao: str, ativo: str) -> ModeloMicroestrutura:
    """Carrega a versao do registro, cruzando ativo, hashes, origem e status. Levanta em qualquer divergencia."""
    entrada = reg.exigir_ativo(nome, versao, ativo)  # AtivoDivergente se de outro ativo
    info = reg.artefato(nome, versao)
    if info is None:
        raise ErroArtefato(f"{nome}@{versao} sem artefato persistido")
    caminho = _dir_artefatos(reg) / Path(info["arquivo"]).name  # so o nome: sem sair da pasta
    m = carregar_artefato(caminho, ativo=ativo, sha_arquivo=info["sha256"], entrada=entrada,
                          origem_registro=reg.origem(nome, versao), status_registro=entrada.status)
    return m


def versao_a_carregar(reg: RegistroModelos, nome: str) -> tuple[str, str] | None:
    """(versao, motivo): o CAMPEAO (validado_oos); sem campeao, a versao `em_validacao` mais recente
    (o braco quant a trata como EXPLORATORIA, rotulada). `reprovado`/`nao_treinado` nunca sao escolhidos."""
    v = reg.campeao(nome)
    if v is not None:
        return v, "campeao"
    cand = [(reg.entrada(nome, x).treinado_ate_ns, x) for x in reg.versoes(nome)
            if reg.entrada(nome, x).status == "em_validacao"]
    return (max(cand)[1], "exploratorio") if cand else None


@dataclass(frozen=True)
class ResultadoCarga:
    modelo: ModeloMicroestrutura | None
    versao: str | None = None
    papel: str = ""  # "campeao" | "exploratorio" | ""
    motivo: str = ""  # por que NAO carregou (modelo None)

    @property
    def carregado(self) -> bool:
        return self.modelo is not None


def carregar_para_ativo(reg: RegistroModelos | str | Path, nome: str, ativo: str) -> ResultadoCarga:
    """Ponto de carga do nucleo/simulacao. NUNCA levanta: sem artefato valido, `modelo=None` e o quant segue
    `nao_treinado` (e NAO opera). Reversao por ponteiro => recarrega o campeao anterior."""
    try:
        if not isinstance(reg, RegistroModelos):
            if not Path(reg).is_file():
                return ResultadoCarga(None, motivo=f"registro de modelos ausente: {reg}")
            reg = RegistroModelos(reg)
        alvo = versao_a_carregar(reg, nome)
        if alvo is None:
            return ResultadoCarga(None, motivo=f"nenhuma versao carregavel de {nome} (sem campeao nem em_validacao)")
        versao, papel = alvo
        return ResultadoCarga(carregar_versao(reg, nome, versao, ativo), versao, papel)
    except (ErroArtefato, AtivoDivergente, ErroRegistro) as e:
        return ResultadoCarga(None, motivo=f"{e.__class__.__name__}: {e}")
    except Exception as e:  # registro corrompido etc.: recusa sem excecao nao tratada
        return ResultadoCarga(None, motivo=f"falha ao carregar ({e.__class__.__name__}): {e}")
