"""Braco B: proposta do proponente (Claude por padrao) SEM revisor."""
from __future__ import annotations

from ..validade import Prazos
from .base import Geometria, Modo
from .llm import BracoLLM, ServicoProposta


class BracoProponente(BracoLLM):
    """B-imediato / B-condicional: a proposta do proponente vira intencao direto (o risco e o unico veto)."""

    def __init__(self, id: str, proponente: ServicoProposta, modo: Modo = "imediato",
                 geometria: Geometria | None = None, prazos: Prazos | None = None) -> None:
        super().__init__(id, proponente, None, modo, geometria, prazos)


BracoClaude = BracoProponente
