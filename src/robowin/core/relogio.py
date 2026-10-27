"""Relogios real e simulado."""
from __future__ import annotations

import time
from typing import Protocol


class Relogio(Protocol):
    def agora_ns(self) -> int: ...
    def monotonico_ns(self) -> int: ...


class RelogioReal:
    def agora_ns(self) -> int:
        return time.time_ns()

    def monotonico_ns(self) -> int:
        return time.monotonic_ns()


class RelogioSimulado:
    def __init__(self, inicio_ns: int = 0) -> None:
        self._t = inicio_ns

    def agora_ns(self) -> int:
        return self._t

    def monotonico_ns(self) -> int:
        return self._t

    def avancar_para(self, t_ns: int) -> None:
        if t_ns < self._t:
            raise ValueError("relogio simulado nao anda para tras")
        self._t = t_ns

    def avancar(self, delta_ns: int) -> None:
        self.avancar_para(self._t + delta_ns)
