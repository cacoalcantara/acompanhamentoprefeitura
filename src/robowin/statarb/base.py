"""Base WIN x a vista/ETF: spread dinamico via Kalman com z-score causal (EWMA) do residuo."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .kalman import KalmanHedge


@dataclass(frozen=True)
class EstadoBase:
    beta: float
    base: float  # WIN - alpha - beta*vista (pontos)
    z: float  # z-score EWMA do residuo; media/desvio usam so dados ate t-1
    z_kalman: float


class BaseWinVista:
    def __init__(self, delta: float = 1e-4, var_obs: float = 1.0, halflife_z: float = 60.0, min_obs: int = 30,
                 beta0: float = 0.0) -> None:
        self.kf = KalmanHedge(delta=delta, var_obs=var_obs, beta0=beta0)
        self.lam = 0.5 ** (1.0 / halflife_z)
        self.min_obs = min_obs
        self._m = 0.0
        self._v = 0.0
        self.n = 0

    def atualizar(self, win: float, vista: float) -> EstadoBase:
        passo = self.kf.atualizar(win, vista)
        r = win - passo.alpha - passo.beta * vista
        z = 0.0
        if self.n >= self.min_obs and self._v > 0:
            z = (r - self._m) / float(np.sqrt(self._v))
        if self.n == 0:
            self._m, self._v = r, 0.0
        else:
            d = r - self._m
            self._m += (1 - self.lam) * d
            self._v = self.lam * (self._v + (1 - self.lam) * d * d)
        self.n += 1
        return EstadoBase(passo.beta, r, z, passo.z)
