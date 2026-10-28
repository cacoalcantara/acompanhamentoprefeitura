"""Laco AO VIVO do market making na conta DEMO (``robowin mm ao-vivo``; SOMENTE demo, atras de ``[mm].demo_mm``).

Consome ticks/livros do gateway (ZeroMQ), calcula o estado do ``CotadorMM`` (o mesmo ``estado_mm`` da simulacao),
cota a cada ``intervalo_s`` e aplica as mensagens pela ``PonteMMDemo``. Envia o heartbeat ao gateway (sem ele o
guardiao bloqueia e depois zera). Dado velho (ou backfill) => cancela as cotacoes e nao recota.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable

from ..core.relogio import Relogio
from ..core.tipos import LivroSnapshot, Tick
from ..decisao.nucleo import FonteEventos, livro_de_dict, tick_de_dict
from .cotacao import CotadorMM
from .ponte_demo import PonteMMDemo
from .simulacao import estado_mm

NS = 1_000_000_000


class MMAoVivo:
    def __init__(self, ponte: PonteMMDemo, cotador: CotadorMM, fonte: FonteEventos, relogio: Relogio, *,
                 intervalo_s: float = 1.0, dado_max_s: float = 5, heartbeat: Callable[[], None] | None = None,
                 intervalo_hb_s: float = 1.0, cooperar_s: float = 0.005) -> None:
        self.ponte, self.cotador, self.fonte, self.rel = ponte, cotador, fonte, relogio
        self.intervalo_s, self.dado_max_s, self.heartbeat = intervalo_s, dado_max_s, heartbeat
        self.intervalo_hb_s, self.cooperar_s = intervalo_hb_s, cooperar_s
        self.mids: deque[float] = deque(maxlen=60)
        self.fluxo: deque[float] = deque(maxlen=10)
        self.ultimo_tick: Tick | None = None
        self.livro: LivroSnapshot | None = None
        self._t_tick_mono = 0.0
        self._prox = 0.0
        self._prox_hb = 0.0
        self._lacunas = 0
        self.parar = asyncio.Event()
        self.ciclos = 0

    def tratar(self, topico: str, d: dict) -> None:  # type: ignore[type-arg]
        if topico == "tick":
            if d.get("backfill"):
                return
            tk = tick_de_dict(d)
            self.ultimo_tick, self._t_tick_mono = tk, time.monotonic()
            if tk.flags & 32:
                self.fluxo.append(1.0)
            elif tk.flags & 64:
                self.fluxo.append(-1.0)
        elif topico == "livro":
            self.livro = livro_de_dict(d)
        elif topico == "estado":
            self.ponte.ao_estado_gateway(d)

    def passo(self) -> None:
        t = self.rel.agora_ns()
        p = self.ponte
        if p.verificar_kill(t) or p._bloqueio():
            p.cancelar_cotacoes("bloqueio_da_ponte", t)
            return
        if self.fonte.lacunas > self._lacunas or self.ultimo_tick is None or time.monotonic() - self._t_tick_mono > self.dado_max_s:
            self._lacunas = self.fonte.lacunas
            p.cancelar_cotacoes("dado_velho_ou_lacuna", t)  # sem dado fresco nao se cota
            return
        p.sincronizar(t)
        estado = estado_mm(self.ultimo_tick, self.livro, self.mids, self.fluxo, p.estoque, self.intervalo_s)
        if estado is None:
            return
        self.cotador.cotar(t, estado)
        self.ciclos += 1
        p.aplicar(t)

    async def executar(self, duracao_s: float | None = None) -> None:
        ini = time.monotonic()
        try:
            while not self.parar.is_set():
                n = 0
                while n < 1000 and (m := self.fonte.receber()) is not None:
                    self.tratar(*m)
                    n += 1
                agora = time.monotonic()
                if self.heartbeat is not None and agora >= self._prox_hb:
                    self._prox_hb = agora + self.intervalo_hb_s
                    self.heartbeat()
                if agora >= self._prox:
                    self._prox = agora + self.intervalo_s
                    self.passo()
                if duracao_s is not None and agora - ini >= duracao_s:
                    break
                if n == 0:
                    await asyncio.sleep(self.cooperar_s)
        finally:
            self.ponte.encerrar(self.rel.agora_ns())
