"""Modelos de preenchimento conservadores: fila de ordem limitada e slippage de cauda pesada."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..core.tipos import Lado, LivroSnapshot, Tick


@dataclass(frozen=True)
class ParamsSlippage:
    """Slippage de stop em ticks: minimo + Pareto (cauda pesada). Base: >= 1 tick; estresse: >= 5."""
    minimo_ticks: int = 1
    alpha: float = 2.2
    escala: float = 1.0
    maximo_ticks: int = 400

    @classmethod
    def estresse(cls) -> ParamsSlippage:
        return cls(minimo_ticks=5, alpha=1.8, escala=2.0)


def slippage_pontos(rng: np.random.Generator, p: ParamsSlippage, tick_pontos: float) -> float:
    ticks = p.minimo_ticks + int(math.floor(rng.pareto(p.alpha) * p.escala))
    return min(ticks, p.maximo_ticks) * tick_pontos


def eh_negocio(t: Tick) -> bool:
    """Um tick conta como negocio se tem ultimo preco e volume (ou flag de volume)."""
    return t.last > 0 and (t.volume > 0 or bool(t.flags & 8))


def cotacao_valida(t: Tick) -> tuple[float, float]:
    """(bid, ask) usaveis; em leilao/ausencia de lado usa o ultimo."""
    bid = t.bid if t.bid > 0 else t.last
    ask = t.ask if t.ask > 0 else t.last
    return bid, ask


def volume_no_nivel(livro: LivroSnapshot | None, lado: Lado, preco: float) -> float | None:
    """Volume ja existente no nivel do preco, no mesmo lado da nossa ordem (= fila a frente)."""
    if livro is None:
        return None
    alvo = "compra" if lado == Lado.COMPRA else "venda"
    return sum(n.volume for n in livro.niveis if n.lado == alvo and abs(n.preco - preco) < 1e-9)


@dataclass
class FilaLimitada:
    """Estado de fila de uma ordem limitada passiva.

    Executa quando (a) negocio atravessa o preco (compra: last < preco; venda: last > preco) -- fila
    inteira ja consumida; ou (b) negocios NO preco acumulam volume maior que a fila estimada a frente.
    """
    lado: Lado
    preco: float
    restante: int
    fila_a_frente: float

    def processar_negocio(self, last: float, volume: float) -> int:
        """Retorna quantidade executada por este negocio (0 se nada)."""
        if self.restante <= 0:
            return 0
        atravessou = last < self.preco if self.lado == Lado.COMPRA else last > self.preco
        if atravessou:
            q, self.fila_a_frente = self.restante, 0.0
        elif abs(last - self.preco) < 1e-9:
            if volume <= self.fila_a_frente:
                self.fila_a_frente -= volume
                return 0
            sobra = volume - self.fila_a_frente
            self.fila_a_frente = 0.0
            q = min(self.restante, int(sobra))
        else:
            return 0
        self.restante -= q
        return q


def limitada_marcavel(lado: Lado, preco: float, bid: float, ask: float) -> bool:
    """Limitada que cruza o spread na chegada executa como agressora."""
    return preco >= ask if lado == Lado.COMPRA else preco <= bid
