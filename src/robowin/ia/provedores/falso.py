"""Provedor FALSO: para testes e demonstracoes. Rotulado: todo modelo servido termina em ``-FALSO``.

Nunca fala com a rede. Aceita um ``roteiro`` (lista de respostas/comportamentos) para simular falhas.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from robowin.core.tipos import Acao, ClassificacaoNoticia, PropostaIA, RevisaoIA
from robowin.ia.provedores.base import Medidor, ProvedorBase, RespostaBruta

ROTULO = "FALSO"


def resposta_padrao(schema: type[BaseModel]) -> BaseModel:
    if schema is PropostaIA:
        return PropostaIA(acao=Acao.NAO_OPERAR, incerteza=0.9, justificativa_curta="[FALSO] sem sinal")
    if schema is RevisaoIA:
        return RevisaoIA(veredito="REJEITAR", justificativa_curta="[FALSO] revisor de demonstracao")
    if schema is ClassificacaoNoticia:
        return ClassificacaoNoticia(relevancia=0.1, direcao="neutra", intensidade=0.0, tipo="falso")
    return schema.model_construct()


class ProvedorFalso(ProvedorBase):
    nome = "falso"
    rotulo = ROTULO

    def __init__(
        self, *, roteiro: list[Any] | None = None, atraso_s: float = 0.0,
        gerador: Callable[[type[BaseModel]], BaseModel] | None = None, **kw: Any,
    ):
        """``roteiro``: itens consumidos em ordem (o ultimo se repete). Cada item pode ser um BaseModel,
        uma ``str`` (texto bruto, p.ex. JSON invalido), uma ``RespostaBruta``, ou uma ``Exception``
        (levantada)."""
        super().__init__(**kw)
        self._roteiro = list(roteiro or [])
        self._i = 0
        self._atraso = atraso_s
        self._gerador = gerador or resposta_padrao
        self.chamadas = 0

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        self.chamadas += 1
        if self._atraso:
            await asyncio.sleep(self._atraso)
        medidor.primeiro_token()
        item: Any = None
        if self._roteiro:
            item = self._roteiro[min(self._i, len(self._roteiro) - 1)]
            self._i += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, RespostaBruta):
            return item
        servido = f"{modelo}-{ROTULO}"
        if isinstance(item, str):
            return RespostaBruta(texto=item, modelo_servido=servido, stop_reason="end_turn",
                                 request_id=f"falso-{self.chamadas}", tokens_entrada=10, tokens_saida=5)
        obj = item if isinstance(item, BaseModel) else self._gerador(schema)
        return RespostaBruta(
            texto=obj.model_dump_json(), objeto=obj, modelo_servido=servido, stop_reason="end_turn",
            request_id=f"falso-{self.chamadas}", tokens_entrada=1000, tokens_saida=200,
        )
