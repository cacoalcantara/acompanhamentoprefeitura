"""Base dos bracos experimentais (plano 5.4): contexto, resultado e a classe-base.

O Protocolo ``core.protocolos.Braco`` e sincrono (``decidir(t_ns, dossie) -> Intencao | None``). Os bracos
daqui o cumprem, mas a API principal e assincrona (``adecidir``), porque os bracos de IA esperam chamadas
de rede com prazo: o orquestrador usa ``adecidir`` e recebe um ``ResultadoDecisao`` completo (intencao,
proposta, revisao, chamadas de LLM, carimbos de latencia, motivos de bloqueio).

Todo braco tem os dois MODOS pre-registrados: ``imediato`` e ``condicional`` (variantes distintas, com ids
distintos). Nenhum braco conhece o motor de risco: ele so propoe; o veto e do ``MotorRisco``.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Coroutine
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from robowin.core.tipos import (
    Acao,
    CarimboLatencia,
    ChamadaLLM,
    EnvelopeDecisao,
    Intencao,
    PlanoCondicional,
    Posicao,
)

NS = 1_000_000_000
DESVIO_MAX_TICKS = 4.0  # desvio maximo do preco entre a decisao e o envio, em TICKS do ativo
Modo = Literal["imediato", "condicional"]
MODOS: tuple[Modo, ...] = ("imediato", "condicional")


@dataclass(frozen=True)
class Geometria:
    """Geometria da operacao: distancias em pontos e tempo maximo (coerente com a rotulagem do modelo)."""
    stop_pontos: float
    alvo_pontos: float
    tempo_max_s: int = 60


@dataclass
class ContextoDecisao:
    """Instantaneo t0 entregue a um braco: o dossie ANONIMIZADO (o unico texto que vai ao LLM) e os dados
    absolutos que so o codigo deterministico usa (preco de referencia, cotacao, posicao)."""
    t0_ns: int
    dossie: dict[str, Any]
    hash_dossie: str
    preco_ref: float
    bid: float
    ask: float
    posicao: Posicao
    ciclo_id: str
    tick_pontos: float  # do perfil do ativo
    gatilhos: tuple[str, ...] = ()
    # Maquina do Tempo (decisao/odds.py): callable (ctx, lado, stop_pontos, alvo_pontos) -> AvaliacaoOdds | None
    odds: Any = None
    # adicoes opcionais (braco planejador; docs/16). O braco ve o envelope DERIVADO dos limites (pontos, unidades,
    # segundos), nunca o ConfigRisco: os demais bracos ignoram estes campos.
    envelope: EnvelopeDecisao | None = None
    custo_ida_volta_pontos: float = 0.0
    plano_vigente: bool = False  # ha um plano condicional armado deste braco
    latencia_ia_s: tuple[float, float] | None = None  # (p50, p95) da latencia observada do papel de IA


@dataclass
class RegistroChamada:
    """Uma ChamadaLLM a gravar. ``braco`` e o braco a que o custo e atribuido (dono do servico)."""
    chamada: ChamadaLLM
    braco: str
    papel: str
    compartilhada: bool = False  # True: outro braco ja a gravou (chamada Claude compartilhada B/C)


@dataclass
class ResultadoDecisao:
    braco: str
    modo: str
    t0_ns: int
    ciclo_id: str
    acao: Acao
    # entrada | plano | saida | manter | ajuste | manter_sem_ajuste | nao_operar | bloqueada
    resultado: str
    intencao: Intencao | None = None
    proposta: dict[str, Any] | None = None
    revisao: dict[str, Any] | None = None
    motivos: tuple[str, ...] = ()
    chamadas: list[RegistroChamada] = field(default_factory=list)
    carimbos: list[CarimboLatencia] = field(default_factory=list)
    rotulos: tuple[str, ...] = ()
    latencia_ns: int = 0  # de t0 ate o resultado estar pronto (soma das etapas de IA)
    hash_dossie: str = ""
    preco_ref: float = 0.0
    validade_s: float = 60.0
    desvio_max_pts: float = float("inf")  # sempre preenchido por `_resultado` (ticks do ativo x tick)
    plano: PlanoCondicional | None = None  # ABSOLUTO (ver plano_condicional.converter_plano)
    gatilhos: tuple[str, ...] = ()
    # adicoes opcionais: posicao vista em t0 (MANTER/ENCERRAR presos a ela) e pedidos de reavaliacao do planejador
    posicao_ticket_t0: int | None = None
    quantidade_t0: int = 0
    reavaliar_em_s: int | None = None
    reavaliar_se: tuple[str, ...] = ()
    p_alvo: float | None = None

    @property
    def eh_entrada(self) -> bool:
        return self.acao in (Acao.COMPRAR, Acao.VENDER)


def executar_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """Executa uma corrotina de dentro de codigo sincrono (inclusive com um loop ja rodando)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


class BracoBase:
    """Esqueleto comum. Subclasses implementam ``adecidir``."""

    ia: bool = False
    familia: str = "base"

    def __init__(self, id: str, modo: Modo = "imediato", geometria: Geometria | None = None) -> None:
        if modo not in MODOS:
            raise ValueError(f"modo invalido: {modo}")
        self.id = id
        self.modo: Modo = modo
        if geometria is None:
            raise ValueError("geometria obrigatoria: derive-a do perfil do ativo (stop/alvo em ticks)")
        self.geometria = geometria
        self._ctx: ContextoDecisao | None = None

    # --- API assincrona (usada pelo orquestrador) ----------------------------------------------------
    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:  # pragma: no cover - abstrato
        raise NotImplementedError

    # --- Protocolo core.protocolos.Braco (sincrono) -----------------------------------------------------
    def definir_contexto(self, ctx: ContextoDecisao) -> None:
        self._ctx = ctx

    def decidir(self, t_ns: int, dossie: dict[str, Any]) -> Intencao | None:
        if self._ctx is None or self._ctx.t0_ns != t_ns:
            raise RuntimeError("definir_contexto(ctx) com t0 == t_ns antes de decidir()")
        res: ResultadoDecisao = executar_sync(self.adecidir(replace(self._ctx, dossie=dossie)))
        return res.intencao

    # --- identificacao -------------------------------------------------------------------------------
    @property
    def grupo_ciclo(self) -> str:
        """Bracos do mesmo grupo decidem no MESMO ciclo (mesmo gatilho e t0): B e C compartilham o Claude."""
        return self.id

    def manifesto(self) -> dict[str, Any]:
        return {"id": self.id, "familia": self.familia, "modo": self.modo, "ia": self.ia,
                "geometria": {"stop_pontos": self.geometria.stop_pontos, "alvo_pontos": self.geometria.alvo_pontos,
                              "tempo_max_s": self.geometria.tempo_max_s}}

    # --- auxiliares ----------------------------------------------------------------------------------
    def _resultado(self, ctx: ContextoDecisao, acao: Acao, resultado: str, **kw: Any) -> ResultadoDecisao:
        kw.setdefault("desvio_max_pts", DESVIO_MAX_TICKS * ctx.tick_pontos)
        kw.setdefault("posicao_ticket_t0", ctx.posicao.ticket)
        kw.setdefault("quantidade_t0", ctx.posicao.quantidade)
        return ResultadoDecisao(braco=self.id, modo=self.modo, t0_ns=ctx.t0_ns, ciclo_id=ctx.ciclo_id, acao=acao,
                                resultado=resultado, hash_dossie=ctx.hash_dossie, preco_ref=ctx.preco_ref,
                                gatilhos=ctx.gatilhos, **kw)

    def _intencao(self, ctx: ContextoDecisao, acao: Acao, *, stop: float | None, alvo: float | None,
                  tempo_max_s: int | None, validade_ns: int, plano: PlanoCondicional | None = None,
                  sufixo: str = "", proposta_id: str | None = None, revisao_id: str | None = None,
                  modo_intencao: Literal["imediato", "condicional", "quant"] | None = None, quantidade: int = 1,
                  **extras: Any) -> Intencao:
        """``extras``: campos opcionais da Intencao (posicao_ticket, quantidade_t0, gestao, ajuste, ancora_t0,
        p_alvo). Sem eles a Intencao e identica a de antes."""
        return Intencao(
            id=f"{ctx.ciclo_id}:{self.id}{sufixo}", braco=self.id, acao=acao, quantidade=quantidade,
            stop_pontos=stop, alvo_pontos=alvo, tempo_max_s=tempo_max_s, validade_ns=validade_ns,
            modo=modo_intencao or self.modo, plano=plano, preco_referencia=ctx.preco_ref,
            proposta_id=proposta_id, revisao_id=revisao_id, t0_ns=ctx.t0_ns, **extras)

    def _bloqueada(self, ctx: ContextoDecisao, motivos: tuple[str, ...], **kw: Any) -> ResultadoDecisao:
        return self._resultado(ctx, kw.pop("acao", Acao.NAO_OPERAR), "bloqueada", motivos=motivos, **kw)
