"""Contabilidade diaria/semanal por braco: P&L, drawdown, operacoes, perdas seguidas, pausa."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from .tempo import chave_semana, dia_sp


@dataclass
class EstadoBraco:
    braco: str
    dia: dt.date | None = None
    semana: tuple[int, int] | None = None
    pnl_dia: float = 0.0
    pnl_semana: float = 0.0
    pnl_aberto: float = 0.0
    equity_realizado: float = 0.0
    pico: float = 0.0
    operacoes_dia: int = 0
    perdas_seguidas: int = 0
    ultima_perda_ns: int | None = None
    ultimo_tamanho: int = 0
    ultima_foi_perda: bool = False

    @property
    def equity(self) -> float:
        return self.equity_realizado + self.pnl_aberto

    @property
    def drawdown(self) -> float:
        return max(0.0, max(self.pico, self.equity) - self.equity)

    @property
    def pnl_dia_total(self) -> float:
        return self.pnl_dia + self.pnl_aberto


class ContabilidadeSessao:
    """Valores em dinheiro NA MOEDA DA CONTA (quem registra converte). `dia_de` define o dia de negociacao
    (padrao: data em Sao Paulo; mercados 24h usam o dia da sessao do instrumento, que termina no rollover)."""

    def __init__(self, dia_de: Callable[[int], dt.date] = dia_sp) -> None:
        self._b: dict[str, EstadoBraco] = {}
        self._dia_de = dia_de

    def estado(self, braco: str, t_ns: int) -> EstadoBraco:
        e = self._b.setdefault(braco, EstadoBraco(braco=braco))
        d = self._dia_de(t_ns)
        if e.dia != d:
            e.dia = d
            e.pnl_dia = 0.0
            e.operacoes_dia = 0
            e.perdas_seguidas = 0
            e.ultima_perda_ns = None
        sem = chave_semana(d)
        if e.semana != sem:
            e.semana = sem
            e.pnl_semana = 0.0
        return e

    def registrar_abertura(self, braco: str, t_ns: int) -> None:
        self.estado(braco, t_ns).operacoes_dia += 1

    def registrar_trade(self, braco: str, liquido_brl: float, quantidade: int, t_ns: int) -> EstadoBraco:
        e = self.estado(braco, t_ns)
        e.pnl_dia += liquido_brl
        e.pnl_semana += liquido_brl
        e.equity_realizado += liquido_brl
        e.pnl_aberto = 0.0
        e.pico = max(e.pico, e.equity)
        e.ultimo_tamanho = quantidade
        if liquido_brl < 0:
            e.perdas_seguidas += 1
            e.ultima_perda_ns = t_ns
            e.ultima_foi_perda = True
        else:
            e.perdas_seguidas = 0
            e.ultima_foi_perda = False
        return e

    def marcar_a_mercado(self, braco: str, pnl_aberto_brl: float, t_ns: int) -> EstadoBraco:
        e = self.estado(braco, t_ns)
        e.pnl_aberto = pnl_aberto_brl
        e.pico = max(e.pico, e.equity)
        return e

    def bracos(self) -> list[str]:
        return list(self._b)
