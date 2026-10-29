"""Bracos de CONTROLE (plano 5.4): nao operar e entradas aleatorias com a mesma frequencia e geometria.

* ``BracoNaoOperar``: nunca entra; mede o que acontece sem fazer nada (custo zero).
* ``BracoAleatorio``: entradas aleatorias (lado sorteado) com a MESMA frequencia e geometria dos bracos de
  referencia. A frequencia pode ser fixa (``frequencia_por_hora``) ou seguir uma funcao
  (``frequencia_fn(t_ns)``, que o orquestrador liga a taxa observada nos bracos de referencia); as
  geometrias sao sorteadas das ja observadas (``observar``) ou do padrao. ``seed`` torna tudo reprodutivel.
* ``BracoAleatorioPlano`` (docs/16 WP6, H2): copia o plano COMPILADO inteiro de uma entrada do planejador
  (gatilhos, faixa, gestao, tempo e quantidade) e sorteia o lado com a seed. Com o lado trocado, os valores de preco
  sao invertidos (``preco_rompe_acima`` <-> ``preco_rompe_abaixo``, ``preco_toca`` com o sinal trocado). A frequencia
  e a do braco de referencia: sem ``frequencia_fn``, UMA entrada sorteada por plano observado (fila, no proximo ciclo
  sem posicao); com ``frequencia_fn``, a taxa dela, sorteando entre os planos observados. Nunca copia ajustes da IA.
"""
from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from robowin.core.tipos import Acao, Condicao, Intencao, PlanoCondicional

from ..plano_condicional import converter_plano
from .base import DESVIO_MAX_TICKS, NS, BracoBase, ContextoDecisao, Geometria, Modo, ResultadoDecisao

if TYPE_CHECKING:
    from ..compilador_plano import PlanoCompilado


class BracoNaoOperar(BracoBase):
    familia = "controle"

    def __init__(self, id: str = "nao_operar", modo: Modo = "imediato") -> None:
        super().__init__(id, modo, Geometria(0.0, 0.0, 0))  # nunca opera: a geometria nao se aplica

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=("controle_nao_operar",))


class BracoAleatorio(BracoBase):
    familia = "controle"

    def __init__(self, id: str = "controle_aleatorio", modo: Modo = "imediato", *, seed: int = 0,
                 frequencia_por_hora: float = 2.0, geometrias: Sequence[Geometria],
                 frequencia_fn: Callable[[int], float] | None = None, validade_s: float = 60.0,
                 desvio_max_ticks: float = DESVIO_MAX_TICKS, rompimento_ticks: float = 2.0,
                 expira_plano_s: float = 180.0) -> None:
        super().__init__(id, modo, geometrias[0])
        self.seed = seed
        self._rng = random.Random(seed)
        self.frequencia_por_hora = frequencia_por_hora
        self.frequencia_fn = frequencia_fn
        self._geoms: list[Geometria] = list(geometrias)
        self._padrao = len(self._geoms)
        self.validade_s, self.desvio_max_ticks = validade_s, desvio_max_ticks
        self.rompimento_ticks, self.expira_plano_s = rompimento_ticks, expira_plano_s
        self._ultimo_t: int | None = None

    def manifesto(self) -> dict[str, Any]:
        m = super().manifesto()
        m.update(seed=self.seed, frequencia_por_hora=self.frequencia_por_hora,
                 frequencia_dinamica=self.frequencia_fn is not None)
        return m

    def observar(self, intencao: Intencao) -> None:
        """Registra a geometria de uma entrada de um braco de referencia (para copiar a mesma geometria)."""
        if intencao.stop_pontos and intencao.alvo_pontos:
            g = Geometria(float(intencao.stop_pontos), float(intencao.alvo_pontos), int(intencao.tempo_max_s or 60))
            if g not in self._geoms[self._padrao:]:
                self._geoms.append(g)

    def _geometrias_ativas(self) -> list[Geometria]:
        """Observadas (se houver) substituem o padrao: a geometria copiada e a dos bracos de referencia."""
        return self._geoms[self._padrao:] or self._geoms[: self._padrao]

    def _taxa_por_hora(self, t_ns: int) -> float:
        if self.frequencia_fn is not None:
            return max(0.0, float(self.frequencia_fn(t_ns)))
        return self.frequencia_por_hora

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        # os sorteios sao SEMPRE consumidos (mesmo com posicao aberta): o fluxo aleatorio so depende da seed
        # e do numero de ciclos, nao do desfecho das operacoes.
        u, u_lado, u_geom = self._rng.random(), self._rng.random(), self._rng.random()
        dt_h = 0.0 if self._ultimo_t is None else max(0, ctx.t0_ns - self._ultimo_t) / NS / 3600.0
        self._ultimo_t = ctx.t0_ns
        taxa = self._taxa_por_hora(ctx.t0_ns)
        p = 1.0 - math.exp(-taxa * dt_h)
        kw: dict[str, Any] = {"validade_s": self.validade_s, "desvio_max_pts": self.desvio_max_ticks * ctx.tick_pontos}
        base: dict[str, Any] = {"origem": "controle_aleatorio", "seed": self.seed, "taxa_por_hora": taxa,
                                "p_ciclo": p}
        if (ctx.posicao.quantidade or 0) > 0 or u >= p:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=("sorteio_sem_entrada",),
                                   proposta=base, **kw)
        compra = u_lado < 0.5
        geoms = self._geometrias_ativas()
        g = geoms[min(int(u_geom * len(geoms)), len(geoms) - 1)]
        acao = Acao.COMPRAR if compra else Acao.VENDER
        base.update(lado="compra" if compra else "venda", stop_pontos=g.stop_pontos, alvo_pontos=g.alvo_pontos)
        plano = None
        if self.modo == "condicional":  # rompimento a favor do lado sorteado
            rb = self.rompimento_ticks * ctx.tick_pontos
            rel = PlanoCondicional(
                gatilhos=(Condicao(tipo="preco_rompe_acima" if compra else "preco_rompe_abaixo",
                                   valor=rb if compra else -rb),),
                invalidacoes=(Condicao(tipo="preco_rompe_abaixo" if compra else "preco_rompe_acima",
                                       valor=-2 * rb if compra else 2 * rb),),
                expira_ns=int(self.expira_plano_s * NS))
            plano = converter_plano(rel, ctx.t0_ns)
            kw["plano"] = plano
        validade_ns = plano.expira_ns if plano is not None else ctx.t0_ns + int(self.validade_s * NS)
        it = self._intencao(ctx, acao, stop=g.stop_pontos, alvo=g.alvo_pontos, tempo_max_s=g.tempo_max_s,
                            validade_ns=validade_ns, plano=plano)
        return self._resultado(ctx, acao, "plano" if plano is not None else "entrada", intencao=it,
                               proposta=base, **kw)


# --------------------------------------------------------------------------------------------------
# controle do plano (braco planejador)
# --------------------------------------------------------------------------------------------------
MAX_PLANOS_OBSERVADOS = 64


def espelhar_condicao(c: Condicao) -> Condicao:
    """Condicao para o lado OPOSTO: rompe_acima(+v) <-> rompe_abaixo(-v); toca(v) -> toca(-v); spread e janela
    de tempo nao dependem do lado."""
    valor = None if c.valor is None else -c.valor
    if c.tipo == "preco_rompe_acima":
        return Condicao(tipo="preco_rompe_abaixo", valor=valor, ate_ns=c.ate_ns)
    if c.tipo == "preco_rompe_abaixo":
        return Condicao(tipo="preco_rompe_acima", valor=valor, ate_ns=c.ate_ns)
    if c.tipo == "preco_toca":
        return Condicao(tipo="preco_toca", valor=valor, ate_ns=c.ate_ns)
    return c


def _identidade(c: Condicao) -> Condicao:
    return c


class BracoAleatorioPlano(BracoAleatorio):
    """Controle do planejador: o plano compilado de P com o lado sorteado (mede direcao e momento, H2)."""

    def __init__(self, id: str = "controle_aleatorio_plano", *, referencia: str = "planejador", seed: int = 0,
                 frequencia_fn: Callable[[int], float] | None = None) -> None:
        super().__init__(id, "condicional", seed=seed, geometrias=(Geometria(0.0, 0.0, 0),),
                         frequencia_por_hora=0.0, frequencia_fn=frequencia_fn, desvio_max_ticks=math.inf)
        self.referencia = referencia
        self._planos: deque[PlanoCompilado] = deque(maxlen=MAX_PLANOS_OBSERVADOS)
        self._pendentes: deque[PlanoCompilado] = deque(maxlen=MAX_PLANOS_OBSERVADOS)

    def manifesto(self) -> dict[str, Any]:
        m = super().manifesto()
        m.update(referencia=self.referencia, sem_geometria_fixa=True,
                 frequencia="taxa" if self.frequencia_fn is not None else "um_por_plano_observado")
        return m

    def observar(self, intencao: Intencao) -> None:
        """A geometria nao se aplica: o controle copia planos inteiros (``observar_plano``)."""

    def observar_plano(self, c: PlanoCompilado) -> None:
        """Registra o plano compilado de uma entrada ENVIADA do braco de referencia."""
        self._planos.append(c)
        self._pendentes.append(c)

    def _escolher(self, u_ent: float, u_plano: float, dt_h: float, t_ns: int) -> PlanoCompilado | None:
        if self.frequencia_fn is None:
            return self._pendentes.popleft() if self._pendentes else None
        if not self._planos:
            return None
        p = 1.0 - math.exp(-self._taxa_por_hora(t_ns) * dt_h)
        if u_ent >= p:
            return None
        return self._planos[min(int(u_plano * len(self._planos)), len(self._planos) - 1)]

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        # sorteios SEMPRE consumidos (o fluxo aleatorio so depende da seed e do numero de ciclos)
        u_ent, u_lado, u_plano = self._rng.random(), self._rng.random(), self._rng.random()
        dt_h = 0.0 if self._ultimo_t is None else max(0, ctx.t0_ns - self._ultimo_t) / NS / 3600.0
        self._ultimo_t = ctx.t0_ns
        base: dict[str, Any] = {"origem": "controle_aleatorio_plano", "seed": self.seed, "referencia": self.referencia}
        kw: dict[str, Any] = {"desvio_max_pts": math.inf}
        if (ctx.posicao.quantidade or 0) > 0:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=("posicao_aberta",), proposta=base, **kw)
        c = self._escolher(u_ent, u_plano, dt_h, ctx.t0_ns)
        if c is None:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=("sorteio_sem_entrada",),
                                   proposta=base, **kw)
        ref = c.intencao_base
        compra = u_lado < 0.5
        acao = Acao.COMPRAR if compra else Acao.VENDER
        trocou = acao != ref.acao
        conv = espelhar_condicao if trocou else _identidade
        duracao_ns = max(c.plano_abs.expira_ns - ref.t0_ns, NS)
        plano = PlanoCondicional(gatilhos=tuple(conv(g) for g in c.plano_abs.gatilhos),
                                 invalidacoes=tuple(conv(i) for i in c.plano_abs.invalidacoes),
                                 expira_ns=ctx.t0_ns + duracao_ns)
        base.update(lado="compra" if compra else "venda", lado_trocado=trocou, plano_de=ref.id,
                    stop_pontos=ref.stop_pontos, alvo_pontos=ref.alvo_pontos, quantidade=ref.quantidade)
        it = self._intencao(ctx, acao, stop=ref.stop_pontos, alvo=ref.alvo_pontos, tempo_max_s=ref.tempo_max_s,
                            validade_ns=plano.expira_ns, plano=plano, quantidade=ref.quantidade,
                            gestao=c.gestao.model_copy(update={"reavaliar_em_s": None, "reavaliar_se": ()}),
                            ancora_t0=ctx.preco_ref)
        return self._resultado(ctx, acao, "plano", intencao=it, proposta=base, plano=plano,
                               validade_s=duracao_ns / NS, **kw)
