"""Validacao das especificacoes do contrato WIN (tick 5 pts, R$ 0,20/ponto, lote 1)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from robowin.core.tipos import Instrumento

from . import constantes as C


@dataclass(frozen=True)
class EspecEsperada:
    tick_pontos: float = 5.0
    ponto_valor_brl: float = 0.20
    lote: int = 1
    tolerancia: float = 1e-9


ESPEC_WIN = EspecEsperada()


def valor_ponto_brl(info: Any) -> float | None:
    """R$ por ponto = trade_tick_value / trade_tick_size."""
    size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    valor = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    if size <= 0 or valor <= 0:
        return None
    return valor / size


def validar_especificacoes(
    info: Any, esperado: EspecEsperada = ESPEC_WIN, vencimento: dt.date | None = None
) -> list[str]:
    """Lista de divergencias (vazia = especificacoes validas). Qualquer divergencia => recusa."""
    erros: list[str] = []
    if info is None:
        return ["symbol_info indisponivel"]
    tol = esperado.tolerancia
    tick = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    if abs(tick - esperado.tick_pontos) > tol:
        erros.append(f"tick_size={tick} diverge do esperado {esperado.tick_pontos} pontos")
    vp = valor_ponto_brl(info)
    if vp is None:
        erros.append("trade_tick_value/trade_tick_size indisponiveis")
    elif abs(vp - esperado.ponto_valor_brl) > 1e-6:
        erros.append(f"valor do ponto R$ {vp:.4f} diverge do esperado R$ {esperado.ponto_valor_brl:.2f}")
    vmin = float(getattr(info, "volume_min", 0.0) or 0.0)
    vstep = float(getattr(info, "volume_step", 0.0) or 0.0)
    if abs(vmin - esperado.lote) > tol:
        erros.append(f"volume_min={vmin} diverge do lote esperado {esperado.lote}")
    if abs(vstep - esperado.lote) > tol:
        erros.append(f"volume_step={vstep} diverge do lote esperado {esperado.lote}")
    exp = int(getattr(info, "expiration_time", 0) or 0)
    if vencimento is not None and exp > 0:
        d_exp = dt.datetime.fromtimestamp(exp, dt.UTC).date()
        if abs((d_exp - vencimento).days) > 1:
            erros.append(f"vencimento do simbolo {d_exp} diverge do calendario B3 {vencimento}")
    return erros


def instrumento_de(simbolo: str, info: Any, vencimento: dt.date | None) -> Instrumento:
    """Monta o `Instrumento` a partir de um symbol_info JA validado."""
    return Instrumento(
        simbolo=simbolo,
        ponto_valor_brl=float(valor_ponto_brl(info) or 0.0),
        tick_pontos=float(info.trade_tick_size),
        lote=int(info.volume_min),
        vencimento=vencimento.isoformat() if vencimento else None,
        filling_mode=int(getattr(info, "filling_mode", 0)),
        trade_exemode=int(getattr(info, "trade_exemode", C.EXEC_EXCHANGE)),
        book_depth=int(getattr(info, "ticks_bookdepth", 0) or 0),
    )


def preenchimento_do_simbolo(info: Any) -> int:
    """Modo de preenchimento (ORDER_FILLING_*) lido do simbolo; nunca fixo no codigo."""
    mascara = int(getattr(info, "filling_mode", 0))
    if mascara & C.SIMB_FILL_IOC:
        return C.PREENCHE_IOC
    if mascara & C.SIMB_FILL_FOK:
        return C.PREENCHE_FOK
    return C.PREENCHE_RETURN
