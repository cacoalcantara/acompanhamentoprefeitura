"""Estimador de Hayashi-Yoshida (covariancia de series assincronas) e lead-lag."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def hy_cov(tx: np.ndarray, x: np.ndarray, ty: np.ndarray, y: np.ndarray) -> float:
    """Soma de dX_i*dY_j sobre todos os pares de intervalos (a,b] que se sobrepoem. Tempos crescentes."""
    tx, x, ty, y = (np.asarray(v, dtype=float) for v in (tx, x, ty, y))
    if len(tx) < 2 or len(ty) < 2:
        return 0.0
    dx, dy = np.diff(x), np.diff(y)
    cy = np.concatenate([[0.0], np.cumsum(dy)])
    a, b = tx[:-1], tx[1:]
    j0 = np.clip(np.searchsorted(ty, a, side="right") - 1, 0, len(dy))  # 1o intervalo de y com fim > a
    j1 = np.clip(np.searchsorted(ty, b, side="left"), 0, len(dy))  # exclusivo: intervalos de y com inicio < b
    j1 = np.maximum(j1, j0)
    return float(np.sum(dx * (cy[j1] - cy[j0])))


def hy_corr(tx: np.ndarray, x: np.ndarray, ty: np.ndarray, y: np.ndarray) -> float:
    vx, vy = float(np.sum(np.diff(x) ** 2)), float(np.sum(np.diff(y) ** 2))
    if vx <= 0 or vy <= 0:
        return 0.0
    return hy_cov(tx, x, ty, y) / float(np.sqrt(vx * vy))


@dataclass(frozen=True)
class ResultadoLeadLag:
    lag_otimo: float  # > 0: x lidera y por lag_otimo (unidade dos timestamps)
    correlacao: float
    lags: tuple[float, ...]
    correlacoes: tuple[float, ...]


def lead_lag(tx: np.ndarray, x: np.ndarray, ty: np.ndarray, y: np.ndarray, lags: np.ndarray) -> ResultadoLeadLag:
    """Maximiza a correlacao HY entre x(t) e y(t+lag) sobre a grade `lags`."""
    ty = np.asarray(ty, dtype=float)
    cs = np.array([hy_corr(tx, x, ty - L, y) for L in lags])
    i = int(np.argmax(cs))
    return ResultadoLeadLag(float(lags[i]), float(cs[i]), tuple(map(float, lags)), tuple(map(float, cs)))
