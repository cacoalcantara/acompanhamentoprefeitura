"""Transporte HTTP FALSO para os SDKs (anthropic, openai): respostas fabricadas no formato da API, sem rede.

O SDK de verdade monta a requisicao (URL, cabecalhos, corpo) e interpreta a resposta (SSE); o transporte so
grava a requisicao e devolve o texto fabricado. Assim o teste confere o caminho completo adaptador -> SDK -> HTTP
sem chave real e sem rede.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2


def _sse(eventos: list[tuple[str | None, dict[str, Any] | str]]) -> bytes:
    linhas: list[str] = []
    for nome, dado in eventos:
        if nome:
            linhas.append(f"event: {nome}")
        linhas.append("data: " + (dado if isinstance(dado, str) else json.dumps(dado)))
        linhas.append("")
    return ("\n".join(linhas) + "\n").encode("utf-8")


def sse_chat_completions(texto: str, *, modelo: str = "m", fim: str = "stop", recusa: bool = False) -> bytes:
    """Stream de /chat/completions (OpenAI e compativeis) com uso no ultimo pedaco."""
    delta: dict[str, Any] = {"role": "assistant", "content": None if recusa else texto}
    if recusa:
        delta["refusal"] = "nao posso ajudar"
    base = {"id": "cmpl-falso-1", "object": "chat.completion.chunk", "created": 1, "model": modelo}
    return _sse([
        (None, {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}),
        (None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": fim}]}),
        (None, {**base, "choices": [], "usage": {"prompt_tokens": 6000, "completion_tokens": 500,
                                                 "total_tokens": 6500,
                                                 "prompt_tokens_details": {"cached_tokens": 5000}}}),
        (None, "[DONE]"),
    ])


def sse_anthropic(texto: str, *, modelo: str = "m", parada: str = "end_turn") -> bytes:
    """Stream da Messages API da Anthropic (tambem servido pelo Vertex)."""
    msg = {"id": "msg_falso_1", "type": "message", "role": "assistant", "model": modelo, "content": [],
           "stop_reason": None, "stop_sequence": None,
           "usage": {"input_tokens": 1000, "output_tokens": 1, "cache_read_input_tokens": 5000,
                     "cache_creation_input_tokens": 0}}
    return _sse([
        ("message_start", {"type": "message_start", "message": msg}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": texto}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": parada, "stop_sequence": None},
                           "usage": {"output_tokens": 500}}),
        ("message_stop", {"type": "message_stop"}),
    ])


def _resposta_openai(texto: str, modelo: str, status: str, incompleto: str | None, recusa: bool) -> dict[str, Any]:
    conteudo = ({"type": "refusal", "refusal": "nao posso ajudar"} if recusa
                else {"type": "output_text", "text": texto, "annotations": []})
    return {
        "id": "resp_falso_1", "object": "response", "created_at": 1, "model": modelo, "status": status,
        "incomplete_details": {"reason": incompleto} if incompleto else None, "error": None,
        "output": [{"type": "message", "id": "msg_falso_1", "status": "completed", "role": "assistant",
                    "content": [conteudo]}],
        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        "usage": {"input_tokens": 6000, "input_tokens_details": {"cached_tokens": 5000}, "output_tokens": 500,
                  "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 6500},
    }


def sse_responses(texto: str, *, modelo: str = "m", incompleto: str | None = None, recusa: bool = False) -> bytes:
    """Stream da Responses API da OpenAI (tambem servida pelo Azure OpenAI)."""
    final = _resposta_openai(texto, modelo, "incomplete" if incompleto else "completed", incompleto, recusa)
    inicial = {**final, "status": "in_progress", "output": [], "usage": None}
    fim = "response.incomplete" if incompleto else "response.completed"
    parte = dict(final["output"][0]["content"][0])
    parte["refusal" if recusa else "text"] = ""
    delta = ("response.refusal.delta", {"type": "response.refusal.delta", "sequence_number": 3,
                                        "item_id": "msg_falso_1", "output_index": 0, "content_index": 0,
                                        "delta": "nao posso ajudar"}) if recusa else (
        "response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 3,
                                       "item_id": "msg_falso_1", "output_index": 0, "content_index": 0,
                                       "delta": texto, "logprobs": []})
    return _sse([
        ("response.created", {"type": "response.created", "sequence_number": 0, "response": inicial}),
        ("response.output_item.added", {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
                                        "item": {**final["output"][0], "status": "in_progress", "content": []}}),
        ("response.content_part.added", {"type": "response.content_part.added", "sequence_number": 2,
                                         "item_id": "msg_falso_1", "output_index": 0, "content_index": 0,
                                         "part": parte}),
        delta,
        (fim, {"type": fim, "sequence_number": 4, "response": final}),
    ])


@dataclass
class TransporteFalso:
    """Devolve ``corpo`` (bytes, ou funcao da requisicao -> bytes) com status ``status``, opcionalmente depois de
    ``atraso_s`` (para testar o prazo), e grava cada requisicao recebida. Nunca abre conexao."""
    corpo: bytes | Callable[[httpx2.Request], bytes]
    status: int = 200
    atraso_s: float = 0.0
    requisicoes: list[httpx2.Request] = field(default_factory=list)

    async def _responder(self, req: httpx2.Request) -> httpx2.Response:
        self.requisicoes.append(req)
        if self.atraso_s:
            await asyncio.sleep(self.atraso_s)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"type": "falso", "message": "erro fabricado"}})
        corpo = self.corpo(req) if callable(self.corpo) else self.corpo
        return httpx2.Response(200, content=corpo, headers={"content-type": "text/event-stream",
                                                                  "request-id": "req_falso_1"})

    def cliente(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._responder))

    @property
    def ultima(self) -> httpx2.Request:
        return self.requisicoes[-1]

    def corpo_json(self) -> dict[str, Any]:
        return json.loads(self.ultima.content.decode("utf-8"))
