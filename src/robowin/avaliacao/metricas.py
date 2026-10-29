"""Metricas de desempenho a partir de listas de `core.tipos.Trade` (valores em reais)."""
from __future__ import annotations

import datetime as dt
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from robowin.core.tipos import Trade

FUSO_B3 = ZoneInfo("America/Sao_Paulo")


def dia_pregao(trade: Trade) -> str:
    """Data (AAAA-MM-DD, fuso de Sao Paulo) do pregao em que a operacao foi encerrada."""
    t = dt.datetime.fromtimestamp(trade.t_saida_ns / 1e9, tz=dt.UTC).astimezone(FUSO_B3)
    return t.date().isoformat()


def ordenar(trades: Sequence[Trade]) -> list[Trade]:
    return sorted(trades, key=lambda t: (t.t_saida_ns, t.id))


def liquidos(trades: Sequence[Trade]) -> np.ndarray:
    return np.array([t.liquido_brl for t in trades], dtype=float)


def curva_capital(trades: Sequence[Trade], capital_inicial: float = 0.0) -> np.ndarray:
    """Capital acumulado apos cada operacao, em ordem de saida (sem o ponto inicial)."""
    return capital_inicial + np.cumsum(liquidos(ordenar(trades)))


@dataclass(frozen=True)
class Drawdown:
    maximo_brl: float  # valor positivo (queda do pico ate o fundo)
    duracao_operacoes: int  # operacoes do pico ate a recuperacao (ou fim da serie)
    duracao_dias: float  # em dias corridos, do pico ate a recuperacao (ou fim)
    recuperado: bool


def drawdown(trades: Sequence[Trade]) -> Drawdown:
    ts = ordenar(trades)
    if not ts:
        return Drawdown(0.0, 0, 0.0, True)
    pico, pico_i, pico_t = 0.0, -1, ts[0].t_entrada_ns  # pico inicial = capital antes da 1a operacao
    acum = 0.0
    dd_max, dur_ops, dur_ns, rec = 0.0, 0, 0, True
    for i, t in enumerate(ts):
        acum += t.liquido_brl
        if acum >= pico:
            pico, pico_i, pico_t = acum, i, t.t_saida_ns
            continue
        dd = pico - acum
        if dd > dd_max:
            dd_max = dd
            j, rec, a = i, False, acum
            for k in range(i + 1, len(ts)):
                a += ts[k].liquido_brl
                j = k
                if a >= pico:
                    rec = True
                    break
            dur_ops = j - pico_i
            dur_ns = ts[j].t_saida_ns - pico_t
    return Drawdown(dd_max, dur_ops, dur_ns / 86_400e9, rec)


def agrupar_por_dia(trades: Sequence[Trade]) -> dict[str, list[Trade]]:
    g: dict[str, list[Trade]] = defaultdict(list)
    for t in ordenar(trades):
        g[dia_pregao(t)].append(t)
    return dict(sorted(g.items()))


def pnl_por_dia(trades: Sequence[Trade]) -> dict[str, float]:
    return {d: float(sum(t.liquido_brl for t in ts)) for d, ts in agrupar_por_dia(trades).items()}


def distribuicao_diaria(trades: Sequence[Trade]) -> dict[str, float]:
    pnl = np.array(list(pnl_por_dia(trades).values()), dtype=float)
    if pnl.size == 0:
        return {"dias": 0, "media": 0.0, "desvio": 0.0, "minimo": 0.0, "maximo": 0.0, "mediana": 0.0,
                "p05": 0.0, "p95": 0.0, "dias_positivos": 0, "dias_negativos": 0}
    return {
        "dias": int(pnl.size), "media": float(pnl.mean()),
        "desvio": float(pnl.std(ddof=1)) if pnl.size > 1 else 0.0,
        "minimo": float(pnl.min()), "maximo": float(pnl.max()), "mediana": float(np.median(pnl)),
        "p05": float(np.percentile(pnl, 5)), "p95": float(np.percentile(pnl, 95)),
        "dias_positivos": int((pnl > 0).sum()), "dias_negativos": int((pnl < 0).sum()),
    }


def fator_de_lucro(x: np.ndarray) -> float:
    """Soma dos ganhos / soma das perdas (modulo). inf sem perdas e com ganhos; nan sem ambos."""
    ganhos = float(x[x > 0].sum())
    perdas = float(-x[x < 0].sum())
    if perdas == 0:
        return math.inf if ganhos > 0 else math.nan
    return ganhos / perdas


@dataclass(frozen=True)
class ResumoMetricas:
    n: int
    dias: int
    periodo_ini: str | None
    periodo_fim: str | None
    acertos: int
    taxa_acerto: float  # liquido > 0
    lucro_bruto_brl: float
    lucro_liquido_brl: float
    ganho_medio_brl: float  # media das operacoes vencedoras
    perda_media_brl: float  # modulo da media das perdedoras
    expectativa_brl: float
    desvio_brl: float
    fator_lucro: float
    razao_ganho_perda: float  # ganho medio / perda media
    drawdown_max_brl: float
    drawdown_duracao_operacoes: int
    drawdown_duracao_dias: float
    custos_brl: float  # tarifas + corretagem
    slippage_total_brl: float
    slippage_medio_brl: float

    def como_dict(self) -> dict:
        return asdict(self)


def resumo(trades: Sequence[Trade]) -> ResumoMetricas:
    ts = ordenar(trades)
    n = len(ts)
    if n == 0:
        nan = math.nan
        return ResumoMetricas(0, 0, None, None, 0, nan, 0.0, 0.0, nan, nan, nan, nan, nan, nan, 0.0, 0, 0.0,
                              0.0, 0.0, nan)
    x = liquidos(ts)
    venc, perd = x[x > 0], x[x < 0]
    dd = drawdown(ts)
    dias = sorted({dia_pregao(t) for t in ts})
    ganho_medio = float(venc.mean()) if venc.size else 0.0
    perda_media = float(-perd.mean()) if perd.size else 0.0
    return ResumoMetricas(
        n=n, dias=len(dias), periodo_ini=dias[0], periodo_fim=dias[-1],
        acertos=int(venc.size), taxa_acerto=float(venc.size / n),
        lucro_bruto_brl=float(sum(t.bruto_brl for t in ts)), lucro_liquido_brl=float(x.sum()),
        ganho_medio_brl=ganho_medio, perda_media_brl=perda_media, expectativa_brl=float(x.mean()),
        desvio_brl=float(x.std(ddof=1)) if n > 1 else 0.0, fator_lucro=fator_de_lucro(x),
        razao_ganho_perda=ganho_medio / perda_media if perda_media else math.nan,
        drawdown_max_brl=dd.maximo_brl, drawdown_duracao_operacoes=dd.duracao_operacoes,
        drawdown_duracao_dias=dd.duracao_dias,
        custos_brl=float(sum(t.tarifas_brl + t.corretagem_brl for t in ts)),
        slippage_total_brl=float(sum(t.slippage_brl for t in ts)),
        slippage_medio_brl=float(np.mean([t.slippage_brl for t in ts])),
    )


# ---------------------------------------------------------------- episodios (uma posicao = um episodio)
def _ids_do_episodio(ep: Mapping[str, Any]) -> list[str]:
    bruto = ep.get("trades_json")
    if not bruto:
        return []
    try:
        v = json.loads(bruto) if isinstance(bruto, str) else bruto
    except (TypeError, ValueError):
        return []
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x.get("id")) if isinstance(x, Mapping) else str(x) for x in v]


def agrupar_por_episodio(trades: Sequence[Trade], episodios: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Agrupa as operacoes (parciais e fechamento) por EPISODIO (linhas de ``posicoes_episodio``).

    Cada trade vai para o episodio que o lista em ``trades_json``; sem essa lista, para o episodio do MESMO braco
    cujo intervalo ``[t_aberta_ns, t_fechada_ns]`` contem a saida do trade. Trade sem episodio vira um episodio
    proprio (``id`` = id do trade, ``origem = "trade_avulso"``): sem parciais, episodio = operacao, como hoje.
    Devolve uma linha por episodio, com o liquido somado, em ordem de fechamento."""
    eps = sorted(episodios, key=lambda e: (int(e.get("t_aberta_ns") or 0), str(e.get("id"))))
    por_id: dict[str, list[Trade]] = {str(e.get("id")): [] for e in eps}
    dono: dict[str, str] = {}
    for e in eps:
        for tid in _ids_do_episodio(e):
            dono.setdefault(tid, str(e.get("id")))
    avulsos: list[Trade] = []
    for t in ordenar(trades):
        eid = dono.get(t.id)
        if eid is None:
            for e in eps:
                ini, fim = e.get("t_aberta_ns"), e.get("t_fechada_ns")
                if (str(e.get("braco")) == t.braco and ini is not None and int(ini) <= t.t_saida_ns
                        and (fim is None or t.t_saida_ns <= int(fim)) and not _ids_do_episodio(e)):
                    eid = str(e.get("id"))
                    break
        if eid is None:
            avulsos.append(t)
        else:
            por_id[eid].append(t)
    out: list[dict[str, Any]] = []
    for e in eps:
        eid = str(e.get("id"))
        ts = por_id[eid]
        liq = float(sum(t.liquido_brl for t in ts)) if ts else float(e.get("liquido_total") or 0.0)
        fim = max((t.t_saida_ns for t in ts), default=int(e.get("t_fechada_ns") or e.get("t_aberta_ns") or 0))
        np_ep = e.get("n_parciais")
        out.append({"id": eid, "braco": str(e.get("braco")), "origem": "episodio", "n_trades": len(ts),
                    "n_parciais": int(np_ep) if np_ep is not None else max(len(ts) - 1, 0),
                    "liquido_brl": liq, "t_fechada_ns": fim, "motivo_final": e.get("motivo_final"),
                    "sombra_de": e.get("sombra_de"), "trades": [t.id for t in ts]})
    for t in avulsos:
        out.append({"id": t.id, "braco": t.braco, "origem": "trade_avulso", "n_trades": 1, "n_parciais": 0,
                    "liquido_brl": t.liquido_brl, "t_fechada_ns": t.t_saida_ns, "motivo_final": t.motivo_saida,
                    "sombra_de": None, "trades": [t.id]})
    return sorted(out, key=lambda x: (x["t_fechada_ns"], x["id"]))
