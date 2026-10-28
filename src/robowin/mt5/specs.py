"""Validacao das especificacoes do simbolo da corretora CONTRA O PERFIL do ativo (tick, valor do ponto, lote)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import Instrumento

from . import constantes as C


@dataclass(frozen=True)
class EspecEsperada:
    tick_pontos: float
    ponto_valor_brl: float
    lote: int
    tolerancia: float = 1e-9
    moeda: str = "BRL"

    @classmethod
    def do_perfil(cls, perfil: PerfilInstrumento) -> EspecEsperada:
        return cls(tick_pontos=perfil.tick_pontos, ponto_valor_brl=perfil.ponto_valor_brl, lote=perfil.lote,
                   moeda=perfil.moeda)


def valor_ponto_brl(info: Any) -> float | None:
    """R$ por ponto por unidade de volume = trade_tick_value / trade_tick_size."""
    size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    valor = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    if size <= 0 or valor <= 0:
        return None
    return valor / size


def validar_especificacoes(
    info: Any, esperado: EspecEsperada, vencimento: dt.date | None = None, moeda_conta: str | None = None
) -> list[str]:
    """Lista de divergencias (vazia = especificacoes validas). Qualquer divergencia => recusa."""
    erros: list[str] = []
    if info is None:
        return ["symbol_info indisponivel"]
    tol = esperado.tolerancia
    # moeda: o P&L e os limites de risco estao na moeda do perfil; moeda nao informada = nao confirmada = recusa
    moeda_lucro = str(getattr(info, "currency_profit", "") or "").strip().upper()
    if not moeda_lucro:
        erros.append(f"currency_profit nao informada pelo terminal: moeda {esperado.moeda} nao confirmada")
    elif moeda_lucro != esperado.moeda.upper():
        erros.append(f"currency_profit={moeda_lucro} diverge da moeda do perfil {esperado.moeda}")
    if moeda_conta is not None:
        mc = str(moeda_conta).strip().upper()
        if not mc:
            erros.append(f"moeda da conta nao informada pelo terminal: moeda {esperado.moeda} nao confirmada")
        elif mc != esperado.moeda.upper():
            erros.append(f"moeda da conta {mc} diverge da moeda do perfil {esperado.moeda} (P&L/risco seriam em outra moeda)")
    tick = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    if abs(tick - esperado.tick_pontos) > tol:
        erros.append(f"tick_size={tick} diverge do esperado {esperado.tick_pontos} pontos")
    vp = valor_ponto_brl(info)
    if vp is None:
        erros.append("trade_tick_value/trade_tick_size indisponiveis")
    elif abs(vp * esperado.lote - esperado.ponto_valor_brl) > 1e-6:  # valor do ponto POR LOTE (lote 100 em acoes)
        erros.append(f"valor do ponto R$ {vp * esperado.lote:.4f} por lote diverge do esperado R$ {esperado.ponto_valor_brl:.4f}")
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


def instrumento_de(simbolo: str, info: Any, vencimento: dt.date | None, ativo: str = "") -> Instrumento:
    """Monta o `Instrumento` a partir de um symbol_info JA validado."""
    return Instrumento(
        simbolo=simbolo, ativo=ativo,
        ponto_valor_brl=float(valor_ponto_brl(info) or 0.0) * int(info.volume_min),
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
