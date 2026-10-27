"""Simulacao SOMBRA de pernas multiplas. Apenas contabilidade hipotetica: NAO ha execucao multiperna na v1."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from robowin.core.tipos import Lado, Perna


def _sinal(lado: Lado) -> int:
    return 1 if lado == Lado.COMPRA else -1


def pnl_perna(perna: Perna, preco_entrada: float, preco_saida: float, valor_ponto_brl: float) -> float:
    return _sinal(perna.lado) * (preco_saida - preco_entrada) * perna.quantidade * valor_ponto_brl


@dataclass(frozen=True)
class CestaSombra:
    pernas: tuple[Perna, ...]
    precos_entrada: Mapping[str, float]
    t_entrada_ns: int = 0

    def pnl(self, precos: Mapping[str, float], valor_ponto_brl: Mapping[str, float]) -> float:
        """PnL hipotetico (marcado a mercado) em BRL; falta de preco ou multiplicador levanta KeyError."""
        return sum(pnl_perna(p, self.precos_entrada[p.simbolo], precos[p.simbolo], valor_ponto_brl[p.simbolo])
                   for p in self.pernas)


def abrir_sombra(pernas: Sequence[Perna], precos: Mapping[str, float], t_ns: int = 0) -> CestaSombra:
    faltam = [p.simbolo for p in pernas if p.simbolo not in precos]
    if faltam:
        raise KeyError(f"sem preco para: {faltam}")
    return CestaSombra(tuple(pernas), dict(precos), t_ns)


def pernas_de_hedge(simbolo_alvo: str, simbolo_ref: str, beta: float, lado_alvo: Lado) -> tuple[Perna, Perna]:
    """Par alvo/referencia (quantidade 1 cada; a razao beta so define se a referencia e oposta ou igual)."""
    oposto = Lado.VENDA if lado_alvo == Lado.COMPRA else Lado.COMPRA
    return (Perna(simbolo=simbolo_alvo, lado=lado_alvo),
            Perna(simbolo=simbolo_ref, lado=oposto if beta >= 0 else lado_alvo))
