"""Engle-Granger (statsmodels), meia-vida de Ornstein-Uhlenbeck e residuo de cesta. Johansen fora da v1."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from statsmodels.tsa.stattools import coint


@dataclass(frozen=True)
class ResultadoEG:
    pvalor: float
    estatistica: float
    alfa: float  # nivel de significancia usado
    cointegrado: bool
    intercepto: float
    betas: tuple[float, ...]
    residuo: np.ndarray
    meia_vida: float  # em passos; inf se nao reverte


def meia_vida_ou(z: np.ndarray) -> float:
    """Regressao dz_t = a + b z_{t-1}; meia-vida = -ln2 / ln(1+b) (discreta). inf se b >= 0."""
    z = np.asarray(z, dtype=float)
    zl, dz = z[:-1], np.diff(z)
    X = np.column_stack([np.ones_like(zl), zl])
    b = float(np.linalg.lstsq(X, dz, rcond=None)[0][1])
    if b >= 0:
        return float("inf")
    if b <= -1:
        return 0.0
    return float(-np.log(2) / np.log1p(b))


def residuo_cesta(y: np.ndarray, X: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """MQO de y sobre a cesta X (n x k). Devolve (intercepto, betas, residuo)."""
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float).reshape(len(y), -1)
    A = np.column_stack([np.ones(len(y)), X])
    coef = np.linalg.lstsq(A, y, rcond=None)[0]
    return float(coef[0]), coef[1:], y - A @ coef


def engle_granger(y: np.ndarray, X: np.ndarray, alfa: float = 0.05, meia_vida_max: float = np.inf) -> ResultadoEG:
    """Engle-Granger de y contra um ativo ou cesta X. `cointegrado` exige p < alfa e meia-vida <= max."""
    y = np.asarray(y, dtype=float)
    X = np.asarray(X, dtype=float).reshape(len(y), -1)
    est, p, _ = coint(y, X, trend="c", autolag="aic")
    c, betas, res = residuo_cesta(y, X)
    hl = meia_vida_ou(res)
    return ResultadoEG(float(p), float(est), alfa, bool(p < alfa and hl <= meia_vida_max), c,
                       tuple(float(b) for b in betas), res, hl)
