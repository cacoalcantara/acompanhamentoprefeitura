"""Markouts de preenchimentos: mede selecao adversa.

markout(h) = sinal * (meio(t_fill + h) - preco_fill), sinal = +1 para compra preenchida, -1 para venda.
Positivo = o mercado andou a nosso favor depois do fill; markout MEDIO negativo e curto prazo = o fill
tende a ocorrer quando o mercado esta indo contra nos (selecao adversa). `captura_inicial` e
sinal * (meio(t_fill) - preco): o meio-spread "capturado" no instante do fill (antes de andar).
O meio em t e o ULTIMO meio conhecido em ou antes de t (sem olhar o futuro alem do horizonte pedido).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..core.tipos import Lado, Tick


@dataclass(frozen=True)
class Preenchimento:
    t_ns: int
    lado: Lado
    preco: float


@dataclass(frozen=True)
class ResumoMarkout:
    horizonte_s: float
    media_pts: float
    mediana_pts: float
    n: int
    n_sem_dados: int


def serie_meio(ticks: Sequence[Tick]) -> tuple[np.ndarray, np.ndarray]:
    t = np.array([x.t_utc_ns for x in ticks], dtype=np.int64)
    m = np.array([((x.bid if x.bid > 0 else x.last) + (x.ask if x.ask > 0 else x.last)) / 2 for x in ticks])
    return t, m


def markouts(fills: Sequence[Preenchimento], t_ns: np.ndarray, meio: np.ndarray,
             horizontes_s: Sequence[float] = (1.0, 5.0, 30.0)) -> tuple[np.ndarray, list[ResumoMarkout], float]:
    """Retorna (matriz [n_fills, n_horizontes] em pontos, com NaN sem dados; resumos; captura inicial media)."""
    out = np.full((len(fills), len(horizontes_s)), np.nan)
    capt = []
    for i, f in enumerate(fills):
        sg = 1.0 if f.lado == Lado.COMPRA else -1.0
        j0 = int(np.searchsorted(t_ns, f.t_ns, side="right")) - 1
        if j0 >= 0:
            capt.append(sg * (meio[j0] - f.preco))
        for k, h in enumerate(horizontes_s):
            alvo = f.t_ns + int(h * 1e9)
            if alvo > t_ns[-1]:
                continue  # sem dados no horizonte
            j = int(np.searchsorted(t_ns, alvo, side="right")) - 1
            if j >= 0:
                out[i, k] = sg * (meio[j] - f.preco)
    resumos = []
    for k, h in enumerate(horizontes_s):
        col = out[:, k]
        ok = col[~np.isnan(col)]
        resumos.append(ResumoMarkout(h, float(ok.mean()) if ok.size else float("nan"),
                                     float(np.median(ok)) if ok.size else float("nan"), int(ok.size),
                                     int(len(col) - ok.size)))
    return out, resumos, float(np.mean(capt)) if capt else float("nan")
