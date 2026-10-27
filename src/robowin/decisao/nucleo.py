"""``robowin nucleo``: o mesmo orquestrador alimentado AO VIVO pelo gateway via ZeroMQ.

O nucleo NUNCA envia ordem real: os bracos operam no simulador interno com os ticks/livros vivos; o
espelhamento de UM braco na conta demo (comandos ao gateway, unico dono de ``order_send``) fica como ponto
de extensao (``Roteador(modo="gateway_demo", emissor_gateway=...)``) e nao e ligado aqui.

Falha segura: lacuna de sequencia do PUB/SUB, heartbeat do gateway ausente ou dado velho bloqueiam novas
entradas; saidas e protecao continuam. O nucleo envia heartbeat ao gateway (sem ele o gateway bloqueia
ordens novas e depois zera a posicao demo).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from robowin.core.tipos import LivroSnapshot, NivelBook, Tick

from .orquestrador import Orquestrador
from .registro import ORIGEM_SINTETICO

NS = 1_000_000_000
CAMPOS_TICK = ("t_servidor_ms", "t_utc_ns", "t_recebido_ns", "bid", "ask", "last", "volume", "flags")
TOPICOS = ["tick", "livro", "heartbeat", "estado", "alerta"]


def tick_de_dict(d: dict[str, Any]) -> Tick:
    return Tick(**{k: d[k] for k in CAMPOS_TICK if k in d})


def livro_de_dict(d: dict[str, Any]) -> LivroSnapshot:
    return LivroSnapshot(t_utc_ns=int(d["t_utc_ns"]), niveis=tuple(NivelBook(**n) for n in d["niveis"]))


class FonteEventos(Protocol):
    @property
    def lacunas(self) -> int: ...

    def receber(self) -> tuple[str, dict[str, Any]] | None: ...
    def fechar(self) -> None: ...


class FonteZMQ:
    """SUB do gateway (nao bloqueante); conta lacunas de sequencia."""

    def __init__(self, endereco: str, topicos: list[str] | None = None) -> None:
        from robowin.core.barramento import AssinanteZMQ
        self._s = AssinanteZMQ(endereco, topicos or TOPICOS)

    @property
    def lacunas(self) -> int:
        return int(self._s.lacunas)

    def receber(self) -> tuple[str, dict[str, Any]] | None:
        return self._s.receber(timeout_ms=0)

    def fechar(self) -> None:
        self._s.fechar()


class FonteLista:
    """Fonte em memoria (testes): entrega mensagens ja prontas; ``lacunas`` pode ser alterado pelo teste."""

    def __init__(self, mensagens: Iterable[tuple[str, dict[str, Any]]]) -> None:
        self._it = iter(mensagens)
        self.lacunas: int = 0

    def receber(self) -> tuple[str, dict[str, Any]] | None:
        return next(self._it, None)

    def fechar(self) -> None:
        return None


class Nucleo:
    def __init__(self, orq: Orquestrador, fonte: FonteEventos, *, heartbeat: Callable[[], None] | None = None,
                 intervalo_hb_s: float = 1.0, hb_gateway_max_s: float = 15.0, cooperar_s: float = 0.005) -> None:
        self.orq, self.fonte, self.heartbeat = orq, fonte, heartbeat
        self.intervalo_hb_s, self.hb_gateway_max_s, self.cooperar_s = intervalo_hb_s, hb_gateway_max_s, cooperar_s
        self._lacunas_vistas = 0
        self._ult_hb_gw = time.monotonic()
        self._prox_hb = 0.0
        self.mensagens = 0
        self.estado_gateway: dict[str, Any] = {}
        self._marcou_duble = False
        self.parar = asyncio.Event()

    async def tratar(self, topico: str, d: dict[str, Any]) -> None:
        o = self.orq
        self.mensagens += 1
        if topico == "tick":
            if d.get("backfill"):  # historico recuperado: nao e dado "ao vivo" - segura entradas por precaucao
                o.bloqueio.marcar("backfill", o._agora(), "tick de backfill")
                return
            await o.on_tick(tick_de_dict(d))
        elif topico == "livro":
            o.on_livro(livro_de_dict(d))
        elif topico == "heartbeat":
            self._ult_hb_gw = time.monotonic()
        elif topico == "estado":
            self.estado_gateway = d
            if d.get("duble") and not self._marcou_duble:  # dados do DUBLE sao sinteticos: acende a faixa no painel
                self._marcou_duble = True
                o.registro.incidente(t_ns=o._agora(), origem=ORIGEM_SINTETICO, severidade="info",
                                     detalhe="gateway em modo DUBLE: dados SINTETICOS (nao e mercado)")
            if d.get("feed_parado") or d.get("lacuna_pendente"):
                o.bloqueio.marcar("feed_gateway", o._agora(), "gateway reporta feed parado/lacuna")
        elif topico == "alerta":
            o.registro.incidente(t_ns=o._agora(), origem="gateway", severidade="alerta",
                                 detalhe=str(d.get("tipo", ""))[:200])

    def _verificar_integridade(self) -> None:
        o = self.orq
        agora = o._agora()
        if self.fonte.lacunas > self._lacunas_vistas:
            n = self.fonte.lacunas - self._lacunas_vistas
            self._lacunas_vistas = self.fonte.lacunas
            o.bloqueio.lacuna_sequencia(agora, f"{n} lacuna(s) de sequencia no PUB/SUB")
            o.registro.incidente(t_ns=agora, origem="nucleo", severidade="alerta",
                                 detalhe=f"lacuna de sequencia ({n}): entradas bloqueadas")
        if time.monotonic() - self._ult_hb_gw > self.hb_gateway_max_s:
            o.bloqueio.marcar("heartbeat_gateway", agora, "sem heartbeat do gateway")

    async def executar(self, duracao_s: float | None = None) -> None:
        ini = time.monotonic()
        while not self.parar.is_set():
            n = 0
            while n < 1000 and (m := self.fonte.receber()) is not None:
                await self.tratar(*m)
                n += 1
            self._verificar_integridade()
            await self.orq.on_tempo()
            agora = time.monotonic()
            if self.heartbeat is not None and agora >= self._prox_hb:
                self._prox_hb = agora + self.intervalo_hb_s
                self.heartbeat()
            if duracao_s is not None and agora - ini >= duracao_s:
                break
            if n == 0:
                await asyncio.sleep(self.cooperar_s)
