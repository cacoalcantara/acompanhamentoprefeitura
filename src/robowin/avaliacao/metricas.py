"""Metricas de desempenho a partir de listas de `core.tipos.Trade` (valores em reais)."""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
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
