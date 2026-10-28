"""Calendario B3: feriados via exchange-calendars (BVMF), grade de sessao e vencimentos por perfil de instrumento."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd

from .instrumentos import LETRAS_MES, PerfilInstrumento, sessoes_padrao
from .tipos import SessaoPregao

SP = ZoneInfo("America/Sao_Paulo")

CAL_B3 = "BVMF"  # codigo do exchange-calendars da B3 (o de qualquer bolsa vem de config/bolsas.toml)
_cals: dict[str, object] = {}


def _calendario(codigo: str = CAL_B3):
    if codigo not in _cals:
        import exchange_calendars as xc
        _cals[codigo] = xc.get_calendar(codigo)
    return _cals[codigo]


def eh_pregao(d: dt.date, calendario: str = CAL_B3) -> bool:
    cal = _calendario(calendario)
    try:
        return bool(cal.is_session(pd.Timestamp(d)))  # type: ignore[attr-defined]
    except Exception:  # fora do intervalo do calendario
        return d.weekday() < 5


def proximo_pregao(d: dt.date, calendario: str = CAL_B3) -> dt.date:
    d += dt.timedelta(days=1)
    while not eh_pregao(d, calendario):
        d += dt.timedelta(days=1)
    return d


def sessao_em(d: dt.date, perfil: PerfilInstrumento | None = None) -> SessaoPregao:
    """Grade vigente em `d`: a do perfil (se declarar) ou a grade padrao de instrumentos.toml."""
    grade = (perfil.sessoes if perfil is not None else ()) or sessoes_padrao()
    vig = [s for s in grade if s.vigente_desde <= d.isoformat()]
    if not vig:
        raise ValueError(f"sem grade de sessao para {d}")
    return max(vig, key=lambda s: s.vigente_desde)


def _quarta_proxima_dia15(ano: int, mes: int, cal: str = CAL_B3) -> dt.date:
    """Quarta-feira mais proxima do dia 15; se nao for pregao, a sessao seguinte."""
    ref = dt.date(ano, mes, 15)
    quartas = [ref + dt.timedelta(days=k) for k in range(-3, 4) if (ref + dt.timedelta(days=k)).weekday() == 2]
    q = quartas[0]
    return q if eh_pregao(q, cal) else proximo_pregao(q, cal)


def _primeiro_dia_util(ano: int, mes: int, cal: str = CAL_B3) -> dt.date:
    d = dt.date(ano, mes, 1)
    return d if eh_pregao(d, cal) else proximo_pregao(d, cal)


def vencimento_contrato(perfil: PerfilInstrumento, ano: int, mes: int) -> dt.date | None:
    """Vencimento do contrato do mes (`None` se o ativo nao vence ou nao ha contrato neste mes)."""
    cal = perfil.bolsa_perfil().calendario
    if perfil.vencimento == "quarta_dia15_meses_pares":
        return _quarta_proxima_dia15(ano, mes, cal) if mes % 2 == 0 else None
    if perfil.vencimento == "primeiro_dia_util_mes":  # [a confirmar] regra do WDO/DOL
        return _primeiro_dia_util(ano, mes, cal)
    return None


def codigo_contrato(perfil: PerfilInstrumento, ano: int, mes: int) -> str:
    return f"{perfil.simbolo_base}{LETRAS_MES[mes - 1]}{ano % 100:02d}"


def contrato_vigente(perfil: PerfilInstrumento, hoje: dt.date) -> str:
    """Codigo do contrato vigente em `hoje` (vence na propria data = ainda vigente). Acao/ETF: o proprio ticker."""
    if not perfil.tem_vencimento:
        return perfil.simbolo_base
    ano, mes = hoje.year, hoje.month
    for _ in range(14):
        v = vencimento_contrato(perfil, ano, mes)
        if v is not None and v >= hoje:
            return codigo_contrato(perfil, ano, mes)
        mes += 1
        if mes > 12:
            mes, ano = 1, ano + 1
    raise RuntimeError("nao encontrou contrato vigente")


def vencimento_do_codigo(perfil: PerfilInstrumento, codigo: str) -> dt.date | None:
    if not perfil.tem_vencimento:
        return None
    n = len(perfil.simbolo_base)
    return vencimento_contrato(perfil, 2000 + int(codigo[n + 1:]), LETRAS_MES.index(codigo[n]) + 1)


def dt_utc_ns(d: dt.datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=SP)
    return int(d.timestamp() * 1e9)
