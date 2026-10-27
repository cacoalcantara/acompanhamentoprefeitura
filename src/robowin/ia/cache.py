"""Cache deterministico de respostas de LLM para backtest: modo gravacao / repeticao.

A chave e o hash da requisicao (modelo, sistema, mensagem, schema, effort, max_tokens): duas execucoes
com a mesma requisicao produzem a mesma chave. Em ``repetir`` nunca ha chamada real: falta no cache =>
resposta invalida ``cache_miss`` (o backtest e reprodutivel e nao gasta verba).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from robowin.core.tipos import ChamadaLLM

Modo = Literal["desligado", "gravar", "repetir"]


def hash_requisicao(*, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
                    effort: str | None, max_tokens: int) -> str:
    canon = json.dumps(
        {"modelo": modelo, "sistema": sistema, "mensagem": mensagem, "schema": schema.model_json_schema(),
         "effort": effort, "max_tokens": max_tokens},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class CacheLLM:
    def __init__(self, diretorio: str | Path, modo: Modo = "repetir"):
        self.dir = Path(diretorio)
        self.modo = modo
        if modo == "gravar":
            self.dir.mkdir(parents=True, exist_ok=True)
        self.acertos = self.faltas = 0

    def _arq(self, chave: str) -> Path:
        return self.dir / f"{chave}.json"

    def obter(self, chave: str, schema: type[BaseModel]) -> tuple[BaseModel | None, ChamadaLLM] | None:
        p = self._arq(chave)
        if not p.exists():
            self.faltas += 1
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
        self.acertos += 1
        obj = schema.model_validate(d["objeto"]) if d.get("objeto") is not None else None
        return obj, ChamadaLLM(**d["chamada"])

    def gravar(self, chave: str, objeto: BaseModel | None, chamada: ChamadaLLM) -> None:
        d: dict[str, Any] = {"chave": chave, "objeto": objeto.model_dump(mode="json") if objeto else None,
                             "chamada": chamada.model_dump(mode="json")}
        self._arq(chave).write_text(json.dumps(d, sort_keys=True, ensure_ascii=False), encoding="utf-8")


class ProvedorComCache:
    def __init__(self, interno: Any, cache: CacheLLM):
        self.interno, self.cache = interno, cache
        self.nome = interno.nome

    async def gerar_estruturado(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        prazo_s: float, effort: str | None = None, max_tokens: int = 4096,
    ) -> tuple[BaseModel | None, ChamadaLLM]:
        kw = dict(modelo=modelo, sistema=sistema, mensagem=mensagem, schema=schema, prazo_s=prazo_s,
                  effort=effort, max_tokens=max_tokens)
        if self.cache.modo == "desligado":
            return await self.interno.gerar_estruturado(**kw)
        chave = hash_requisicao(modelo=f"{self.nome}:{modelo}", sistema=sistema, mensagem=mensagem,
                                schema=schema, effort=effort, max_tokens=max_tokens)
        if self.cache.modo == "repetir":
            achado = self.cache.obter(chave, schema)
            if achado is not None:
                return achado
            return None, ChamadaLLM(papel="", modelo_pedido=f"{self.nome}:{modelo}", erro="cache_miss",
                                    hash_prompt=chave)
        achado = self.cache.obter(chave, schema) if self.cache._arq(chave).exists() else None
        if achado is not None:
            return achado
        obj, ch = await self.interno.gerar_estruturado(**kw)
        self.cache.gravar(chave, obj, ch)
        return obj, ch
