"""Filtro de Kalman para razao de hedge dinamica: y_t = alpha_t + beta_t * x_t + e_t (estados em passeio aleatorio)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class PassoKalman:
    alpha: float
    beta: float
    inovacao: float  # erro de previsao a priori (y - yhat) com estados ate t-1
    var_inovacao: float
    z: float  # inovacao padronizada


class KalmanHedge:
    """Estimativa recursiva e causal. `delta` controla a velocidade de adaptacao (menor = mais lento)."""

    def __init__(self, delta: float = 1e-4, var_obs: float = 1e-3, beta0: float = 0.0, alpha0: float = 0.0,
                 p0: float = 1e3) -> None:
        self.Vw = delta / (1 - delta) * np.eye(2)
        self.Ve = float(var_obs)
        self.theta = np.array([alpha0, beta0], dtype=float)
        self.P = np.eye(2) * p0
        self.n = 0

    def atualizar(self, y: float, x: float) -> PassoKalman:
        h = np.array([1.0, x])
        P = self.P + self.Vw
        e = y - float(h @ self.theta)
        S = float(h @ P @ h) + self.Ve
        K = P @ h / S
        self.theta = self.theta + K * e
        self.P = P - np.outer(K, h) @ P
        self.n += 1
        return PassoKalman(float(self.theta[0]), float(self.theta[1]), e, S, e / float(np.sqrt(S)))

    @property
    def beta(self) -> float:
        return float(self.theta[1])

    @property
    def alpha(self) -> float:
        return float(self.theta[0])


def filtrar(y: np.ndarray, x: np.ndarray, **kw: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Roda o filtro sobre series; devolve (beta, alpha, z) por instante."""
    kf = KalmanHedge(**kw)
    b, a, z = np.empty(len(y)), np.empty(len(y)), np.empty(len(y))
    for i, (yi, xi) in enumerate(zip(y, x, strict=True)):
        p = kf.atualizar(float(yi), float(xi))
        b[i], a[i], z[i] = p.beta, p.alpha, p.z
    return b, a, z
