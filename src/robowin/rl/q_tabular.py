"""Q-learning tabular de HORIZONTE FINITO sobre estados discretizados.

Q[h, s, a]: valor esperado da soma de recompensas a partir do passo h (0..H-1) no estado s tomando a e
seguindo a politica gulosa depois. Em h = H o valor e zero (terminal). Atualizacao off-policy:
    Q[h,s,a] += alfa * (r + max_a' Q[h+1,s',a'] * (1-terminal) - Q[h,s,a]).
`induzir_para_tras` resolve exatamente um MDP conhecido (programacao dinamica) para conferir o aprendizado.
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np


class Discretizador:
    """Mapeia um vetor continuo em indice inteiro por bordas por dimensao (np.digitize)."""

    def __init__(self, bordas: Sequence[Sequence[float]]) -> None:
        self.bordas = [np.asarray(b, dtype=float) for b in bordas]
        self.formas = [len(b) + 1 for b in self.bordas]
        self.n_estados = int(np.prod(self.formas))

    def __call__(self, x: Sequence[float]) -> int:
        idx = 0
        for v, b, f in zip(x, self.bordas, self.formas, strict=True):
            idx = idx * f + int(np.digitize(v, b))
        return idx


class QTabularFinito:
    def __init__(self, n_estados: int, n_acoes: int, horizonte: int, alfa: float = 0.1, seed: int = 0) -> None:
        self.S, self.A, self.H, self.alfa = n_estados, n_acoes, horizonte, alfa
        self.Q = np.zeros((horizonte + 1, n_estados, n_acoes))
        self.visitas = np.zeros((horizonte, n_estados, n_acoes), dtype=np.int64)
        self._rng = np.random.default_rng(seed)

    def atualizar(self, h: int, s: int, a: int, r: float, s2: int | None, terminal: bool) -> None:
        h = min(h, self.H - 1)
        alvo = r if terminal or s2 is None or h + 1 >= self.H else r + float(self.Q[h + 1, s2].max())
        self.visitas[h, s, a] += 1
        self.Q[h, s, a] += self.alfa * (alvo - self.Q[h, s, a])

    def gulosa(self, h: int, s: int, permitidas: tuple[int, ...] | None = None) -> int:
        h = min(h, self.H - 1)
        perm = list(range(self.A)) if permitidas is None else list(permitidas)
        q = self.Q[h, s, perm]
        return int(perm[int(np.argmax(q))])  # empate => menor indice (deterministico)

    def probabilidades(self, h: int, s: int, epsilon: float, permitidas: tuple[int, ...] | None = None) -> np.ndarray:
        """Politica epsilon-gulosa (propensao exata de cada acao)."""
        perm = list(range(self.A)) if permitidas is None else list(permitidas)
        p = np.zeros(self.A)
        p[perm] = epsilon / len(perm)
        p[self.gulosa(h, s, tuple(perm))] += 1 - epsilon
        return p

    def escolher(self, h: int, s: int, epsilon: float = 0.1,
                 permitidas: tuple[int, ...] | None = None) -> tuple[int, float]:
        p = self.probabilidades(h, s, epsilon, permitidas)
        a = int(self._rng.choice(self.A, p=p))
        return a, float(p[a])


def induzir_para_tras(P: np.ndarray, R: np.ndarray, horizonte: int) -> np.ndarray:
    """Q* exato de horizonte finito. P[s,a,s'] transicoes, R[s,a] recompensa esperada. Retorna Q[h,s,a]."""
    S, A, _ = P.shape
    Q = np.zeros((horizonte + 1, S, A))
    for h in range(horizonte - 1, -1, -1):
        v = Q[h + 1].max(axis=1)
        Q[h] = R + P @ v
    return Q
