"""Perfil de BOLSA (config/bolsas.toml): fuso, calendario, sessoes versionadas, moeda, tarifas e tributacao.

So a B3 vem preenchida; as demais sao modelos `a_confirmar` que BLOQUEIAM a operacao (falha segura).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from .config import ErroConfig, carregar_toml
from .tipos import Base, SessaoPregao

CAMINHO_PADRAO = Path(__file__).resolve().parents[3] / "config" / "bolsas.toml"
RegimeTributario = Literal["brasil_day_trade", "nao_calculado"]


class SemTaxaDeCambio(ErroConfig):
    """Conversao entre moedas pedida sem taxa fornecida: nunca se inventa taxa."""


class LimiteOscilacao(Base):
    percentual: float = Field(gt=0, lt=1)


class PerfilBolsa(Base):
    codigo: str
    nome: str
    fuso: str
    calendario: str  # codigo do exchange-calendars (ex.: BVMF)
    moeda: str
    modelo_tarifas: str
    regime_tributario: RegimeTributario
    sessoes: tuple[SessaoPregao, ...] = Field(min_length=1)
    limite_oscilacao: LimiteOscilacao | None = None
    a_confirmar: bool = True
    a_confirmar_itens: tuple[str, ...] = ()
    fonte: str = ""  # proveniencia opcional (`robowin perfil confirmar bolsa:<codigo>`)
    fonte_data: str = ""
    confirmado_por: str = ""
    confirmado_em: str = ""

    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.fuso)

    def sessao_em(self, d: dt.date) -> SessaoPregao:
        vig = [s for s in self.sessoes if s.vigente_desde <= d.isoformat()]
        if not vig:
            raise ValueError(f"sem grade de sessao da bolsa {self.codigo} para {d}")
        return max(vig, key=lambda s: s.vigente_desde)

    @property
    def calcula_tributo(self) -> bool:
        return self.regime_tributario == "brasil_day_trade"


def bolsas_disponiveis(caminho: str | Path | None = None) -> list[str]:
    return sorted(carregar_toml(caminho or CAMINHO_PADRAO))


def carregar_bolsa(codigo: str, caminho: str | Path | None = None) -> PerfilBolsa:
    c = Path(caminho) if caminho else CAMINHO_PADRAO
    if not c.is_file():
        raise ErroConfig(f"arquivo de bolsas nao encontrado: {c}")
    bruto: dict[str, Any] = carregar_toml(c)
    chave = codigo.strip().upper()
    sec = bruto.get(chave)
    if not isinstance(sec, dict):
        raise ErroConfig(f"bolsa '{codigo}' sem perfil em bolsas.toml (ha: {', '.join(sorted(bruto))})")
    try:
        return PerfilBolsa(codigo=chave, **{k: v for k, v in sec.items() if k != "sessoes"},
                           sessoes=tuple(SessaoPregao(**s) for s in sec.get("sessoes", [])))
    except Exception as e:
        raise ErroConfig(f"bolsa {chave} invalida: {e}") from e


def converter(valor: float, de: str, para: str, taxa: float | None = None) -> float:
    """Converte `valor` de `de` para `para` SO com a taxa fornecida (unidades de `para` por 1 `de`)."""
    if de == para:
        return valor
    if taxa is None or taxa <= 0:
        raise SemTaxaDeCambio(f"conversao {de}->{para} exige taxa de cambio fornecida pelo usuario")
    return valor * taxa
