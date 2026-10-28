"""Comparacoes entre bracos: pareadas, Holm, contrafactuais, beneficio liquido do LLM, controle aleatorio."""
from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence

import numpy as np
from scipy import stats

from robowin.core.tipos import Trade

from . import estatistica as est
from .metricas import dia_pregao, ordenar

STATUS_AMOSTRA_INSUFICIENTE = "amostra_insuficiente"


# ---------------------------------------------------------------- amostra
def amostra_insuficiente(n: int, n_preregistrado: int) -> bool:
    return n < n_preregistrado


def _insuficiente(n: int, n_pre: int | None) -> dict | None:
    if n_pre is not None and amostra_insuficiente(n, n_pre):
        return {"status": STATUS_AMOSTRA_INSUFICIENTE, "n": n, "n_preregistrado": n_pre,
                "mensagem": f"Amostra insuficiente: n={n} < n pre-registrado={n_pre}. Sem conclusao."}
    return None


# ---------------------------------------------------------------- pareamento
def pnl_por_chave(trades: Iterable[Trade], chave: Callable[[Trade], str] = dia_pregao) -> dict[str, float]:
    """Soma do liquido por chave (padrao: pregao). Use `lambda t: t.id` para parear por gatilho/operacao."""
    out: dict[str, float] = {}
    for t in trades:
        k = chave(t)
        out[k] = out.get(k, 0.0) + t.liquido_brl
    return out


def diferencas_pareadas(a: Mapping[str, float], b: Mapping[str, float], n_boot: int = 2000, conf: float = 0.95,
                        seed: int = 0, n_preregistrado: int | None = None) -> dict:
    """Diferenca a - b pareada pelas chaves comuns (pregao ou gatilho). Teste t pareado e IC bootstrap
    (reamostra pares). `p_valor_unilateral` testa H1: media(a - b) > 0."""
    chaves = sorted(set(a) & set(b))
    n = len(chaves)
    ins = _insuficiente(n, n_preregistrado)
    if n < 2:
        return ins or {"status": STATUS_AMOSTRA_INSUFICIENTE, "n_pares": n}
    d = np.array([a[k] - b[k] for k in chaves], dtype=float)
    sd = float(d.std(ddof=1))
    media = float(d.mean())
    if sd == 0:
        t, p2, p1 = (math.inf if media > 0 else (-math.inf if media < 0 else 0.0)), (0.0 if media else 1.0), (
            0.0 if media > 0 else 1.0)
    else:
        t = media / (sd / math.sqrt(n))
        p2 = float(2 * stats.t.sf(abs(t), n - 1))
        p1 = float(stats.t.sf(t, n - 1))
    rng = np.random.default_rng(seed)
    reps = d[rng.integers(n, size=(n_boot, n))].mean(axis=1)
    res = {"status": "ok", "n_pares": n, "media_dif": media, "desvio_dif": sd, "t": float(t),
           "p_valor_bilateral": p2, "p_valor_unilateral": p1, "ic_bootstrap": est.ic_percentil(reps, conf)}
    if ins:
        res["status"] = STATUS_AMOSTRA_INSUFICIENTE
        res["aviso"] = ins["mensagem"]
    return res


# ---------------------------------------------------------------- Holm
def holm(p_valores: Mapping[str, float], alfa: float = 0.05) -> dict[str, dict]:
    """Correcao de Holm-Bonferroni (controle de FWER). Devolve p ajustado (monotono) e decisao."""
    itens = sorted(p_valores.items(), key=lambda kv: kv[1])
    m = len(itens)
    out: dict[str, dict] = {}
    max_ant = 0.0
    parou = False
    for i, (nome, p) in enumerate(itens):
        aj = min(1.0, max(max_ant, (m - i) * p))
        max_ant = aj
        rejeita = (not parou) and p <= alfa / (m - i)
        if not rejeita:
            parou = True
        out[nome] = {"p": p, "p_ajustado": aj, "rejeita_h0": rejeita}
    return out


# ---------------------------------------------------------------- contrafactuais
def _ic_expectativa(trades: Sequence[Trade], n_boot: int, seed: int) -> dict:
    x = [t.liquido_brl for t in trades]
    return est.bootstrap_por_pregao(x, [dia_pregao(t) for t in trades], n_boot=n_boot, seed=seed)


def valor_do_veto(trades_braco: Sequence[Trade], ids_rejeitados: set[str], n_boot: int = 2000, seed: int = 0,
                  n_preregistrado: int | None = None) -> dict:
    """Contrafactual 'valor do veto do revisor': resultado das operacoes que o revisor REJEITOU (e que o braco
    sem revisor executou). Expectativa negativa = o veto evitou prejuizo (valor do veto = -expectativa por
    operacao vetada). `trades_braco` = operacoes do braco sem revisor; `ids_rejeitados` = ids dessas operacoes."""
    rej = ordenar([t for t in trades_braco if t.id in ids_rejeitados])
    apr = ordenar([t for t in trades_braco if t.id not in ids_rejeitados])
    n = len(rej)
    ins = _insuficiente(n, n_preregistrado)
    if n == 0:
        return ins or {"status": STATUS_AMOSTRA_INSUFICIENTE, "n_rejeitadas": 0}
    bs = _ic_expectativa(rej, n_boot, seed)
    inf, sup = bs["expectativa_ic"]
    valor = -float(bs["expectativa"])
    res = {
        "status": STATUS_AMOSTRA_INSUFICIENTE if ins else "ok", "n_rejeitadas": n, "n_aprovadas": len(apr),
        "expectativa_rejeitadas_brl": float(bs["expectativa"]), "expectativa_rejeitadas_ic": (inf, sup),
        "expectativa_aprovadas_brl": float(np.mean([t.liquido_brl for t in apr])) if apr else math.nan,
        "valor_veto_por_operacao_brl": valor, "valor_veto_total_brl": valor * n,
        "veto_agrega_valor": bool(sup < 0) and not ins,
    }
    if ins:
        res["aviso"] = ins["mensagem"]
    return res


def beneficio_liquido_llm(trades_llm: Sequence[Trade], trades_base: Sequence[Trade], custo_api_brl: float,
                          n_boot: int = 2000, conf: float = 0.95, seed: int = 0,
                          n_preregistrado: int | None = None) -> dict:
    """Beneficio liquido do LLM = expectativa incremental (por operacao) - custo de API por operacao.
    IC por bootstrap de PREGOES comuns aos dois bracos (pareado). `custo_api_brl` e o custo total do periodo."""
    d_llm = {d: [t.liquido_brl for t in ts] for d, ts in _por_dia(trades_llm).items()}
    d_base = {d: [t.liquido_brl for t in ts] for d, ts in _por_dia(trades_base).items()}
    dias = sorted(set(d_llm) & set(d_base))
    n_llm = sum(len(d_llm[d]) for d in dias)
    ins = _insuficiente(n_llm, n_preregistrado)
    if not dias or n_llm == 0 or sum(len(d_base[d]) for d in dias) == 0:
        return ins or {"status": STATUS_AMOSTRA_INSUFICIENTE, "n_operacoes_llm": n_llm}
    s_l = np.array([sum(d_llm[d]) for d in dias])
    c_l = np.array([len(d_llm[d]) for d in dias])
    s_b = np.array([sum(d_base[d]) for d in dias])
    c_b = np.array([len(d_base[d]) for d in dias])
    custo_dia = np.full(len(dias), custo_api_brl / len(dias))

    def stat(ix: np.ndarray) -> float:
        nl, nb = c_l[ix].sum(), c_b[ix].sum()
        if nl == 0 or nb == 0:
            return math.nan
        return s_l[ix].sum() / nl - s_b[ix].sum() / nb - custo_dia[ix].sum() / nl

    todos = np.arange(len(dias))
    rng = np.random.default_rng(seed)
    reps = np.array([stat(rng.integers(len(dias), size=len(dias))) for _ in range(n_boot)])
    incremental = float(s_l.sum() / c_l.sum() - s_b.sum() / c_b.sum())
    custo_op = custo_api_brl / n_llm
    ic = est.ic_percentil(reps, conf)
    res = {
        "status": STATUS_AMOSTRA_INSUFICIENTE if ins else "ok", "pregoes": len(dias), "n_operacoes_llm": int(n_llm),
        "expectativa_incremental_brl": incremental, "custo_api_por_operacao_brl": custo_op,
        "beneficio_liquido_brl": stat(todos), "ic_bootstrap": ic,
        "llm_se_justifica": bool(ic[0] > 0) and not ins,
    }
    if ins:
        res["aviso"] = ins["mensagem"]
    return res


def _por_dia(trades: Sequence[Trade]) -> dict[str, list[Trade]]:
    g: dict[str, list[Trade]] = {}
    for t in trades:
        g.setdefault(dia_pregao(t), []).append(t)
    return g


# ---------------------------------------------------------------- controle aleatorio
def acerto_controle_teorico(stop_pontos: float, alvo_pontos: float) -> float:
    """Passeio aleatorio simetrico (ruina do jogador), sem limite de tempo e sem custos: P(alvo antes do stop)."""
    return stop_pontos / (stop_pontos + alvo_pontos)


def gerar_controle_aleatorio(n: int, stop_pontos: float, alvo_pontos: float, custos_brl: float,
                             ponto_valor_brl: float, seed: int = 0) -> np.ndarray:
    """Resultados liquidos de n entradas aleatorias com a mesma geometria (stop/alvo) e os mesmos custos."""
    rng = np.random.default_rng(seed)
    ganho = rng.random(n) < acerto_controle_teorico(stop_pontos, alvo_pontos)
    return np.where(ganho, alvo_pontos * ponto_valor_brl, -stop_pontos * ponto_valor_brl) - custos_brl


def comparar_com_controle(k_braco: int, n_braco: int, k_controle: int, n_controle: int,
                          n_preregistrado: int | None = None) -> dict:
    """Teste H1: p_braco > p_controle (Fisher exato unilateral) + ICs de Wilson lado a lado."""
    ins = _insuficiente(n_braco, n_preregistrado)
    if n_braco == 0 or n_controle == 0:
        return ins or {"status": STATUS_AMOSTRA_INSUFICIENTE, "n": n_braco}
    _, p = stats.fisher_exact([[k_braco, n_braco - k_braco], [k_controle, n_controle - k_controle]],
                              alternative="greater")
    res = {
        "status": STATUS_AMOSTRA_INSUFICIENTE if ins else "ok",
        "p_braco": k_braco / n_braco, "ic_wilson_braco": est.ic_wilson(k_braco, n_braco),
        "p_controle": k_controle / n_controle, "ic_wilson_controle": est.ic_wilson(k_controle, n_controle),
        "diferenca_pp": 100 * (k_braco / n_braco - k_controle / n_controle),
        "p_valor_braco_maior": float(p),
    }
    if ins:
        res["aviso"] = ins["mensagem"]
    return res
