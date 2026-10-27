"""Replay cronologico por DISPONIBILIDADE (nao por tempo do evento).

Regra central do backtest: um dado so existe para o sistema no instante em que CHEGOU.
* tick: disponivel em max(t_utc_ns, t_recebido_ns). Um backfill (tick antigo recebido tarde) entra no
  instante em que chegou, nunca no seu t_evento;
* snapshot do livro: so tem t_utc_ns, que e tratado como instante de disponibilidade.

Para o pipeline de features e para o simulador de execucao, o tick entregue e uma COPIA cujo t_utc_ns e
t_recebido_ns valem o instante de disponibilidade (`tick_no_instante`), de modo que ambos andam sempre para
frente no tempo, mesmo com backfill.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from robowin.core.tipos import LivroSnapshot, Tick

PRIO_LIVRO, PRIO_TICK = 0, 1  # no mesmo instante, o livro e processado antes do tick


@dataclass(frozen=True, slots=True)
class EventoReplay:
    t_disp_ns: int
    tipo: Literal["livro", "tick"]
    t_evento_ns: int
    tick: Tick | None = None
    livro: LivroSnapshot | None = None

    @property
    def atraso_ns(self) -> int:
        return self.t_disp_ns - self.t_evento_ns


def disponivel_tick(t: Tick) -> int:
    return max(t.t_utc_ns, t.t_recebido_ns)


def disponivel_livro(lv: LivroSnapshot) -> int:
    return lv.t_utc_ns


def tick_no_instante(t: Tick, t_disp_ns: int) -> Tick:
    """Copia do tick com os dois relogios iguais ao instante em que o dado chegou."""
    if t.t_utc_ns == t_disp_ns and t.t_recebido_ns == t_disp_ns:
        return t
    return t.model_copy(update={"t_utc_ns": t_disp_ns, "t_recebido_ns": t_disp_ns})


def _montar(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot], por_disponibilidade: bool) -> list[EventoReplay]:
    ev: list[tuple[int, int, int, EventoReplay]] = []
    for i, t in enumerate(ticks):
        d = disponivel_tick(t) if por_disponibilidade else t.t_utc_ns
        ev.append((d, PRIO_TICK, i, EventoReplay(d, "tick", t.t_utc_ns, tick=t)))
    for j, lv in enumerate(livros):
        d = disponivel_livro(lv)
        ev.append((d, PRIO_LIVRO, j, EventoReplay(d, "livro", lv.t_utc_ns, livro=lv)))
    ev.sort(key=lambda e: (e[0], e[1], e[2]))
    return [e[3] for e in ev]


def ordenar_por_disponibilidade(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot] = ()) -> list[EventoReplay]:
    """Ordem de replay do backtest (estavel e deterministica)."""
    return _montar(ticks, livros, True)


def ordenar_por_evento(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot] = ()) -> list[EventoReplay]:
    """Ordem por t_evento. NAO usada pelo backtest; existe para contraste e testes."""
    return _montar(ticks, livros, False)


def backfills(eventos: Sequence[EventoReplay], tolerancia_ns: int = 1_000_000_000) -> list[EventoReplay]:
    """Eventos que chegaram com atraso maior que a tolerancia (backfill / reconexao)."""
    return [e for e in eventos if e.atraso_ns > tolerancia_ns]


class FonteEventos(Protocol):
    sintetico: bool

    def eventos(self) -> list[EventoReplay]: ...


class FonteMemoria:
    """Eventos ja em memoria (testes, perturbacao futura)."""

    def __init__(self, ticks: Sequence[Tick], livros: Sequence[LivroSnapshot] = (), sintetico: bool = False) -> None:
        self.ticks, self.livros, self.sintetico = tuple(ticks), tuple(livros), sintetico

    def eventos(self) -> list[EventoReplay]:
        return ordenar_por_disponibilidade(self.ticks, self.livros)


class FonteArmazem:
    """Le um `ArmazenamentoTicks` no intervalo [de_ns, ate_ns) (por t_utc_ns)."""

    def __init__(self, armazem: Any, simbolo: str, de_ns: int | None = None, ate_ns: int | None = None) -> None:
        self.armazem, self.simbolo = armazem, simbolo
        self.de_ns = 0 if de_ns is None else de_ns
        self.ate_ns = (1 << 62) if ate_ns is None else ate_ns
        self.sintetico = bool(getattr(armazem, "eh_sintetico", False))

    def eventos(self) -> list[EventoReplay]:
        ticks = self.armazem.ler(self.simbolo, self.de_ns, self.ate_ns)
        livros = self.armazem.ler_livros(self.simbolo, self.de_ns, self.ate_ns)
        return ordenar_por_disponibilidade(ticks, livros)
