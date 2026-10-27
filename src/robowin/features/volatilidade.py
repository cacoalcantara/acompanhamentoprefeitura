"""Volatilidade realizada multiescala (grade fixa de 5s, retornos logaritmicos do ponto medio).

A previsao HAR-RV de Corsi (2009) fica em robowin.modelos.har; aqui so a parte causal incremental.
"""
from __future__ import annotations

import math

import numpy as np

from ._janela import JanelaSoma

NS = 1_000_000_000
NAN = float("nan")


class VolRealizada:
    def __init__(self, grade_s: int = 5, janelas_min: tuple[int, ...] = (1, 5, 30)):
        self.grade_ns = grade_s * NS
        self.janelas_min = janelas_min
        self._j = {m: JanelaSoma(m * 60 * NS, 1) for m in janelas_min}
        self._max_pontos = max(janelas_min) * 60 // grade_s + 1
        self._idx: int | None = None  # indice do ultimo ponto da grade processado
        self._preco_grade: float | None = None  # preco vigente no ultimo ponto da grade
        self._meio: float | None = None

    def atualizar(self, t_ns: int, meio: float) -> None:
        if meio <= 0:
            return
        idx = t_ns // self.grade_ns
        if self._idx is None or self._preco_grade is None or self._meio is None:
            self._idx, self._preco_grade, self._meio = idx, meio, meio
            return
        if idx - self._idx > self._max_pontos:  # lacuna longa: reinicia sem criar retorno artificial
            for j in self._j.values():
                j.podar(10**30)
            self._idx, self._preco_grade, self._meio = idx, meio, meio
            return
        for b in range(self._idx + 1, idx + 1):
            t_b = b * self.grade_ns
            p = meio if t_b == t_ns else self._meio  # ponto da grade usa o ultimo preco com t <= t_b
            r = math.log(p / self._preco_grade)
            if r != 0.0:
                for j in self._j.values():
                    j.adicionar(t_b, r * r)
            self._preco_grade = p
        self._idx = idx
        self._meio = meio

    def valores(self, t_ns: int) -> dict[str, float]:
        out = {}
        for m, j in self._j.items():
            out[f"rv_{m}m"] = math.sqrt(max(j.somas(t_ns)[0], 0.0))
        r1, r30 = out.get("rv_1m", 0.0), out.get("rv_30m", 0.0)
        out["rv_razao_1m_30m"] = (r1 / r30 * math.sqrt(30.0)) if r30 > 0 else NAN
        return out


def rv_diaria(t_ns: np.ndarray, meio: np.ndarray, grade_s: int = 300) -> float:
    """RV de um pregao: soma dos retornos log quadraticos em grade de `grade_s` (padrao 5 min)."""
    t_ns = np.asarray(t_ns, dtype=np.int64)
    meio = np.asarray(meio, dtype=float)
    if len(t_ns) < 2:
        return 0.0
    g = grade_s * NS
    grade = np.arange(t_ns[0], t_ns[-1] + 1, g, dtype=np.int64)
    if len(grade) < 2:
        return 0.0
    i = np.searchsorted(t_ns, grade, side="right") - 1
    p = meio[np.clip(i, 0, None)]
    r = np.diff(np.log(p))
    return float(np.sum(r * r))
