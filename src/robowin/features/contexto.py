"""Contexto intradiario: tempo desde a abertura e sazonalidade, em codigos nao identificaveis.

Nao usa data, dia da semana nem nada que identifique um pregao especifico: so a posicao dentro do dia.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

SP = ZoneInfo("America/Sao_Paulo")
NS = 1_000_000_000


def _hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


class Contexto:
    def __init__(self, abertura: str = "09:00", fechamento: str = "18:25"):
        self.abertura_min = _hhmm(abertura)
        self.duracao_min = _hhmm(fechamento) - self.abertura_min

    def valores(self, t_ns: int) -> dict[str, float]:
        d = dt.datetime.fromtimestamp(t_ns / NS, tz=SP)
        minutos = d.hour * 60 + d.minute + d.second / 60.0 + d.microsecond / 6e7 - self.abertura_min
        fase = 2 * math.pi * minutos / self.duracao_min
        bucket = min(max(int(minutos // 30), 0), int(self.duracao_min // 30))
        return {
            "ctx_min_desde_abertura": minutos,
            "ctx_sazonal_sin": math.sin(fase),
            "ctx_sazonal_cos": math.cos(fase),
            "ctx_bucket_30m": float(bucket),
        }
