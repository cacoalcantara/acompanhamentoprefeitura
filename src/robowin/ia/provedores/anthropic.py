"""Adaptador Anthropic: ``messages.stream(output_format=Modelo, output_config={'effort': ...})``.

Opus 5.5 nao aceita temperature/top_p/top_k nem thinking desligado: nenhum deles e enviado.
Recusa = HTTP 200 com ``stop_reason == 'refusal'`` (categoria em ``stop_details.category``).
Streaming para medir o TTFT; ``max_retries=0``; sem fallback de servidor.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from robowin.ia.provedores.base import Medidor, ProvedorBase, RespostaBruta, exigir_pacote
from robowin.ia.segredos import registrar_segredo

_EVENTOS_TOKEN = {"text", "thinking", "content_block_delta"}


class ProvedorAnthropic(ProvedorBase):
    nome = "anthropic"

    def __init__(self, *, api_key: str | None = None, cliente: Any = None, base_url: str | None = None, **kw: Any):
        super().__init__(**kw)
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        registrar_segredo(self._api_key)
        self._cliente = cliente
        self._base_url = base_url

    def _get_cliente(self) -> Any:
        if self._cliente is None:
            sdk = exigir_pacote("anthropic", "")
            extra = {"base_url": self._base_url} if self._base_url else {}
            self._cliente = sdk.AsyncAnthropic(api_key=self._api_key, max_retries=0, **extra)
        return self._cliente

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        kw: dict[str, Any] = {
            "model": modelo, "max_tokens": max_tokens, "system": sistema,
            "messages": [{"role": "user", "content": mensagem}], "output_format": schema,
        }
        if effort:
            kw["output_config"] = {"effort": effort}
        async with self._get_cliente().messages.stream(**kw) as fluxo:
            async for ev in fluxo:
                if getattr(ev, "type", "") in _EVENTOS_TOKEN:
                    medidor.primeiro_token()
            msg = await fluxo.get_final_message()
        return self._normalizar(msg)

    @staticmethod
    def _normalizar(msg: Any) -> RespostaBruta:
        usage = getattr(msg, "usage", None)
        texto = "".join(
            getattr(b, "text", "") or "" for b in (getattr(msg, "content", None) or [])
            if getattr(b, "type", "") == "text"
        )
        stop = str(getattr(msg, "stop_reason", "") or "")
        det = getattr(msg, "stop_details", None)
        recusa = stop == "refusal"
        objeto = None
        erro_parse = None
        if not recusa and stop != "max_tokens":
            try:
                objeto = getattr(msg, "parsed_output", None)
            except Exception:
                erro_parse = "json_invalido"
        return RespostaBruta(
            texto=texto, objeto=objeto, modelo_servido=str(getattr(msg, "model", "") or ""),
            request_id=str(getattr(msg, "_request_id", "") or ""), stop_reason=stop, recusa=recusa,
            categoria_recusa=(getattr(det, "category", None) or "desconhecida") if recusa else None,
            tokens_entrada=int(getattr(usage, "input_tokens", 0) or 0),
            tokens_cache=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            tokens_saida=int(getattr(usage, "output_tokens", 0) or 0), erro_parse=erro_parse,
        )
