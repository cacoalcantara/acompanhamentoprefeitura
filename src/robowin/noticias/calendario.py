"""Calendario de eventos agendados (BCB/Copom, IBGE, FOMC, BLS) com fuso de origem e janelas de bloqueio.

Cada evento tem horario LOCAL no fuso de origem; a conversao para UTC/Brasilia usa o banco tzdata, logo
o fim do horario de verao dos EUA (01/11/2026) desloca 08:30 ET de 09:30 para 10:30 de Brasilia sozinho.
Evento NAO verificado continua bloqueando (falha segura: bloquear a mais nunca e perigoso).
Durante a janela de bloqueio novas entradas sao vetadas (o motor de risco consulta ``em_bloqueio``).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from robowin.core.config import carregar_toml
from robowin.noticias.tempo import local_para_ns

ORGAOS = ("BCB", "IBGE", "FOMC", "BLS")
MIN_NS = 60 * 1_000_000_000


@dataclass(frozen=True)
class EventoAgendado:
    orgao: str
    nome: str
    data_local: date
    hora_local: time
    fuso: str
    importancia: str = "alta"
    antes_min: int = 5
    depois_min: int = 5
    verificado: bool = False

    @property
    def t_ns(self) -> int:
        return local_para_ns(self.data_local, self.hora_local, self.fuso)

    @property
    def janela_bloqueio_ns(self) -> tuple[int, int]:
        return self.t_ns - self.antes_min * MIN_NS, self.t_ns + self.depois_min * MIN_NS

    def horario_em(self, fuso: str) -> datetime:
        from zoneinfo import ZoneInfo

        return datetime.combine(self.data_local, self.hora_local, tzinfo=ZoneInfo(self.fuso)).astimezone(ZoneInfo(fuso))


class CalendarioEventos:
    def __init__(self, eventos: Iterable[EventoAgendado] = ()):
        self._eventos = sorted(eventos, key=lambda e: e.t_ns)

    def adicionar(self, ev: EventoAgendado) -> None:
        self._eventos.append(ev)
        self._eventos.sort(key=lambda e: e.t_ns)

    def __len__(self) -> int:
        return len(self._eventos)

    def todos(self) -> list[EventoAgendado]:
        return list(self._eventos)

    def em_bloqueio(self, t_ns: int) -> EventoAgendado | None:
        for e in self._eventos:
            ini, fim = e.janela_bloqueio_ns
            if ini <= t_ns <= fim:
                return e
        return None

    def proximos(self, t_ns: int, n: int = 5) -> list[EventoAgendado]:
        return [e for e in self._eventos if e.t_ns >= t_ns][:n]


def bls_emprego(ano: int, mes: int, verificado: bool = False) -> EventoAgendado:
    """Relatorio de emprego do BLS: primeira sexta-feira do mes, 08:30 ET (regra usual; confirmar no calendario do BLS)."""
    d = date(ano, mes, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return EventoAgendado("BLS", "Relatorio de emprego (payroll)", d, time(8, 30), "America/New_York",
                          verificado=verificado)


def carregar_calendario(cfg: dict[str, Any] | str | Path = "config/fontes.toml") -> CalendarioEventos:
    dados: dict[str, Any] = cfg if isinstance(cfg, dict) else carregar_toml(cfg)
    evs = []
    for c in dados.get("calendario", []):
        if c["orgao"] not in ORGAOS:
            raise ValueError(f"orgao desconhecido no calendario: {c['orgao']}")
        h, m = (int(x) for x in c["hora"].split(":"))
        evs.append(EventoAgendado(
            orgao=c["orgao"], nome=c["nome"], data_local=date.fromisoformat(c["data"]), hora_local=time(h, m),
            fuso=c["fuso"], importancia=c.get("importancia", "alta"), antes_min=int(c.get("antes_min", 5)),
            depois_min=int(c.get("depois_min", 5)), verificado=bool(c.get("verificado", False))))
    return CalendarioEventos(evs)
