"""Adaptador para qualquer servidor com API OpenAI Chat Completions (vLLM, LM Studio, terceiros).

Serve a QUALQUER provedor do registro com adaptador ``openai_compativel`` (DeepSeek, Mistral, Groq, OpenRouter,
LM Studio, vLLM...): o ``nome`` e a ``base_url`` vem de config/provedores.toml. Modo de saida: o do catalogo
(``saida_estruturada`` json_schema/json) ou, se ele nao declarar, o padrao do provedor no registro; ``json`` usa
``json_object`` e ``texto`` so pede o schema no prompt. Sempre validacao Pydantic. URL base obrigatoria.
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

MODOS_SAIDA = ("json_schema", "json", "texto")


class ProvedorOpenAICompativel(ProvedorBase):
    nome = "openai_compativel"

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None, cliente: Any = None,
                 http_client: Any = None, saida_padrao: str = "json", envia_effort: bool = True, **kw: Any):
        """``saida_padrao``: modo de saida estruturada do PROVEDOR (registro) quando o catalogo nao declara
        ``json_schema``/``json``: json_schema | json | texto. ``envia_effort``: manda ``reasoning_effort``."""
        super().__init__(**kw)
        self._base_url = base_url or os.environ.get("OPENAI_COMPATIVEL_BASE_URL")
        self._api_key = api_key or os.environ.get("OPENAI_COMPATIVEL_API_KEY") or "sem-chave"
        registrar_segredo(self._api_key)
        self._cliente = cliente
        self._http_client = http_client
        self._saida_padrao = saida_padrao if saida_padrao in MODOS_SAIDA else "json"
        self._envia_effort = envia_effort

    def parametros_cliente(self) -> dict[str, Any]:
        if not self._base_url:
            raise ErroProvedor(f"{self.nome} exige base_url (config/provedores.toml ou OPENAI_COMPATIVEL_BASE_URL)")
        kw: dict[str, Any] = {"api_key": self._api_key, "base_url": self._base_url, "max_retries": 0}
        if self._http_client is not None:
            kw["http_client"] = self._http_client
        return kw

    def _get_cliente(self) -> Any:
        if self._cliente is None:
            params = self.parametros_cliente()
            sdk = exigir_pacote("openai", "")
            self._cliente = sdk.AsyncOpenAI(**params)
        return self._cliente

    def _modo_saida(self, modelo: str) -> str:
        if self._catalogo is not None:
            try:
                declarado = str(self._catalogo.obter(f"{self.nome}:{modelo}").saida_estruturada)
            except Exception:
                declarado = "nativa"
            if declarado in MODOS_SAIDA:
                return declarado
        return self._saida_padrao

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
            if modo == "json":
                kw["response_format"] = {"type": "json_object"}
            sis += "\n\nResponda SOMENTE com um objeto JSON que siga este schema:\n" + json.dumps(
                schema.model_json_schema(), ensure_ascii=False)
        kw["messages"] = [{"role": "system", "content": sis}, {"role": "user", "content": mensagem}]
        if effort and self._envia_effort:
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
                conteudo = getattr(delta, "content", None)
                if conteudo:
                    medidor.primeiro_token()
                    partes.append(conteudo)
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
