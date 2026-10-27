"""Braco C: a MESMA proposta do B, revisada pelo revisor (GPT por padrao) - so para ENTRADAS.

ENCERRAR, MANTER e NAO_OPERAR nunca esperam revisao. Revisao rejeitada, tardia ou invalida => nenhuma
entrada ("divergencia"). Cada par proponente/revisor e um braco; outros pares entram por ``[[bracos]]``.
"""
from __future__ import annotations

from ..validade import Prazos
from .base import Geometria, Modo
from .llm import BracoLLM, ServicoProposta, ServicoRevisao


class BracoProponenteRevisor(BracoLLM):
    def __init__(self, id: str, proponente: ServicoProposta, revisor: ServicoRevisao, modo: Modo = "imediato",
                 geometria: Geometria | None = None, prazos: Prazos | None = None) -> None:
        super().__init__(id, proponente, revisor, modo, geometria, prazos)


BracoClaudeGPT = BracoProponenteRevisor
