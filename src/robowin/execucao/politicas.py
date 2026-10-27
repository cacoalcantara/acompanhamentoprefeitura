"""Politicas de execucao: quando agredir (mercado) ou ser passivo (limitada). Interface para RL."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

AGREDIR = "agredir"
PASSIVA = "passiva"
ESCOLHA_PARA_TIPO = {AGREDIR: "mercado", PASSIVA: "limitada"}


@runtime_checkable
class PoliticaExecucao(Protocol):
    """Mesma assinatura de core.protocolos.PoliticaExecucao; um agente de RL implementa `escolher`."""
    nome: str

    def escolher(self, estado: dict[str, float]) -> str: ...


class PoliticaPadrao:
    """Sempre agride: ordem a mercado (conservadora quanto a preenchimento e a latencia de decisao)."""
    nome = "agredir"

    def escolher(self, estado: dict[str, float]) -> str:
        return AGREDIR


class PoliticaPassiva:
    """Limitada na ponta propria so quando o spread permite; senao agride."""
    nome = "passiva"

    def __init__(self, spread_min_ticks: float = 2.0) -> None:
        self.spread_min_ticks = spread_min_ticks

    def escolher(self, estado: dict[str, float]) -> str:
        return PASSIVA if estado.get("spread_ticks", 0.0) >= self.spread_min_ticks else AGREDIR


def tipo_de_ordem(escolha: str) -> str:
    """Traduz a escolha da politica para ComandoOrdem.tipo; escolha desconhecida => mercado (seguro)."""
    return ESCOLHA_PARA_TIPO.get(escolha, "mercado")
