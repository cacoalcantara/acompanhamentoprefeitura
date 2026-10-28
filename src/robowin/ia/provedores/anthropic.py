"""Adaptador Anthropic: ``messages.stream(output_config={'format': json_schema, 'effort': ...})``.

O schema vai como dicionario em ``output_config.format`` (caminho documentado do SDK) e o JSON e validado pela
camada comum (``base.py``). NAO se usa ``output_format=Modelo``: com ele o SDK valida o texto no meio do stream
(no fim do bloco, ANTES de chegar o ``stop_reason``) e uma recusa ou um corte por max_tokens viravam
``ValidationError`` generico em vez de ``recusa``/``max_tokens`` (achado pela bateria de contrato por HTTP).

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


def schema_para_anthropic(schema: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema do modelo no formato aceito pela saida estruturada (a mesma transformacao que o SDK aplica a
    ``output_format``); sem ela disponivel, o schema do Pydantic como esta."""
    bruto = schema.model_json_schema()
    try:
        from anthropic.lib._parse._transform import transform_schema
    except ImportError:  # SDK sem o utilitario: o servidor valida o schema
        return bruto
    return dict(transform_schema(bruto))


class ProvedorAnthropic(ProvedorBase):
    nome = "anthropic"

    classe_sdk = "AsyncAnthropic"
    variavel_chave = "ANTHROPIC_API_KEY"

    def __init__(self, *, api_key: str | None = None, cliente: Any = None, base_url: str | None = None,
                 http_client: Any = None, **kw: Any):
        """``http_client`` (opcional): cliente HTTP do SDK, p.ex. com transporte FALSO nos testes (sem rede)."""
        super().__init__(**kw)
        self._api_key = api_key or (os.environ.get(self.variavel_chave) if self.variavel_chave else None)
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
            sdk = exigir_pacote("anthropic", "")
            self._cliente = getattr(sdk, self.classe_sdk)(**self.parametros_cliente())
        return self._cliente

    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta:
        config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema_para_anthropic(schema)}}
        if effort:
            config["effort"] = effort
        kw: dict[str, Any] = {
            "model": modelo, "max_tokens": max_tokens, "system": sistema,
            "messages": [{"role": "user", "content": mensagem}], "output_config": config,
        }
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


class ProvedorAnthropicBedrock(ProvedorAnthropic):
    """Claude no Amazon Bedrock pelo cliente de plataforma ``AsyncAnthropicBedrock`` do SDK anthropic.

    Credencial: chave de API do Bedrock (``AWS_BEARER_TOKEN_BEDROCK``); a URL sai da regiao
    (``https://bedrock-runtime.<regiao>.amazonaws.com``), calculada pelo SDK quando ``base_url`` e vazia."""
    nome = "aws_bedrock"
    classe_sdk = "AsyncAnthropicBedrock"
    variavel_chave = "AWS_BEARER_TOKEN_BEDROCK"

    def __init__(self, *, regiao: str | None = None, **kw: Any):
        super().__init__(**kw)
        self._regiao = regiao or os.environ.get("AWS_REGION")

    def parametros_cliente(self) -> dict[str, Any]:
        kw = super().parametros_cliente()
        if kw.get("api_key") is None:
            kw.pop("api_key")  # sem chave de API: o SDK usa as credenciais AWS (exige botocore)
        if self._regiao:
            kw["aws_region"] = self._regiao
        return kw


class ProvedorAnthropicVertex(ProvedorAnthropic):
    """Claude no Google Vertex AI pelo cliente de plataforma ``AsyncAnthropicVertex`` do SDK anthropic.

    Credencial: token de acesso (``access_token``) ou credenciais padrao do Google (google-auth)."""
    nome = "google_vertex"
    classe_sdk = "AsyncAnthropicVertex"
    variavel_chave = ""

    def __init__(self, *, regiao: str | None = None, projeto: str | None = None, access_token: str | None = None,
                 **kw: Any):
        super().__init__(**kw)
        self._regiao = regiao or os.environ.get("CLOUD_ML_REGION")
        self._projeto = projeto or os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID")
        self._token = access_token
        registrar_segredo(access_token)

    def parametros_cliente(self) -> dict[str, Any]:
        kw = super().parametros_cliente()
        kw.pop("api_key", None)
        if self._regiao:
            kw["region"] = self._regiao
        if self._projeto:
            kw["project_id"] = self._projeto
        if self._token:
            kw["access_token"] = self._token
        return kw
