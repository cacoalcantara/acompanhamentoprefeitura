"""Custos de execucao em tabela versionada por vigencia (tarifas B3, corretagem, taxa de zeragem)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..core.instrumentos import PerfilInstrumento, TarifaB3, perfil_de_instrumento
from ..core.tipos import Instrumento

__all__ = ["TarifaB3", "Custos", "CustoLado", "tarifa_em"]

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
class Custos:
    tabela: tuple[TarifaB3, ...]
    corretagem_ida_volta_brl: float = CORRETAGEM_BASE_IDA_VOLTA_BRL

    @classmethod
    def do_perfil(cls, perfil: PerfilInstrumento, corretagem_ida_volta_brl: float = CORRETAGEM_BASE_IDA_VOLTA_BRL) -> Custos:
        return cls(tabela=perfil.tarifas, corretagem_ida_volta_brl=corretagem_ida_volta_brl)

    @classmethod
    def do_instrumento(cls, ins: Instrumento) -> Custos:
        return cls.do_perfil(perfil_de_instrumento(ins))

    def estresse(self) -> Custos:
        return Custos(tabela=self.tabela, corretagem_ida_volta_brl=CORRETAGEM_ESTRESSE_IDA_VOLTA_BRL)

    def lado(self, d: dt.date, quantidade: int, zeragem: bool = False) -> CustoLado:
        t = tarifa_em(d, self.tabela)
        tarifas = t.tarifa_lado_brl * quantidade + (t.taxa_zeragem_brl * quantidade if zeragem else 0.0)
        return CustoLado(tarifas_brl=tarifas, corretagem_brl=self.corretagem_ida_volta_brl / 2 * quantidade)

    def ida_e_volta(self, d: dt.date, quantidade: int, zeragem: bool = False) -> float:
        return self.lado(d, quantidade).total + self.lado(d, quantidade, zeragem).total
