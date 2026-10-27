"""Barramento em processo (backtest/testes) e ZeroMQ (producao)."""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel


def _dict(msg: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return msg.model_dump(mode="json") if isinstance(msg, BaseModel) else msg


class BarramentoLocal:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[dict[str, Any]], None]]] = defaultdict(list)
        self.seq = 0

    def assinar(self, topico: str, cb: Callable[[dict[str, Any]], None]) -> None:
        self._subs[topico].append(cb)

    def publicar(self, topico: str, msg: BaseModel | dict[str, Any]) -> None:
        self.seq += 1
        d = _dict(msg)
        for cb in list(self._subs.get(topico, ())) + list(self._subs.get("*", ())):
            cb({"topico": topico, "seq": self.seq, **d})


class PublicadorZMQ:
    """PUB com numero de sequencia; o assinante detecta lacunas."""
    def __init__(self, endereco: str) -> None:
        import zmq
        self._ctx = zmq.Context.instance()
        self._s = self._ctx.socket(zmq.PUB)
        self._s.bind(endereco)
        self.seq = 0

    def publicar(self, topico: str, msg: BaseModel | dict[str, Any]) -> None:
        self.seq += 1
        corpo = json.dumps({"seq": self.seq, **_dict(msg)}, default=str)
        self._s.send_multipart([topico.encode(), corpo.encode()])

    def fechar(self) -> None:
        self._s.close(0)


class AssinanteZMQ:
    def __init__(self, endereco: str, topicos: list[str]) -> None:
        import zmq
        self._ctx = zmq.Context.instance()
        self._s = self._ctx.socket(zmq.SUB)
        self._s.connect(endereco)
        for t in topicos:
            self._s.setsockopt(zmq.SUBSCRIBE, t.encode())
        self._ultimo = 0
        self.lacunas = 0

    def receber(self, timeout_ms: int = 100) -> tuple[str, dict[str, Any]] | None:
        if not self._s.poll(timeout_ms):
            return None
        t, c = self._s.recv_multipart()
        d = json.loads(c)
        if self._ultimo and d["seq"] != self._ultimo + 1:
            self.lacunas += 1
        self._ultimo = d["seq"]
        return t.decode(), d

    def fechar(self) -> None:
        self._s.close(0)
