"""Contrato comum dos provedores de LLM.

Garantias de TODO adaptador (impostas aqui, nao em cada SDK):
- prazo imposto por ``asyncio.wait_for`` (o timeout do SDK vale por fase e nao basta);
- sem retentativa automatica (``max_retries=0`` nos SDKs) e sem fallback de servidor;
- recusa, max_tokens, JSON invalido, erro HTTP (429/529), modelo servido fora da lista
  => resposta invalida (``valida=False``), nunca excecao nao tratada;
- nunca se pede campo de raciocinio/thinking no schema (rejeitado em ``verificar_schema``).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from robowin.core.tipos import ChamadaLLM
from robowin.ia.segredos import redigir

log = logging.getLogger("robowin.ia")

CAMPOS_PROIBIDOS_SCHEMA = ("reasoning", "thinking", "raciocinio", "chain_of_thought", "cot")
MAX_RESPOSTA_BRUTA = 20000
PARADAS_MAX_TOKENS = ("max_tokens", "length", "max_output_tokens", "model_context_window_exceeded")


class ErroProvedor(Exception):
    """Erro de configuracao/ambiente (ex.: pacote opcional ausente). Nao e erro de chamada."""


class PacoteAusente(ErroProvedor):
    pass


def hash_texto(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


@dataclass
class Medidor:
    """Mede TTFT e total com relogio monotonico."""
    t0: float = field(default_factory=time.monotonic)
    t_primeiro: float | None = None

    def primeiro_token(self) -> None:
        if self.t_primeiro is None:
            self.t_primeiro = time.monotonic()

    @property
    def ttft_ms(self) -> float:
        return 0.0 if self.t_primeiro is None else (self.t_primeiro - self.t0) * 1000

    @property
    def total_ms(self) -> float:
        return (time.monotonic() - self.t0) * 1000


@dataclass
class RespostaBruta:
    """Resposta normalizada de um adaptador, antes da validacao comum."""
    texto: str = ""
    objeto: BaseModel | None = None
    modelo_servido: str = ""
    request_id: str = ""
    versao_api: str = ""
    stop_reason: str = ""
    recusa: bool = False
    categoria_recusa: str | None = None
    tokens_entrada: int = 0
    tokens_cache: int = 0
    tokens_saida: int = 0
    erro_parse: str | None = None


def verificar_schema(schema: type[BaseModel]) -> None:
    """Falha se o schema pedir campos de raciocinio/thinking."""
    def varrer(d: Any) -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                if k == "properties" and isinstance(v, dict):
                    for nome in v:
                        if any(p == str(nome).lower() or p in str(nome).lower().split("_")
                               for p in CAMPOS_PROIBIDOS_SCHEMA):
                            raise ErroProvedor(f"schema pede campo de raciocinio: {nome}")
                varrer(v)
        elif isinstance(d, list):
            for x in d:
                varrer(x)
    varrer(schema.model_json_schema())


def status_http(e: BaseException) -> int | None:
    for attr in ("status_code", "status"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    v = getattr(getattr(e, "response", None), "status_code", None)
    return v if isinstance(v, int) else None


def descrever_erro(e: BaseException) -> str:
    st = status_http(e)
    if st is not None:
        return f"http_{st}"
    return f"{type(e).__name__}: {redigir(str(e))[:300]}"


def modelo_servido_aceito(pedido: str, servido: str, aceitos: Iterable[str] | None) -> bool:
    if not servido:
        return True  # provedor nao informa; regressao.py registra a ausencia
    if aceitos is not None:
        return any(servido == a or servido.startswith(a + "-") for a in aceitos)
    return servido == pedido or servido.startswith((pedido + "-", pedido + ":"))


class ProvedorBase(ABC):
    """Esqueleto comum; subclasses implementam ``_executar``."""
    nome: str = "base"
    rotulo: str = ""  # ex.: "FALSO" para o provedor de testes

    def __init__(self, *, catalogo: Any = None, modelos_servidos_aceitos: Iterable[str] | None = None,
                 nome: str | None = None) -> None:
        if nome:
            self.nome = nome  # nome do REGISTRO (ex.: 'deepseek' com o adaptador openai_compativel)
        self._catalogo = catalogo
        self._aceitos = list(modelos_servidos_aceitos) if modelos_servidos_aceitos is not None else None

    @abstractmethod
    async def _executar(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        effort: str | None, max_tokens: int, medidor: Medidor,
    ) -> RespostaBruta: ...

    def _custo(self, modelo: str, r: RespostaBruta) -> float:
        if self._catalogo is None:
            return 0.0
        try:
            return float(self._catalogo.custo_tokens(
                f"{self.nome}:{modelo}", r.tokens_entrada, r.tokens_cache, r.tokens_saida))
        except Exception:  # modelo fora do catalogo: custo desconhecido nao derruba a chamada
            return 0.0

    async def gerar_estruturado(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        prazo_s: float, effort: str | None = None, max_tokens: int = 4096,
    ) -> tuple[BaseModel | None, ChamadaLLM]:
        base = ChamadaLLM(
            papel="", modelo_pedido=f"{self.nome}:{modelo}", hash_prompt=hash_texto(sistema),
            hash_dossie=hash_texto(mensagem),
        )
        try:
            verificar_schema(schema)
        except ErroProvedor as e:
            return None, base.model_copy(update={"erro": str(e)})
        medidor = Medidor()
        try:
            r = await asyncio.wait_for(
                self._executar(modelo=modelo, sistema=sistema, mensagem=mensagem, schema=schema,
                               effort=effort, max_tokens=max_tokens, medidor=medidor),
                timeout=prazo_s,
            )
        except TimeoutError:
            log.warning("prazo estourado modelo=%s prazo_s=%s", base.modelo_pedido, prazo_s)
            return None, base.model_copy(update={"erro": "timeout", "total_ms": medidor.total_ms,
                                                 "ttft_ms": medidor.ttft_ms})
        except ErroProvedor:
            raise
        except Exception as e:  # 429/529/rede/SDK: resposta invalida, nunca excecao
            msg = descrever_erro(e)
            log.warning("erro de chamada modelo=%s erro=%s", base.modelo_pedido, msg)
            return None, base.model_copy(update={"erro": msg, "total_ms": medidor.total_ms,
                                                 "ttft_ms": medidor.ttft_ms})
        return self._finalizar(base, r, modelo, schema, medidor)

    def _finalizar(
        self, base: ChamadaLLM, r: RespostaBruta, modelo: str, schema: type[BaseModel], medidor: Medidor,
    ) -> tuple[BaseModel | None, ChamadaLLM]:
        objeto: BaseModel | None = None
        erro: str | None = None
        if r.recusa:
            erro = "recusa"
        elif r.stop_reason in PARADAS_MAX_TOKENS:
            erro = "max_tokens"
        elif not modelo_servido_aceito(modelo, r.modelo_servido, self._aceitos):
            erro = f"modelo_servido_fora_da_lista:{r.modelo_servido}"
        else:
            objeto = r.objeto
            if objeto is not None and not isinstance(objeto, schema):
                try:
                    objeto = schema.model_validate(
                        objeto.model_dump() if isinstance(objeto, BaseModel) else objeto)
                except (ValidationError, ValueError):
                    objeto = None
            if objeto is None:
                try:
                    objeto = schema.model_validate_json(r.texto)
                except (ValidationError, ValueError):
                    erro = r.erro_parse or "json_invalido"
        valida = erro is None and objeto is not None
        chamada = base.model_copy(update={
            "modelo_servido": r.modelo_servido, "request_id": r.request_id, "versao_api": r.versao_api,
            "resposta_bruta": r.texto[:MAX_RESPOSTA_BRUTA], "stop_reason": r.stop_reason,
            "categoria_recusa": r.categoria_recusa, "tokens_entrada": r.tokens_entrada,
            "tokens_cache": r.tokens_cache, "tokens_saida": r.tokens_saida,
            "custo_usd": self._custo(modelo, r), "ttft_ms": medidor.ttft_ms, "total_ms": medidor.total_ms,
            "erro": erro, "valida": valida,
        })
        return (objeto if valida else None), chamada


def exigir_pacote(modulo: str, extra: str) -> Any:
    try:
        return importlib.import_module(modulo)
    except ImportError as e:
        raise PacoteAusente(
            f"pacote '{modulo}' ausente; instale o extra opcional: uv sync --extra {extra}"
        ) from e
