"""Adaptador OpenAI: ``responses.stream(text={'format': json_schema}, reasoning={'effort': ...}, store=False)``.

Famila GPT-6 nao tem snapshot datado: o modelo servido e registrado em cada chamada. Sem temperature.
Tambem serve para Azure OpenAI passando ``cliente`` (AsyncAzureOpenAI) pronto.
"""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel

from robowin.ia.provedores.base import ErroProvedor, Medidor, ProvedorBase, RespostaBruta, exigir_pacote
from robowin.ia.segredos import registrar_segredo


def formato_texto(schema: type[BaseModel]) -> dict[str, Any]:
    """Saida estruturada como ``text.format`` (json_schema estrito, a mesma conversao que o SDK faz para
    ``text_format``), validada depois pela camada comum. Com ``text_format`` o SDK valida o JSON DENTRO do stream
    e um texto invalido virava ``ValidationError`` generico em vez de ``json_invalido`` (achado pela bateria de
    contrato por HTTP). Sem o utilitario no SDK, cai no ``text_format``."""
    try:
        from openai.lib._parsing._responses import type_to_text_format_param
    except ImportError:
        return {"text_format": schema}
    return {"text": {"format": type_to_text_format_param(schema)}}


class ProvedorOpenAI(ProvedorBase):
    nome = "openai"

    classe_sdk = "AsyncOpenAI"
    variavel_chave = "OPENAI_API_KEY"

    def __init__(self, *, api_key: str | None = None, cliente: Any = None, base_url: str | None = None,
                 http_client: Any = None, **kw: Any):
        """``http_client`` (opcional): cliente HTTP do SDK, p.ex. com transporte FALSO nos testes (sem rede)."""
        super().__init__(**kw)
        self._api_key = api_key or os.environ.get(self.variavel_chave)
        registrar_segredo(self._api_key)
        self._cliente = cliente
        self._base_url = base_url
        self._http_client = http_client

    def parametros_cliente(self) -> dict[str, Any]:
        """Argumentos do construtor do SDK (sem rede). ``max_retries=0`` sempre."""
        kw: dict[str, Any] = {"api_key": self._api_key, "max_retries": 0}
        if self._base_url:
            kw["base_url"] = self._base_url
        if self._http_client is not None:
            kw["http_client"] = self._http_client
        return kw

    def _get_cliente(self) -> Any:
        if self._cliente is None:
            sdk = exigir_pacote("openai", "")
            self._cliente = getattr(sdk, self.classe_sdk)(**self.parametros_cliente())
        return self._cliente

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        kw: dict[str, Any] = {
            "model": modelo, "instructions": sistema, "input": mensagem,
            "max_output_tokens": max_tokens, "store": False, **formato_texto(schema),
        }
        if effort:
            kw["reasoning"] = {"effort": effort}
        terminal: Any = None
        async with self._get_cliente().responses.stream(**kw) as fluxo:
            async for ev in fluxo:
                tipo = str(getattr(ev, "type", ""))
                if tipo.endswith(".delta"):
                    medidor.primeiro_token()
                if tipo in ("response.completed", "response.incomplete", "response.failed"):
                    terminal = getattr(ev, "response", None)
            try:
                resp = await fluxo.get_final_response()
            except RuntimeError:
                # o SDK so monta a resposta final com 'response.completed'; com max_output_tokens/content_filter a
                # API termina em 'response.incomplete': normaliza por ela (vira max_tokens/recusa, nao erro generico)
                if terminal is None:
                    raise
                resp = terminal
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


class ProvedorAzureOpenAI(ProvedorOpenAI):
    """Modelos OpenAI no Azure (``AsyncAzureOpenAI``). O 'modelo' e o nome da implantacao (deployment);
    ``azure_endpoint`` = https://<recurso>.openai.azure.com e ``api_version`` sao obrigatorios."""
    nome = "azure_openai"
    classe_sdk = "AsyncAzureOpenAI"
    variavel_chave = "AZURE_OPENAI_API_KEY"

    def __init__(self, *, azure_endpoint: str | None = None, api_version: str | None = None, **kw: Any):
        super().__init__(**kw)
        self._endpoint = azure_endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        self._api_version = api_version or os.environ.get("OPENAI_API_VERSION")

    def parametros_cliente(self) -> dict[str, Any]:
        if not self._endpoint or not self._api_version:
            raise ErroProvedor("azure_openai exige AZURE_OPENAI_ENDPOINT e OPENAI_API_VERSION")
        kw = super().parametros_cliente()
        kw.pop("base_url", None)
        kw.update(azure_endpoint=self._endpoint, api_version=self._api_version)
        return kw
