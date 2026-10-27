"""Auxiliares de tempo em America/Sao_Paulo (ns UTC -> horario local do pregao)."""
from __future__ import annotations

import datetime as dt

from ..core.calendario_b3 import SP


def local(t_ns: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(t_ns / 1e9, tz=SP)


def dia_sp(t_ns: int) -> dt.date:
    return local(t_ns).date()


def segundos_do_dia(t_ns: int) -> int:
    d = local(t_ns)
    return d.hour * 3600 + d.minute * 60 + d.second


def hhmm_para_s(hhmm: str) -> int:
    h, m = hhmm.split(":")[:2]
    return int(h) * 3600 + int(m) * 60


def chave_semana(d: dt.date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso.year, iso.week)
