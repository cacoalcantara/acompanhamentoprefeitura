"""Bandit contextual com amostragem de Thompson linear (regressao bayesiana ridge por braco).

Maximiza recompensa (aqui: -custo de execucao). Para a avaliacao off-policy (IPS) a propensao REGISTRADA
precisa ser exata para a distribuicao realmente usada na amostragem; por isso `probabilidades` estima por
Monte Carlo (com rng PROPRIO, deterministico) P_TS(a|x) e mistura com um piso exploratorio:
    p(a|x) = (1 - piso) * P_TS(a|x) + piso / K,
e a acao e sorteada DESSA mistura. Logo a propensao devolvida e a probabilidade exata de ter escolhido a acao.
"""
from __future__ import annotations

import numpy as np


class BanditThompsonLinear:
    def __init__(self, n_acoes: int, dim: int, seed: int = 0, lambda_: float = 1.0, sigma: float = 1.0,
                 piso: float = 0.05, n_amostras: int = 200) -> None:
        if not 0.0 <= piso < 1.0:
            raise ValueError("piso em [0,1)")
        self.k, self.d = n_acoes, dim
        self.sigma, self.piso, self.n_amostras = sigma, piso, n_amostras
        self._A = [lambda_ * np.eye(dim) for _ in range(n_acoes)]
        self._b = [np.zeros(dim) for _ in range(n_acoes)]
        self._rng = np.random.default_rng(seed)       # sorteio da acao
        self._rng_p = np.random.default_rng(seed + 1)  # Monte Carlo das probabilidades
        self.n_obs = [0] * n_acoes

    def _posterior(self, a: int) -> tuple[np.ndarray, np.ndarray]:
        cov_inv = self._A[a]
        mu = np.linalg.solve(cov_inv, self._b[a])
        cov = self.sigma**2 * np.linalg.inv(cov_inv)
        return mu, (cov + cov.T) / 2

    def probabilidades(self, x: np.ndarray, permitidas: tuple[int, ...] | None = None) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        perm = list(range(self.k)) if permitidas is None else list(permitidas)
        amostras = np.empty((self.n_amostras, len(perm)))
        for j, a in enumerate(perm):
            mu, cov = self._posterior(a)
            th = self._rng_p.multivariate_normal(mu, cov, size=self.n_amostras, method="cholesky")
            amostras[:, j] = th @ x
        venc = np.bincount(np.argmax(amostras, axis=1), minlength=len(perm)) / self.n_amostras
        p = np.zeros(self.k)
        p[perm] = (1 - self.piso) * venc + self.piso / len(perm)
        return p

    def escolher(self, x: np.ndarray, permitidas: tuple[int, ...] | None = None) -> tuple[int, float]:
        """(acao, propensao registravel)."""
        p = self.probabilidades(x, permitidas)
        a = int(self._rng.choice(self.k, p=p / p.sum()))
        return a, float(p[a])

    def atualizar(self, x: np.ndarray, a: int, r: float) -> None:
        x = np.asarray(x, dtype=float)
        self._A[a] += np.outer(x, x)
        self._b[a] += r * x
        self.n_obs[a] += 1

    def media_prevista(self, x: np.ndarray, a: int) -> float:
        mu, _ = self._posterior(a)
        return float(mu @ np.asarray(x, dtype=float))
