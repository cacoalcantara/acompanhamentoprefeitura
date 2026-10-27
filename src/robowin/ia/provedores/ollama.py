"""Adaptador Ollama (extra opcional ``ollama``; API nativa com ``format`` = JSON Schema).

Modelos locais: custo 0 no catalogo; a versao fixa e o digest do modelo (``digest``).
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from robowin.ia.provedores.base import Medidor, ProvedorBase, RespostaBruta, exigir_pacote


class ProvedorOllama(ProvedorBase):
    nome = "ollama"

    def __init__(self, *, host: str | None = None, cliente: Any = None, **kw: Any):
        super().__init__(**kw)
        self._host = host or os.environ.get("OLLAMA_HOST")
        self._cliente = cliente

    def _get_cliente(self) -> Any:
        if self._cliente is None:
            sdk = exigir_pacote("ollama", "ollama")
            self._cliente = sdk.AsyncClient(host=self._host) if self._host else sdk.AsyncClient()
        return self._cliente

    async def digest(self, modelo: str) -> str:
        """Digest do modelo local (entra no manifesto do experimento)."""
        info = await self._get_cliente().show(modelo)
        return str(getattr(info, "digest", None) or (info.get("digest", "") if isinstance(info, dict) else ""))

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        fluxo = await self._get_cliente().chat(
            model=modelo, stream=True, format=schema.model_json_schema(),
            messages=[{"role": "system", "content": sistema}, {"role": "user", "content": mensagem}],
            options={"num_predict": max_tokens},
        )
        partes: list[str] = []
        servido = fim = ""
        entrada = saida = 0
        async for ch in fluxo:
            msg = getattr(ch, "message", None)
            conteudo = getattr(msg, "content", None)
            if conteudo:
                medidor.primeiro_token()
                partes.append(conteudo)
            servido = getattr(ch, "model", None) or servido
            if getattr(ch, "done", False):
                fim = getattr(ch, "done_reason", None) or "stop"
                entrada = int(getattr(ch, "prompt_eval_count", 0) or 0)
                saida = int(getattr(ch, "eval_count", 0) or 0)
        return RespostaBruta(
            texto="".join(partes), modelo_servido=str(servido),
            stop_reason="max_tokens" if fim == "length" else (fim or "end_turn"),
            tokens_entrada=entrada, tokens_saida=saida,
        )
