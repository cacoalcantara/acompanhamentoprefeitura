"""Geradores de series SINTETICAS (rotuladas) para testes e demo. Nada aqui e dado de mercado."""
from __future__ import annotations

import numpy as np


def par_cointegrado(n: int, beta: float = 1.5, alpha: float = 10.0, phi: float = 0.95, sigma_e: float = 1.0,
                    sigma_x: float = 1.0, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """x passeio aleatorio; y = alpha + beta*x + u, u AR(1) (meia-vida = -ln2/ln(phi))."""
    r = np.random.default_rng(seed)
    x = 100 + np.cumsum(r.normal(0, sigma_x, n))
    u = np.zeros(n)
    e = r.normal(0, sigma_e, n)
    for i in range(1, n):
        u[i] = phi * u[i - 1] + e[i]
    return alpha + beta * x + u, x


def passeios_independentes(n: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    r = np.random.default_rng(seed)
    return 100 + np.cumsum(r.normal(0, 1, n)), 100 + np.cumsum(r.normal(0, 1, n))


def ticks_assincronos(duracao: float, lag: float, seed: int = 0, taxa_x: float = 1.0, taxa_y: float = 0.7,
                      sigma: float = 1.0) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Processo latente W (browniano em grade fina); x(t)=W(t), y(t)=W(t-lag): x lidera y por `lag`.
    Observacao em instantes de Poisson distintos. Devolve (tx, x, ty, y)."""
    r = np.random.default_rng(seed)
    dt = 0.01
    grade0 = -lag - 1.0
    n = int((duracao + 2 + lag) / dt)
    W = np.cumsum(r.normal(0, sigma * np.sqrt(dt), n))

    def obs(taxa: float, desloc: float) -> tuple[np.ndarray, np.ndarray]:
        t = np.cumsum(r.exponential(1 / taxa, int(duracao * taxa * 1.5) + 10))
        t = t[t < duracao]
        idx = np.clip(np.round((t - desloc - grade0) / dt).astype(int), 0, n - 1)
        return t, W[idx]

    tx, x = obs(taxa_x, 0.0)
    ty, y = obs(taxa_y, lag)
    return tx, x, ty, y
