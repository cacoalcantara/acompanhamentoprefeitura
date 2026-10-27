"""Adaptador para qualquer servidor com API OpenAI Chat Completions (vLLM, LM Studio, terceiros).

``response_format`` json_schema so quando o catalogo declara ``saida_estruturada == 'json_schema'``;
senao, JSON simples (ou pedido no prompt) + validacao Pydantic. URL base obrigatoria.
"""
from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel

from robowin.ia.provedores.base import (
    ErroProvedor,
    Medidor,
    ProvedorBase,
    RespostaBruta,
    exigir_pacote,
)
from robowin.ia.segredos import registrar_segredo


class ProvedorOpenAICompativel(ProvedorBase):
    nome = "openai_compativel"

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, cliente: Any = None, **kw: Any):
        super().__init__(**kw)
        self._base_url = base_url or os.environ.get("OPENAI_COMPATIVEL_BASE_URL")
        self._api_key = api_key or os.environ.get("OPENAI_COMPATIVEL_API_KEY") or "sem-chave"
        registrar_segredo(self._api_key)
        self._cliente = cliente

    def _get_cliente(self) -> Any:
        if self._cliente is None:
            if not self._base_url:
                raise ErroProvedor("openai_compativel exige base_url (ou OPENAI_COMPATIVEL_BASE_URL)")
            sdk = exigir_pacote("openai", "")
            self._cliente = sdk.AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, max_retries=0)
        return self._cliente

    def _modo_saida(self, modelo: str) -> str:
        if self._catalogo is not None:
            try:
                return str(self._catalogo.obter(f"{self.nome}:{modelo}").saida_estruturada)
            except Exception:
                return "json"
        return "json"

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        modo = self._modo_saida(modelo)
        sis = sistema
        kw: dict[str, Any] = {
            "model": modelo, "max_tokens": max_tokens, "stream": True,
            "stream_options": {"include_usage": True},
        }
        if modo == "json_schema":
            kw["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "schema": schema.model_json_schema(), "strict": False}}
        else:
            kw["response_format"] = {"type": "json_object"}
            sis += "\n\nResponda SOMENTE com um objeto JSON que siga este schema:\n" + json.dumps(
                schema.model_json_schema(), ensure_ascii=False)
        kw["messages"] = [{"role": "system", "content": sis}, {"role": "user", "content": mensagem}]
        if effort:
            kw["reasoning_effort"] = effort
        fluxo = await self._get_cliente().chat.completions.create(**kw)
        partes: list[str] = []
        servido = rid = stop = ""
        recusa = False
        usage: Any = None
        async for ch in fluxo:
            servido = getattr(ch, "model", "") or servido
            rid = getattr(ch, "id", "") or rid
            if getattr(ch, "usage", None) is not None:
                usage = ch.usage
            for esc in getattr(ch, "choices", None) or []:
                delta = getattr(esc, "delta", None)
                if getattr(delta, "refusal", None):
                    recusa = True
                if getattr(delta, "content", None):
                    medidor.primeiro_token()
                    partes.append(delta.content)
                stop = getattr(esc, "finish_reason", None) or stop
        det = getattr(usage, "prompt_tokens_details", None)
        cache = int(getattr(det, "cached_tokens", 0) or 0)
        entrada = int(getattr(usage, "prompt_tokens", 0) or 0)
        return RespostaBruta(
            texto="".join(partes), modelo_servido=str(servido), request_id=str(rid),
            stop_reason=str(stop), recusa=recusa or stop == "content_filter",
            categoria_recusa="openai_compativel_refusal" if (recusa or stop == "content_filter") else None,
            tokens_entrada=max(entrada - cache, 0), tokens_cache=cache,
            tokens_saida=int(getattr(usage, "completion_tokens", 0) or 0),
        )
