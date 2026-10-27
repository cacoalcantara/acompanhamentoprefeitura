"""Classificador de noticias via papel ``classificador_nlp`` (qualquer ProvedorLLM). Implementa ClassificadorNoticias.

Falha segura: resposta invalida, recusa, timeout ou orcamento esgotado => classificacao neutra sem relevancia
e confianca 0 (nunca inventa direcao). A ultima ``ChamadaLLM`` fica em ``ultima_chamada`` e e entregue a ``ao_chamar``
(auditoria/orcamento).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable
from typing import Any

from robowin.core.tipos import ChamadaLLM, ClassificacaoNoticia, EventoNoticia
from robowin.ia.prompts import carregar_prompt, mensagem_nlp

NEUTRA = ClassificacaoNoticia(relevancia=0.0, direcao="neutra", intensidade=0.0, horizonte_min=0,
                              tipo="falha", confianca=0.0)


class ClassificadorLLM:
    def __init__(self, provedor: Any, modelo: str, *, prazo_s: float = 10.0, effort: str | None = None,
                 max_tokens: int = 400, prompt: str | None = None,
                 ao_chamar: Callable[[EventoNoticia, ChamadaLLM], None] | None = None):
        """``modelo``: id do modelo no provedor (sem o prefixo 'provedor:'). Por papel: ver ``ia.papeis.resolver``."""
        self.provedor, self.modelo = provedor, modelo
        self.prazo_s, self.effort, self.max_tokens = prazo_s, effort, max_tokens
        self.prompt = prompt if prompt is not None else carregar_prompt("classificador_nlp").texto
        self.ao_chamar = ao_chamar
        self.ultima_chamada: ChamadaLLM | None = None
        self._cache: dict[str, ClassificacaoNoticia] = {}

    async def aclassificar(self, ev: EventoNoticia) -> ClassificacaoNoticia:
        if ev.id in self._cache:
            return self._cache[ev.id]
        obj, ch = await self.provedor.gerar_estruturado(
            modelo=self.modelo, sistema=self.prompt, mensagem=mensagem_nlp(ev.titulo), schema=ClassificacaoNoticia,
            prazo_s=self.prazo_s, effort=self.effort, max_tokens=self.max_tokens)
        ch = ch.model_copy(update={"papel": "classificador_nlp"})
        self.ultima_chamada = ch
        if self.ao_chamar:
            self.ao_chamar(ev, ch)
        res = obj if (ch.valida and isinstance(obj, ClassificacaoNoticia)) else NEUTRA
        if res is not NEUTRA:
            self._cache[ev.id] = res
        return res

    def classificar(self, ev: EventoNoticia) -> ClassificacaoNoticia:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aclassificar(ev))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:  # chamado de dentro de um loop
            return ex.submit(asyncio.run, self.aclassificar(ev)).result()
