"""Adaptador Google Gemini (extra opcional ``google-genai``; importacao tardia).

Saida estruturada: ``response_mime_type='application/json'`` + ``response_json_schema``.
Se o pacote estiver ausente, ``PacoteAusente`` (erro claro de configuracao) e levantado ao usar.
O mapeamento de ``effort`` para o raciocinio do Gemini nao e aplicado na v1 (nao verificado
nesta sessao): o parametro e ignorado e isso fica registrado na documentacao do adaptador.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from robowin.ia.provedores.base import ErroProvedor, Medidor, ProvedorBase, RespostaBruta, exigir_pacote
from robowin.ia.segredos import registrar_segredo

_BLOQUEIOS = ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY")


def _nome(v: Any) -> str:
    n = getattr(v, "name", None)
    return str(n if n is not None else (v or ""))


class ProvedorGoogle(ProvedorBase):
    nome = "google"

    def __init__(self, *, api_key: str | None = None, cliente: Any = None, **kw: Any):
        super().__init__(**kw)
        self._api_key = api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        registrar_segredo(self._api_key)
        self._cliente = cliente

    def parametros_cliente(self) -> dict[str, Any]:
        """Argumentos de ``google.genai.Client`` (sem rede)."""
        return {"api_key": self._api_key}

    def _get_cliente(self) -> Any:
        if self._cliente is None:
            params = self.parametros_cliente()
            genai = exigir_pacote("google.genai", "google")
            self._cliente = genai.Client(**params)
        return self._cliente

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        config = {
            "system_instruction": sistema, "response_mime_type": "application/json",
            "response_json_schema": schema.model_json_schema(), "max_output_tokens": max_tokens,
        }
        fluxo = await self._get_cliente().aio.models.generate_content_stream(
            model=modelo, contents=mensagem, config=config)
        partes: list[str] = []
        servido = rid = fim = ""
        bloqueio = ""
        usage: Any = None
        async for ch in fluxo:
            if getattr(ch, "text", None):
                medidor.primeiro_token()
                partes.append(ch.text)
            servido = getattr(ch, "model_version", None) or servido
            rid = getattr(ch, "response_id", None) or rid
            if getattr(ch, "usage_metadata", None) is not None:
                usage = ch.usage_metadata
            pf = getattr(ch, "prompt_feedback", None)
            motivo_bloqueio = getattr(pf, "block_reason", None)
            if motivo_bloqueio:
                bloqueio = _nome(motivo_bloqueio)
            for c in getattr(ch, "candidates", None) or []:
                if getattr(c, "finish_reason", None):
                    fim = _nome(c.finish_reason)
        recusa = bool(bloqueio) or fim in _BLOQUEIOS
        stop = "max_tokens" if fim == "MAX_TOKENS" else (fim.lower() or "end_turn")
        cache = int(getattr(usage, "cached_content_token_count", 0) or 0)
        entrada = int(getattr(usage, "prompt_token_count", 0) or 0)
        saida = int(getattr(usage, "candidates_token_count", 0) or 0) + int(
            getattr(usage, "thoughts_token_count", 0) or 0)
        return RespostaBruta(
            texto="".join(partes), modelo_servido=str(servido), request_id=str(rid), stop_reason=stop,
            recusa=recusa, categoria_recusa=(bloqueio or fim) if recusa else None,
            tokens_entrada=max(entrada - cache, 0), tokens_cache=cache, tokens_saida=saida,
        )


class ProvedorGeminiVertex(ProvedorGoogle):
    """Gemini no Vertex AI: ``google.genai.Client(vertexai=True, project=..., location=...)``.

    Credencial: credenciais padrao do Google (``GOOGLE_APPLICATION_CREDENTIALS``) ou chave do modo expresso."""
    nome = "gemini_vertex"

    def __init__(self, *, projeto: str | None = None, regiao: str | None = None, api_key: str | None = None,
                 **kw: Any):
        super().__init__(api_key=api_key or "", **kw)
        self._api_key = api_key  # sem chave: o SDK usa as credenciais padrao do Google
        self._projeto = projeto or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self._regiao = regiao or os.environ.get("GOOGLE_CLOUD_LOCATION")

    def parametros_cliente(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"vertexai": True}
        if self._api_key:
            kw["api_key"] = self._api_key  # modo expresso (sem projeto/regiao)
        else:
            if not self._projeto or not self._regiao:
                raise ErroProvedor("gemini_vertex exige GOOGLE_CLOUD_PROJECT e GOOGLE_CLOUD_LOCATION")
            kw.update(project=self._projeto, location=self._regiao)
        return kw
