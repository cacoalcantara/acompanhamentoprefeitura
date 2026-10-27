"""Custos de execucao em tabela versionada por vigencia (tarifas B3, corretagem, taxa de zeragem)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..core.tipos import Base


class TarifaB3(Base):
    vigente_desde: str  # AAAA-MM-DD
    tarifa_lado_brl: float  # emolumentos + liquidacao por contrato e por lado
    taxa_zeragem_brl: float = 0.0  # cobrada pela corretora na zeragem forcada (por contrato)
    fonte: str = ""


# [a confirmar] valores do plano (secao 8): ~R$ 0,25/lado em day trade. Novas vigencias entram AQUI,
# sem apagar as antigas, para o backtest reproduzir os custos da epoca.
TABELA_TARIFAS: tuple[TarifaB3, ...] = (
    TarifaB3(vigente_desde="2025-01-01", tarifa_lado_brl=0.25, taxa_zeragem_brl=0.0,
             fonte="premissa do plano v1 (secao 8) - V4.8 a confirmar"),
    TarifaB3(vigente_desde="2026-11-03", tarifa_lado_brl=0.25, taxa_zeragem_brl=0.0,
             fonte="premissa do plano v1 (nova grade); confirmar com o oficio da B3"),
)

CORRETAGEM_BASE_IDA_VOLTA_BRL = 0.0
CORRETAGEM_ESTRESSE_IDA_VOLTA_BRL = 0.22


def tarifa_em(d: dt.date, tabela: tuple[TarifaB3, ...] = TABELA_TARIFAS) -> TarifaB3:
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
    tabela: tuple[TarifaB3, ...] = TABELA_TARIFAS
    corretagem_ida_volta_brl: float = CORRETAGEM_BASE_IDA_VOLTA_BRL

    @classmethod
    def estresse(cls) -> Custos:
        return cls(corretagem_ida_volta_brl=CORRETAGEM_ESTRESSE_IDA_VOLTA_BRL)

    def lado(self, d: dt.date, quantidade: int, zeragem: bool = False) -> CustoLado:
        t = tarifa_em(d, self.tabela)
        tarifas = t.tarifa_lado_brl * quantidade + (t.taxa_zeragem_brl * quantidade if zeragem else 0.0)
        return CustoLado(tarifas_brl=tarifas, corretagem_brl=self.corretagem_ida_volta_brl / 2 * quantidade)

    def ida_e_volta(self, d: dt.date, quantidade: int, zeragem: bool = False) -> float:
        return self.lado(d, quantidade).total + self.lado(d, quantidade, zeragem).total
