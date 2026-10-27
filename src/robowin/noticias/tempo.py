"""Conversao de instantes com fuso de origem para ns UTC."""
from __future__ import annotations

from datetime import UTC, date, datetime, time
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

FUSO_BRASILIA = "America/Sao_Paulo"


def para_ns_utc(valor: str | int | float | datetime, fuso_origem: str = FUSO_BRASILIA, formato: str | None = None) -> int:
    """Converte para ns UTC.

    - datetime/str SEM fuso explicito sao interpretados em ``fuso_origem`` (nunca no fuso da maquina);
    - str RFC 822 (RSS) e ISO 8601 com offset respeitam o offset;
    - int/float: epoch em segundos (ou ms se > 1e11, ou ns se > 1e17).
    """
    if isinstance(valor, bool):
        raise TypeError("instante invalido")
    if isinstance(valor, (int, float)):
        v = float(valor)
        if v > 1e17:
            return int(v)
        if v > 1e11:
            return int(v * 1_000_000)
        return int(v * 1_000_000_000)
    if isinstance(valor, str):
        s = valor.strip()
        if formato:
            dt = datetime.strptime(s, formato)
        else:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(s)
    else:
        dt = valor
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(fuso_origem))
    return int(dt.astimezone(UTC).timestamp()) * 1_000_000_000 + dt.microsecond * 1000


def local_para_ns(dia: date, hora: time, fuso: str) -> int:
    return para_ns_utc(datetime.combine(dia, hora), fuso)


def ns_para_iso(t_ns: int, fuso: str = FUSO_BRASILIA) -> str:
    return datetime.fromtimestamp(t_ns / 1e9, tz=UTC).astimezone(ZoneInfo(fuso)).isoformat(timespec="seconds")
