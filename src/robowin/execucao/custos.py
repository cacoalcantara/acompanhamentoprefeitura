"""Custos de execucao em tabela versionada por vigencia (tarifas B3, corretagem, taxa de zeragem) e custos genericos
de qualquer mercado: comissao por lote (forex/CFD, a confirmar com a corretora), swap overnight (so se a posicao
atravessar o rollover) e spread (ja embutido no preco: entrada no ask/bid do livro/tick). Forex/CFD nao pagam tarifa
de bolsa. Valores na MOEDA DE LUCRO do instrumento; quem registra na conta converte pelo fator do instante."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..core.instrumentos import PerfilInstrumento, TarifaB3, perfil_de_instrumento
from ..core.tipos import Instrumento, Lado

__all__ = ["TarifaB3", "Custos", "CustoLado", "ParamsSwap", "tarifa_em"]

# A tabela de tarifas B3 (versionada por vigencia) mora em config/instrumentos.toml, por ativo: novas vigencias
# entram LA, sem apagar as antigas, para o backtest reproduzir os custos da epoca.
CORRETAGEM_BASE_IDA_VOLTA_BRL = 0.0
CORRETAGEM_ESTRESSE_IDA_VOLTA_BRL = 0.22


def tarifa_em(d: dt.date, tabela: tuple[TarifaB3, ...]) -> TarifaB3:
    vig = [t for t in tabela if t.vigente_desde <= d.isoformat()]
    if not vig:
        raise ValueError(f"sem tarifa B3 vigente em {d}")
    return max(vig, key=lambda t: t.vigente_desde)


@dataclass(frozen=True)
class CustoLado:
    tarifas_brl: float
    corretagem_brl: float

    @property
    def total(self) -> float:
        return self.tarifas_brl + self.corretagem_brl


@dataclass(frozen=True)
class ParamsSwap:
    """Swap do perfil por 1,0 lote e por rollover (convencao do MT: positivo = credito ao cliente)."""
    long: float | None
    short: float | None
    modo: str  # "pontos" | "moeda_lucro" | "moeda_conta" | "" (nao informado)
    dia_triplo: str
    valor_ponto_lote: float  # moeda de lucro por 1 `ponto` do terminal por 1,0 lote

    @classmethod
    def do_perfil(cls, perfil: PerfilInstrumento) -> ParamsSwap | None:
        if perfil.swap_long is None and perfil.swap_short is None:
            return None
        ponto = perfil.ponto or perfil.tick_pontos
        return cls(perfil.swap_long, perfil.swap_short, perfil.swap_modo, perfil.swap_dia_triplo,
                   perfil.tick_valor_por_lote() / perfil.tick_pontos * ponto)

    def por_lote(self, lado: Lado) -> float | None:
        """Credito (+) ou debito (-) por 1,0 lote e por noite, NA MOEDA DE LUCRO; None = nao calculavel."""
        v = self.long if lado == Lado.COMPRA else self.short
        if v is None:
            return None
        if self.modo == "pontos":
            return v * self.valor_ponto_lote
        if self.modo == "moeda_lucro":
            return v
        return None  # moeda_conta/outros: nao convertivel aqui (a_confirmar)


@dataclass(frozen=True)
class Custos:
    tabela: tuple[TarifaB3, ...]
    corretagem_ida_volta_brl: float = CORRETAGEM_BASE_IDA_VOLTA_BRL
    comissao_lote_lado: float = 0.0  # por 1,0 lote e por lado (forex/CFD), moeda de lucro
    volume_unidade: float = 1.0  # lotes do terminal por unidade do robo (a comissao e por LOTE)
    swap: ParamsSwap | None = None

    @classmethod
    def do_perfil(cls, perfil: PerfilInstrumento, corretagem_ida_volta_brl: float = CORRETAGEM_BASE_IDA_VOLTA_BRL) -> Custos:
        return cls(tabela=perfil.tarifas, corretagem_ida_volta_brl=corretagem_ida_volta_brl,
                   comissao_lote_lado=float(perfil.comissao_lote_lado or 0.0), volume_unidade=perfil.volume_unidade,
                   swap=ParamsSwap.do_perfil(perfil))

    @classmethod
    def do_instrumento(cls, ins: Instrumento) -> Custos:
        return cls.do_perfil(perfil_de_instrumento(ins))

    def estresse(self) -> Custos:
        return Custos(tabela=self.tabela, corretagem_ida_volta_brl=CORRETAGEM_ESTRESSE_IDA_VOLTA_BRL,
                      comissao_lote_lado=self.comissao_lote_lado, volume_unidade=self.volume_unidade, swap=self.swap)

    def lado(self, d: dt.date, quantidade: int, zeragem: bool = False) -> CustoLado:
        if self.tabela:
            t = tarifa_em(d, self.tabela)
            tarifas = t.tarifa_lado_brl * quantidade + (t.taxa_zeragem_brl * quantidade if zeragem else 0.0)
        else:  # forex/CFD na corretora: sem tarifa de bolsa
            tarifas = 0.0
        comissao = self.comissao_lote_lado * quantidade * self.volume_unidade
        return CustoLado(tarifas_brl=tarifas, corretagem_brl=self.corretagem_ida_volta_brl / 2 * quantidade + comissao)

    def ida_e_volta(self, d: dt.date, quantidade: int, zeragem: bool = False) -> float:
        return self.lado(d, quantidade).total + self.lado(d, quantidade, zeragem).total

    def swap_custo(self, lado: Lado, quantidade: int, noites: int) -> float | None:
        """Swap PAGO (positivo; recebido = negativo) por `noites` rollovers (ja com o triplo), na moeda de lucro.
        0 se nenhuma noite; None se a posicao atravessou o rollover e o swap do perfil nao e calculavel."""
        if noites <= 0:
            return 0.0
        if self.swap is None:
            return None
        v = self.swap.por_lote(lado)
        if v is None:
            return None
        return -v * quantidade * self.volume_unidade * noites
