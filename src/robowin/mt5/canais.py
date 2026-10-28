"""Canais de comando entre nucleo e gateway: ROUTER/DEALER (ZeroMQ) e versao em processo para testes.

Mensagens sao dicts JSON com `tipo` e `id` (correlacao). O lado do gateway implementa
`receber(timeout_ms) -> (ident, msg) | None` e `responder(ident, msg)`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import time
import uuid
from collections.abc import Callable
from typing import Any

VAR_TOKEN = "ROBOWIN_CMD_TOKEN"


class BindInseguro(RuntimeError):
    """O canal de comandos (ordem/emergencia/rearmar) nao pode escutar fora do loopback sem token compartilhado."""


def _novo_id() -> str:
    return uuid.uuid4().hex[:12]


def token_do_ambiente() -> str:
    return os.environ.get(VAR_TOKEN, "").strip()


def endereco_local(endereco: str) -> bool:
    """True se o endereco so e alcancavel nesta maquina: 127.0.0.1 / localhost / ::1 / ipc / inproc."""
    esquema, _, resto = endereco.partition("://")
    if esquema in ("ipc", "inproc"):
        return True
    host = resto.rsplit(":", 1)[0].strip("[]")
    return host in ("127.0.0.1", "localhost", "::1")


def exigir_bind_seguro(endereco: str, token: str = "") -> None:
    if not endereco_local(endereco) and not token:
        raise BindInseguro(f"canal de comandos em '{endereco}' (fora de 127.0.0.1/localhost/ipc) exige token compartilhado: "
                           f"defina {VAR_TOKEN} no gateway e nos clientes, ou use tcp://127.0.0.1:porta")


def assinatura(msg: dict[str, Any], token: str) -> str:
    corpo = json.dumps({k: v for k, v in msg.items() if k != "token"}, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(token.encode(), corpo.encode(), hashlib.sha256).hexdigest()


def assinar(msg: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    """Copia de `msg` com o campo `token` (HMAC-SHA256 do conteudo); sem token configurado devolve a copia sem assinar."""
    tk = token_do_ambiente() if token is None else token
    out = dict(msg)
    if tk:
        out["token"] = assinatura(out, tk)
    return out


def token_valido(msg: dict[str, Any], token: str) -> bool:
    return hmac.compare_digest(str(msg.get("token", "")), assinatura(msg, token))


class CanalComandosLocal:
    """Lado gateway + fabrica de clientes em processo (testes, BarramentoLocal)."""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[Any, dict[str, Any]]] = queue.Queue()
        self._resp: dict[Any, queue.Queue[dict[str, Any]]] = {}

    def receber(self, timeout_ms: int = 0) -> tuple[Any, dict[str, Any]] | None:
        try:
            if timeout_ms <= 0:
                return self._q.get_nowait()
            return self._q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None

    def responder(self, ident: Any, msg: dict[str, Any]) -> None:
        self._resp.setdefault(ident, queue.Queue()).put(msg)

    def fechar(self) -> None:
        return None

    def cliente(self, ident: str = "local") -> ClienteLocal:
        return ClienteLocal(self, ident)


class ClienteLocal:
    def __init__(self, canal: CanalComandosLocal, ident: str) -> None:
        self.canal, self.ident = canal, ident
        self.canal._resp.setdefault(ident, queue.Queue())

    def enviar_sem_resposta(self, msg: dict[str, Any]) -> None:
        self.canal._q.put((self.ident, assinar(msg)))

    def enviar(self, msg: dict[str, Any], timeout_s: float = 5.0,
               bombear: Callable[[], None] | None = None) -> dict[str, Any] | None:
        """Envia e espera a resposta com o mesmo `id`. `bombear` roda o gateway no mesmo thread (testes)."""
        msg = {"id": _novo_id(), **msg}
        self.enviar_sem_resposta(msg)
        fim = time.monotonic() + timeout_s
        while time.monotonic() < fim:
            if bombear is not None:
                bombear()
            try:
                r = self.canal._resp[self.ident].get(timeout=0.01)
            except queue.Empty:
                continue
            if r.get("id") == msg["id"]:
                return r
        return None


class CanalComandosZMQ:
    """Lado gateway: ROUTER."""

    def __init__(self, endereco: str, token: str | None = None) -> None:
        exigir_bind_seguro(endereco, token_do_ambiente() if token is None else token)
        import zmq
        self._ctx = zmq.Context.instance()
        self._s = self._ctx.socket(zmq.ROUTER)
        self._s.bind(endereco)

    def receber(self, timeout_ms: int = 0) -> tuple[Any, dict[str, Any]] | None:
        if not self._s.poll(timeout_ms):
            return None
        quadros = self._s.recv_multipart()
        try:
            return quadros[0], json.loads(quadros[-1])
        except (ValueError, UnicodeDecodeError):
            return quadros[0], {"tipo": "invalida"}

    def responder(self, ident: Any, msg: dict[str, Any]) -> None:
        self._s.send_multipart([ident, json.dumps(msg, default=str).encode()])

    def fechar(self) -> None:
        self._s.close(0)


class ClienteComandosZMQ:
    """Lado nucleo/CLI: DEALER."""

    def __init__(self, endereco: str) -> None:
        import zmq
        self._ctx = zmq.Context.instance()
        self._s = self._ctx.socket(zmq.DEALER)
        self._s.connect(endereco)

    def enviar_sem_resposta(self, msg: dict[str, Any]) -> None:
        self._s.send(json.dumps(assinar(msg), default=str).encode())

    def enviar(self, msg: dict[str, Any], timeout_s: float = 5.0) -> dict[str, Any] | None:
        msg = {"id": _novo_id(), **msg}
        self.enviar_sem_resposta(msg)
        fim = time.monotonic() + timeout_s
        while (resta := fim - time.monotonic()) > 0:
            if not self._s.poll(int(resta * 1000) + 1):
                break
            r = json.loads(self._s.recv())
            if r.get("id") == msg["id"]:
                return r
        return None

    def fechar(self) -> None:
        self._s.close(0)
