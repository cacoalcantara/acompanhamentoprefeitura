"""Metricas minimas locais do painel. Ponto de extensao: `metricas_provider` (ver app.criar_app)."""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

N_MIN_AMOSTRA = 30  # abaixo disso o painel avisa "amostra insuficiente"
CONTROLE = "controle_aleatorio"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalo de Wilson (95% por padrao) para uma proporcao."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / d
    meia = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centro - meia), min(1.0, centro + meia))


def percentis(valores: Sequence[float], qs: Sequence[float] = (50, 95, 99)) -> dict[str, float | None]:
    v = sorted(valores)
    out: dict[str, float | None] = {}
    for q in qs:
        if not v:
            out[f"p{int(q)}"] = None
            continue
        pos = (len(v) - 1) * q / 100
        lo, hi = math.floor(pos), math.ceil(pos)
        out[f"p{int(q)}"] = v[lo] + (v[hi] - v[lo]) * (pos - lo)
    return out


def max_drawdown(curva: Sequence[float]) -> float:
    pico, dd = 0.0, 0.0
    for x in curva:
        pico = max(pico, x)
        dd = max(dd, pico - x)
    return dd


def metricas_braco(liquidos: Sequence[float]) -> dict[str, Any]:
    n = len(liquidos)
    ganhos = [x for x in liquidos if x > 0]
    perdas = [x for x in liquidos if x < 0]
    k = len(ganhos)
    lo, hi = wilson(k, n)
    soma_perdas = -sum(perdas)
    fl = (sum(ganhos) / soma_perdas) if soma_perdas > 0 else None
    acum, c = [], 0.0
    for x in liquidos:
        c += x
        acum.append(c)
    return {
        "n": n,
        "acertos": k,
        "taxa_acerto": (k / n) if n else None,
        "wilson_inf": lo,
        "wilson_sup": hi,
        "expectativa_brl": (sum(liquidos) / n) if n else None,
        "fator_lucro": fl,
        "total_brl": sum(liquidos),
        "drawdown_max_brl": max_drawdown(acum),
        "amostra_insuficiente": n < N_MIN_AMOSTRA,
    }


# Assinatura do ponto de extensao: recebe o Banco, devolve {braco: metricas}.
MetricasProvider = Callable[[Any], dict[str, dict[str, Any]]]
