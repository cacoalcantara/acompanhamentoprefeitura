"""Adaptador OpenAI: ``responses.stream(text_format=Modelo, reasoning={'effort': ...}, store=False)``.

Famila GPT-6 nao tem snapshot datado: o modelo servido e registrado em cada chamada. Sem temperature.
Tambem serve para Azure OpenAI passando ``cliente`` (AsyncAzureOpenAI) pronto.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from robowin.ia.provedores.base import Medidor, ProvedorBase, RespostaBruta, exigir_pacote
from robowin.ia.segredos import registrar_segredo


class ProvedorOpenAI(ProvedorBase):
    nome = "openai"

    def __init__(self, *, api_key: str | None = None, cliente: Any = None, base_url: str | None = None, **kw: Any):
        super().__init__(**kw)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        registrar_segredo(self._api_key)
        self._cliente = cliente
        self._base_url = base_url

    def _get_cliente(self) -> Any:
        if self._cliente is None:
            sdk = exigir_pacote("openai", "")
            extra = {"base_url": self._base_url} if self._base_url else {}
            self._cliente = sdk.AsyncOpenAI(api_key=self._api_key, max_retries=0, **extra)
        return self._cliente

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        kw: dict[str, Any] = {
            "model": modelo, "instructions": sistema, "input": mensagem, "text_format": schema,
            "max_output_tokens": max_tokens, "store": False,
        }
        if effort:
            kw["reasoning"] = {"effort": effort}
        async with self._get_cliente().responses.stream(**kw) as fluxo:
            async for ev in fluxo:
                if str(getattr(ev, "type", "")).endswith(".delta"):
                    medidor.primeiro_token()
            resp = await fluxo.get_final_response()
        return self._normalizar(resp)

    @staticmethod
    def _normalizar(resp: Any) -> RespostaBruta:
        usage = getattr(resp, "usage", None)
        det = getattr(usage, "input_tokens_details", None)
        cache = int(getattr(det, "cached_tokens", 0) or 0)
        entrada = int(getattr(usage, "input_tokens", 0) or 0)
        status = str(getattr(resp, "status", "") or "")
        inc = getattr(getattr(resp, "incomplete_details", None), "reason", None)
        recusa = False
        texto = ""
        for item in getattr(resp, "output", None) or []:
            for c in getattr(item, "content", None) or []:
                t = getattr(c, "type", "")
                if t == "refusal":
                    recusa = True
                    texto += getattr(c, "refusal", "") or ""
                elif t == "output_text":
                    texto += getattr(c, "text", "") or ""
        stop = "max_tokens" if inc == "max_output_tokens" else (inc or ("end_turn" if status == "completed" else status))
        if inc == "content_filter":
            recusa = True
        objeto = None if recusa or stop == "max_tokens" else getattr(resp, "output_parsed", None)
        return RespostaBruta(
            texto=texto, objeto=objeto, modelo_servido=str(getattr(resp, "model", "") or ""),
            request_id=str(getattr(resp, "_request_id", "") or getattr(resp, "id", "") or ""),
            stop_reason=str(stop), recusa=recusa, categoria_recusa=("openai_refusal" if recusa else None),
            tokens_entrada=max(entrada - cache, 0), tokens_cache=cache,
            tokens_saida=int(getattr(usage, "output_tokens", 0) or 0),
        )
