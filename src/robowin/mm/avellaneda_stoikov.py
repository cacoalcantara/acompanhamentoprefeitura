"""Avellaneda-Stoikov com estoque limitado (|q| <= 1) -- experimental, SOMENTE simulacao.

Preco justo s = microprice + deriva prevista (opcional; deriva em pontos, ja calibrada/limitada por quem chama).
Preco de reserva  r = s - q * gamma * sigma^2 * tau           (q = estoque assinado; tau = horizonte em s)
Spread otimo      d = gamma * sigma^2 * tau + (2/gamma) * ln(1 + gamma/k)
Cotacoes          bid = r - d/2 ; ask = r + d/2
onde gamma = aversao ao risco (1/ponto), sigma = volatilidade em pontos/sqrt(s), k = intensidade de chegada
de ordens (decaimento exponencial da intensidade com a distancia ao meio, 1/ponto).

Restricoes de estoque: com q >= q_max nao ha bid; com q <= -q_max nao ha ask. Discretizacao no tick (5 pts):
bid arredondado para BAIXO, ask para CIMA (nunca melhora a cotacao teorica); sem cruzar o book; no maximo
`afastamento_max_ticks` do topo (cotacao longe do mercado seria isca; nao fazemos isca).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ParamsAS:
    gamma: float = 0.05
    k: float = 0.3
    tau_s: float = 60.0
    q_max: int = 1
    tick: float = 5.0
    afastamento_max_ticks: int = 2

    def __post_init__(self) -> None:
        if self.gamma <= 0 or self.k <= 0 or self.tau_s <= 0 or self.q_max < 1 or self.tick <= 0:
            raise ValueError("parametros AS invalidos")


def preco_reserva(s: float, q: int, sigma: float, p: ParamsAS) -> float:
    return s - q * p.gamma * sigma**2 * p.tau_s


def spread_otimo(sigma: float, p: ParamsAS) -> float:
    return p.gamma * sigma**2 * p.tau_s + (2.0 / p.gamma) * math.log(1.0 + p.gamma / p.k)


def cotacoes_continuas(s: float, q: int, sigma: float, p: ParamsAS) -> tuple[float | None, float | None]:
    r, d = preco_reserva(s, q, sigma, p), spread_otimo(sigma, p)
    bid = r - d / 2 if q < p.q_max else None
    ask = r + d / 2 if q > -p.q_max else None
    return bid, ask


def discretizar(bid: float | None, ask: float | None, melhor_bid: float, melhor_ask: float,
                p: ParamsAS) -> tuple[float | None, float | None]:
    """Grade do tick, sem cruzar o livro e sem afastar mais que `afastamento_max_ticks` do topo."""
    tk = p.tick
    b = a = None
    if bid is not None:
        b = math.floor(bid / tk + 1e-9) * tk
        b = min(b, melhor_ask - tk)
        b = max(b, melhor_bid - p.afastamento_max_ticks * tk)
    if ask is not None:
        a = math.ceil(ask / tk - 1e-9) * tk
        a = max(a, melhor_bid + tk)
        a = min(a, melhor_ask + p.afastamento_max_ticks * tk)
    return b, a
