"""Janela temporal incremental com somas correntes (interna).

A janela cobre (t - largura, t]. So aceita itens em ordem nao decrescente de tempo.
"""
from __future__ import annotations

from collections import deque


class JanelaSoma:
    __slots__ = ("largura_ns", "_itens", "_s")

    def __init__(self, largura_ns: int, n_somas: int = 1):
        self.largura_ns = largura_ns
        self._itens: deque[tuple[int, tuple[float, ...]]] = deque()
        self._s = [0.0] * n_somas

    def adicionar(self, t_ns: int, *valores: float) -> None:
        if self._itens and t_ns < self._itens[-1][0]:
            raise ValueError("evento fora de ordem na janela")
        self._itens.append((t_ns, valores))
        for i, v in enumerate(valores):
            self._s[i] += v

    def podar(self, t_ns: int) -> None:
        lim = t_ns - self.largura_ns
        it = self._itens
        while it and it[0][0] <= lim:
            _, vs = it.popleft()
            for i, v in enumerate(vs):
                self._s[i] -= v
        if not it:  # evita deriva numerica acumulada
            self._s = [0.0] * len(self._s)

    def somas(self, t_ns: int) -> tuple[float, ...]:
        self.podar(t_ns)
        return tuple(self._s)

    def __len__(self) -> int:
        return len(self._itens)
