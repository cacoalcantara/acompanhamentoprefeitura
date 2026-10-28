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
# grade: pregao com abertura/fechamento diarios (bolsas); 24x5: forex (abre domingo, fecha sexta, no fuso da
# convencao, com rollover diario); 24x7: cripto (todos os dias). Em 24x5/24x7 `sessoes` e a JANELA de day trade
# padrao (no fuso da sessao) e o rollover encerra o "dia de negociacao" (zeragem antes dele => sem swap).
SessaoTipo = Literal["grade", "24x5", "24x7"]


class SemTaxaDeCambio(ErroConfig):
    """Conversao entre moedas pedida sem taxa fornecida: nunca se inventa taxa."""


class LimiteOscilacao(Base):
    percentual: float = Field(gt=0, lt=1)


class PerfilBolsa(Base):
    codigo: str
    nome: str
    fuso: str
    calendario: str = ""  # codigo do exchange-calendars (ex.: BVMF); vazio = sem calendario de bolsa
    moeda: str = ""  # vazio = cada instrumento declara a sua (`moeda_lucro`), ex.: forex/CFD na corretora
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
    # adicoes opcionais (mercados 24h da corretora); o padrao preserva o comportamento das bolsas
    sessao_tipo: SessaoTipo = "grade"
    abertura_semanal: str = ""  # 24x5: "domingo 17:00" (fuso da sessao)
    fechamento_semanal: str = ""  # 24x5: "sexta 17:00"
    rollover: str = ""  # "17:00": fim do dia de negociacao e instante do swap
    pausas: tuple[tuple[str, str], ...] = ()  # pausas diarias [inicio, fim) no fuso da sessao, ex.: [["16:55","17:05"]]
    dias_fechados: tuple[str, ...] = ()  # "MM-DD" fechados (ex.: Natal, Ano Novo) - a confirmar com a corretora
    entradas_antes_fechamento_min: int = Field(default=30, ge=0)  # sem entradas tao perto do fechamento/rollover
    zeragem_antes_fechamento_min: int = Field(default=15, ge=0)  # zeragem forcada antes do fechamento/rollover

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
        extras: dict[str, Any] = {}
        if "pausas" in sec:
            extras["pausas"] = tuple((str(p[0]), str(p[1])) for p in sec["pausas"])
        return PerfilBolsa(codigo=chave, **{k: v for k, v in sec.items() if k not in ("sessoes", "pausas")},
                           sessoes=tuple(SessaoPregao(**s) for s in sec.get("sessoes", [])), **extras)
    except Exception as e:
        raise ErroConfig(f"bolsa {chave} invalida: {e}") from e


def converter(valor: float, de: str, para: str, taxa: float | None = None) -> float:
    """Converte `valor` de `de` para `para` SO com a taxa fornecida (unidades de `para` por 1 `de`). A taxa vem da
    cotacao do par de conversao lida do terminal/dados NO MESMO INSTANTE (`core.moedas`); nunca de taxa fixa."""
    if de == para:
        return valor
    if taxa is None or taxa <= 0:
        raise SemTaxaDeCambio(f"conversao {de}->{para} exige taxa de cambio lida do terminal/dados no instante "
                              "(nenhuma taxa e inventada)")
    return valor * taxa
