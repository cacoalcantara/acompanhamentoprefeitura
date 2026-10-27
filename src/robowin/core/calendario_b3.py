"""Calendario B3: feriados via exchange-calendars (BVMF) + grade propria do WIN versionada."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

from .tipos import SessaoPregao

SP = ZoneInfo("America/Sao_Paulo")
LETRAS = {2: "G", 4: "J", 6: "M", 8: "Q", 10: "V", 12: "Z"}  # meses pares (convencao B3)

# Grade do WIN por data de vigencia (fonte: noticias de out/2026; confirmar com o oficio da B3).
GRADE_WIN = [
    SessaoPregao(vigente_desde="2026-03-09", abertura="09:00", fechamento="18:25", entradas_ate="17:30", zeragem="17:45"),
    SessaoPregao(vigente_desde="2026-11-03", abertura="09:00", fechamento="18:25", entradas_ate="17:30", zeragem="17:45"),
]

_cal = None


def _calendario():
    global _cal
    if _cal is None:
        import exchange_calendars as xc
        _cal = xc.get_calendar("BVMF")
    return _cal


def eh_pregao(d: dt.date) -> bool:
    cal = _calendario()
    try:
        return bool(cal.is_session(pd.Timestamp(d)))
    except Exception:  # fora do intervalo do calendario
        return d.weekday() < 5


def proximo_pregao(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not eh_pregao(d):
        d += dt.timedelta(days=1)
    return d


def sessao_em(d: dt.date) -> SessaoPregao:
    vig = [s for s in GRADE_WIN if s.vigente_desde <= d.isoformat()]
    if not vig:
        raise ValueError(f"sem grade de sessao para {d}")
    return max(vig, key=lambda s: s.vigente_desde)


def vencimento_win(ano: int, mes: int) -> dt.date:
    """Quarta-feira mais proxima do dia 15; se nao for pregao, a sessao seguinte."""
    ref = dt.date(ano, mes, 15)
    quartas = [ref + dt.timedelta(days=k) for k in range(-3, 4) if (ref + dt.timedelta(days=k)).weekday() == 2]
    q = quartas[0]
    return q if eh_pregao(q) else proximo_pregao(q)


def contrato_vigente(hoje: dt.date, ajustar_dias: int = 0) -> str:
    """Codigo WIN+letra+AA do vencimento mais proximo ainda nao vencido (vence na propria data = ainda vigente)."""
    ano, mes = hoje.year, hoje.month
    for _ in range(14):
        if mes % 2 == 0 and vencimento_win(ano, mes) >= hoje:
            return f"WIN{LETRAS[mes]}{ano % 100:02d}"
        mes += 1
        if mes > 12:
            mes, ano = 1, ano + 1
    raise RuntimeError("nao encontrou contrato vigente")


def dt_utc_ns(d: dt.datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=SP)
    return int(d.timestamp() * 1e9)
