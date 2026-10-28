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
    # modelo generico (qualquer mercado): None = regra legada (lote inteiro, valor do ponto por lote)
    volume_min: float | None = None
    volume_step: float | None = None
    tick_valor_lucro: float | None = None  # por 1,0 lote, na moeda de lucro
    tamanho_contrato: float | None = None
    modo: str = ""
    aceita_conversao: bool = False  # conta em outra moeda permitida (P&L/risco convertidos pela cotacao do instante)

    @classmethod
    def do_perfil(cls, perfil: PerfilInstrumento) -> EspecEsperada:
        return cls(tick_pontos=perfil.tick_pontos, ponto_valor_brl=perfil.ponto_valor_brl, lote=perfil.lote,
                   moeda=perfil.moeda, volume_min=perfil.volume_min, volume_step=perfil.volume_step,
                   tick_valor_lucro=perfil.tick_valor_lucro, tamanho_contrato=perfil.tamanho_contrato,
                   modo=perfil.modo_calculo_efetivo, aceita_conversao=bool(perfil.moeda_lucro))


def valor_ponto_brl(info: Any) -> float | None:
    """R$ por ponto por unidade de volume = trade_tick_value / trade_tick_size."""
    size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    valor = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    if size <= 0 or valor <= 0:
        return None
    return valor / size


def _n(x: float) -> str:
    """Numero sem zeros inuteis (1 -> '1', 0.01 -> '0.01')."""
    return f"{x:.10g}"


def _conferir_valor_tick(info: Any, esperado: EspecEsperada, mesma_moeda: bool) -> list[str]:
    """Modelo generico: valor do tick por 1,0 lote na moeda de LUCRO. Com a conta na mesma moeda o terminal informa
    `trade_tick_value` nessa moeda; com a conta em outra moeda o valor vem JA convertido (varia com o cambio), entao
    confere-se pelo tamanho do contrato (tick_size x contract_size) nos modos forex/cfd."""
    erros: list[str] = []
    assert esperado.tick_valor_lucro is not None
    tv = float(esperado.tick_valor_lucro)
    rel = 1e-6 * max(1.0, abs(tv))
    cs = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
    size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    if esperado.tamanho_contrato is not None and cs > 0 and abs(cs - esperado.tamanho_contrato) > 1e-9 * max(1.0, cs):
        erros.append(f"trade_contract_size={_n(cs)} diverge do tamanho do contrato do perfil {_n(esperado.tamanho_contrato)}")
    if mesma_moeda:
        valor = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
        if valor <= 0:
            erros.append("trade_tick_value indisponivel")
        elif abs(valor - tv) > rel:
            erros.append(f"trade_tick_value={_n(valor)} por lote diverge do perfil {_n(tv)} (moeda de lucro)")
    elif esperado.modo in ("forex", "cfd"):
        if cs <= 0 or size <= 0:
            erros.append("trade_contract_size/trade_tick_size indisponiveis: valor do tick nao confirmado")
        elif abs(cs * size - tv) > rel:
            erros.append(f"tick_size x contract_size = {_n(cs * size)} diverge do valor do tick do perfil {_n(tv)}")
    # outros modos com conta em outra moeda: o terminal so informa o valor convertido; a conferencia fica na
    # confirmacao manual do perfil (a_confirmar)
    return erros


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
    mesma_moeda = True
    if moeda_conta is not None:
        mc = str(moeda_conta).strip().upper()
        if not mc:
            erros.append(f"moeda da conta nao informada pelo terminal: moeda {esperado.moeda} nao confirmada")
        elif mc != esperado.moeda.upper():
            mesma_moeda = False
            if not esperado.aceita_conversao:
                erros.append(f"moeda da conta {mc} diverge da moeda do perfil {esperado.moeda} (P&L/risco seriam em outra moeda)")
    tick = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
    if abs(tick - esperado.tick_pontos) > tol:
        erros.append(f"tick_size={tick} diverge do esperado {esperado.tick_pontos} pontos")
    vp = valor_ponto_brl(info)
    if esperado.tick_valor_lucro is not None:
        erros += _conferir_valor_tick(info, esperado, mesma_moeda)
    elif vp is None:
        erros.append("trade_tick_value/trade_tick_size indisponiveis")
    elif abs(vp * esperado.lote - esperado.ponto_valor_brl) > 1e-6:  # valor do ponto POR LOTE (lote 100 em acoes)
        erros.append(f"valor do ponto R$ {vp * esperado.lote:.4f} por lote diverge do esperado R$ {esperado.ponto_valor_brl:.4f}")
    vmin = float(getattr(info, "volume_min", 0.0) or 0.0)
    vstep = float(getattr(info, "volume_step", 0.0) or 0.0)
    emin = esperado.volume_min if esperado.volume_min is not None else float(esperado.lote)
    estep = esperado.volume_step if esperado.volume_step is not None else (
        emin if esperado.volume_min is not None else float(esperado.lote))
    if abs(vmin - emin) > tol:
        erros.append(f"volume_min={vmin} diverge do lote esperado {_n(emin)}")
    if abs(vstep - estep) > tol:
        erros.append(f"volume_step={vstep} diverge do lote esperado {_n(estep)}")
    exp = int(getattr(info, "expiration_time", 0) or 0)
    if vencimento is not None and exp > 0:
        d_exp = dt.datetime.fromtimestamp(exp, dt.UTC).date()
        if abs((d_exp - vencimento).days) > 1:
            erros.append(f"vencimento do simbolo {d_exp} diverge do calendario B3 {vencimento}")
    return erros


def instrumento_de(simbolo: str, info: Any, vencimento: dt.date | None, ativo: str = "") -> Instrumento:
    """Monta o `Instrumento` a partir de um symbol_info JA validado."""
    vmin = float(info.volume_min)
    if vmin >= 1 and abs(vmin - round(vmin)) < 1e-9:  # lote inteiro (B3): contrato legado
        return Instrumento(
            simbolo=simbolo, ativo=ativo,
            ponto_valor_brl=float(valor_ponto_brl(info) or 0.0) * int(round(vmin)),
            tick_pontos=float(info.trade_tick_size),
            lote=int(round(vmin)),
            vencimento=vencimento.isoformat() if vencimento else None,
            filling_mode=int(getattr(info, "filling_mode", 0)),
            trade_exemode=int(getattr(info, "trade_exemode", C.EXEC_EXCHANGE)),
            book_depth=int(getattr(info, "ticks_bookdepth", 0) or 0),
        )
    return instrumento_generico_de(simbolo, info, vencimento, ativo)


def instrumento_generico_de(simbolo: str, info: Any, vencimento: dt.date | None, ativo: str = "",
                            perfil: PerfilInstrumento | None = None, moeda_conta: str = "") -> Instrumento:
    """`Instrumento` de qualquer mercado (lote fracionario): a unidade do robo e o lote minimo; o valor do ponto vem
    do PERFIL (moeda de lucro) quando dado, senao de tick_size x contract_size (modos forex/cfd)."""
    vmin = float(info.volume_min)
    size = float(info.trade_tick_size)
    if perfil is not None:
        pv = perfil.ponto_valor_brl
        moeda = perfil.moeda
    else:
        cs = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
        pv = cs * vmin if cs > 0 else float(valor_ponto_brl(info) or 0.0) * vmin
        moeda = str(getattr(info, "currency_profit", "") or "BRL").upper()
    return Instrumento(
        simbolo=simbolo, ativo=ativo, moeda=moeda, moeda_conta=moeda_conta.upper(),
        ponto_valor_brl=pv,
        tick_pontos=size,
        lote=1, volume_unidade=vmin,
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
