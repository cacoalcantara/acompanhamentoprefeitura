"""Retornos do ponto medio em janelas fixas (em pontos), calculados so com o passado."""
from __future__ import annotations

from bisect import bisect_right

NS = 1_000_000_000
NAN = float("nan")


class HistoricoMeio:
    """Historico (t, ponto medio) com consulta 'valor vigente em t - w' por busca binaria."""

    def __init__(self, janelas_s: tuple[int, ...] = (5, 30, 120, 600)):
        self.janelas_s = janelas_s
        self._max_ns = max(janelas_s) * NS
        self._ts: list[int] = []
        self._ps: list[float] = []
        self._ini = 0

    def atualizar(self, t_ns: int, meio: float) -> None:
        if self._ts and t_ns < self._ts[-1]:
            raise ValueError("evento fora de ordem")
        self._ts.append(t_ns)
        self._ps.append(meio)
        # mantem exatamente um item mais antigo que a maior janela
        lim = t_ns - self._max_ns
        while self._ini + 1 < len(self._ts) and self._ts[self._ini + 1] <= lim:
            self._ini += 1
        if self._ini > 20000:
            del self._ts[: self._ini], self._ps[: self._ini]
            self._ini = 0

    def valores(self, t_ns: int) -> dict[str, float]:
        out = {f"ret_{w}s": NAN for w in self.janelas_s}
        if not self._ts:
            return out
        atual = self._ps[-1]
        for w in self.janelas_s:
            alvo = t_ns - w * NS
            if self._ts[self._ini] > alvo:
                continue  # historico insuficiente
            i = bisect_right(self._ts, alvo, lo=self._ini) - 1
            out[f"ret_{w}s"] = atual - self._ps[i]
        return out
