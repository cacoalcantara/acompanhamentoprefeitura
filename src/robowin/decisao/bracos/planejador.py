"""Braco PLANEJADOR (docs/16 WP6): um unico papel de IA plugavel decide o trade inteiro (``PlanoTradeIA``).

* ``ServicoPlanejamento``: papel ``planejador`` (qualquer ``provedor:modelo``), esquema ``PlanoTradeIA``, mensagem
  ``mensagem_planejador`` e validacao deterministica por ``ia.validadores.validar_plano_trade`` contra o envelope
  DERIVADO do contexto (``ctx.envelope``: pontos, unidades, segundos). O cache ``(ciclo, modo, hash_dossie)`` do
  ``ServicoProposta`` e reaproveitado: P e PC com o mesmo dossie fazem UMA chamada pareada (um so custo).
* ``ServicoRevisaoPlano``: papel ``revisor_plano`` (``RevisaoPlanoIA``); recebe o plano JA COMPILADO
  (``PlanoCompilado.resumo``: niveis em pontos, equilibrio e custo), nunca a resposta bruta.
* ``BracoPlanejador``: familia ``planejador``, sempre no modo condicional, ``Geometria(0, 0, 0)`` como sentinela (o
  manifesto marca ``sem_geometria_fixa``). Mapeia a resposta para ``ResultadoDecisao``:

  ===========================================  ==================================================================
  resposta                                     resultado
  ===========================================  ==================================================================
  NAO_OPERAR                                   ``nao_operar``
  MANTER sem ajuste                            ``manter_sem_ajuste`` (so registrado; sem intencao)
  MANTER com ajuste                            ``ajuste`` (Intencao MANTER com ``ajuste``, ticket e quantidade de t0)
  ENCERRAR                                     ``saida`` (Intencao ENCERRAR presa ao ticket visto em t0)
  entrada com posicao                          ``bloqueada:posicao_aberta_do_braco``
  entrada com manter_plano_vigente + plano     ``nao_operar:plano_vigente_mantido``
  demais entradas                              ``compilar_plano`` -> revisor opcional (PC) -> ``plano``
  ===========================================  ==================================================================

Nada aqui conhece o motor de risco nem le limites (o modulo nao importa ``robowin.risco``): o braco so propoe e o
veto e sempre do ``MotorRiscoPadrao``. Qualquer falha de IA, de validacao ou de compilacao BLOQUEIA a entrada;
saidas e protecao seguem pelo codigo deterministico.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robowin.core.tipos import (
    Acao,
    CarimboLatencia,
    ChamadaLLM,
    ConfigPlanejador,
    PlanoTradeIA,
    RevisaoPlanoIA,
)
from robowin.ia.prompts import mensagem_planejador, mensagem_revisor_plano
from robowin.ia.validadores import revisao_plano_aprova, validar_plano_trade

from ..compilador_plano import ErroCompilacao, PlanoCompilado, compilar_gestao, compilar_plano
from ..validade import (
    DIVERGENCIA,
    PRAZOS_PLANEJADOR,
    RESPOSTA_INVALIDA,
    RESPOSTA_TARDIA,
    SEM_TEMPO_REVISAO,
    Prazos,
)
from .base import NS, BracoBase, ContextoDecisao, Geometria, Modo, RegistroChamada, ResultadoDecisao
from .llm import PropostaObtida, ServicoLLM, ServicoProposta

PAPEL_PLANEJADOR = "planejador"
PAPEL_REVISOR_PLANO = "revisor_plano"
GEOMETRIA_SENTINELA = Geometria(0.0, 0.0, 0)  # o planejador nao tem geometria fixa: a IA escolhe stop/alvo/tempo
MOTIVO_PLANO_VIGENTE = "plano_vigente_mantido"
MOTIVO_POSICAO_ABERTA = "posicao_aberta_do_braco"
MAX_COMPILADOS = 64  # planos compilados guardados por braco (sombras e controle do plano os pedem pelo id)


def _slip_stop_do_dossie(dossie: dict[str, Any]) -> float:
    """Slippage medio de stop (pontos) publicado no dossie v2 (``custos``); ausente ou invalido => 0."""
    custos = dossie.get("custos")
    v = custos.get("slippage_stop_medio_pontos") if isinstance(custos, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)) or v < 0:
        return 0.0
    return float(v)


# --------------------------------------------------------------------------------------------------
# servicos
# --------------------------------------------------------------------------------------------------
class ServicoPlanejamento(ServicoProposta):
    """Papel ``planejador``. Reaproveita o cache pareado e o registro de donos do ``ServicoProposta``; a resposta
    validada volta em ``PropostaObtida.plano_trade`` (``proposta`` fica None)."""

    def __init__(self, provedor: Any, ref: str, *, dono: str = "", cfg: ConfigPlanejador | None = None,
                 effort: str | None = None, max_tokens: int = 2500, diretorio_prompts: str | Path | None = None,
                 **kw: Any) -> None:
        ServicoLLM.__init__(self, provedor, ref, PAPEL_PLANEJADOR, PlanoTradeIA, effort=effort, max_tokens=max_tokens,
                            diretorio_prompts=diretorio_prompts, **kw)
        self._donos: dict[str, str] = {}
        if dono:
            self._donos[""] = dono
        self.stop_max_pontos = float("inf")  # nao usado: o planejador e validado contra o envelope do contexto
        self._cache = {}
        self.cfg = cfg or ConfigPlanejador()

    async def _buscar(self, ctx: ContextoDecisao, modo: Modo, prazos: Prazos) -> PropostaObtida:
        dono = self.dono_de(modo)
        obj, ch, motivo = await self.chamar(mensagem_planejador(ctx.dossie), prazos.prazo_proponente(),
                                            ctx.hash_dossie)
        if motivo or not isinstance(obj, PlanoTradeIA):
            return PropostaObtida(None, ch, motivo or RESPOSTA_INVALIDA, (), False, dono)
        v = validar_plano_trade(obj, ctx.dossie, env=ctx.envelope, cfg=self.cfg, tick_pontos=ctx.tick_pontos,
                                custo_ida_volta_pontos=ctx.custo_ida_volta_pontos,
                                slip_stop_medio_pontos=_slip_stop_do_dossie(ctx.dossie),
                                niveis_absolutos=(ctx.preco_ref, ctx.bid, ctx.ask))
        if not v.ok:  # reprovado pela validacao deterministica: a chamada conta como INVALIDA
            ch = ch.model_copy(update={"valida": False, "erro": "plano_invalido: " + "; ".join(v.violacoes)[:300]})
            return PropostaObtida(None, ch, RESPOSTA_INVALIDA, v.violacoes, False, dono)
        return PropostaObtida(None, ch, None, (), False, dono, plano_trade=obj)


@dataclass
class RevisaoPlanoObtida:
    revisao: RevisaoPlanoIA | None
    chamada: ChamadaLLM
    motivo: str | None


class ServicoRevisaoPlano(ServicoLLM):
    """Papel ``revisor_plano``: revisa o plano COMPILADO (so entradas)."""

    def __init__(self, provedor: Any, ref: str, **kw: Any) -> None:
        kw.setdefault("max_tokens", 800)
        super().__init__(provedor, ref, PAPEL_REVISOR_PLANO, RevisaoPlanoIA, **kw)

    async def revisar(self, ctx: ContextoDecisao, compilado: dict[str, Any], prazo_s: float) -> RevisaoPlanoObtida:
        obj, ch, motivo = await self.chamar(mensagem_revisor_plano(ctx.dossie, compilado), prazo_s, ctx.hash_dossie)
        if motivo or not isinstance(obj, RevisaoPlanoIA):
            return RevisaoPlanoObtida(None, ch, motivo or RESPOSTA_INVALIDA)
        return RevisaoPlanoObtida(obj, ch, None)


# --------------------------------------------------------------------------------------------------
# braco
# --------------------------------------------------------------------------------------------------
class BracoPlanejador(BracoBase):
    """P (sem revisor) ou PC (com ``revisor_plano``). Sempre condicional; sem geometria fixa."""

    ia = True
    familia = "planejador"

    def __init__(self, id: str, servico: ServicoPlanejamento, revisor: ServicoRevisaoPlano | None = None, *,
                 cfg: ConfigPlanejador | None = None, prazos: Prazos | None = None, espelhado: bool = False) -> None:
        super().__init__(id, "condicional", GEOMETRIA_SENTINELA)
        self.servico = servico
        self.revisor = revisor
        self.cfg = cfg or servico.cfg
        self.prazos = prazos or PRAZOS_PLANEJADOR
        self.espelhado = espelhado  # braco espelhado na DEMO: passo minimo de ajuste maior (passo_min_ticks_espelhado)
        self._compilados: OrderedDict[str, PlanoCompilado] = OrderedDict()
        servico.registrar_braco(id, self.modo)

    @property
    def grupo_ciclo(self) -> str:
        return f"planejador:{self.servico.chave}"

    def compilado(self, intencao_id: str) -> PlanoCompilado | None:
        """Plano compilado de uma entrada deste braco (para o controle do plano)."""
        return self._compilados.get(intencao_id)

    def _guardar(self, c: PlanoCompilado) -> None:
        self._compilados[c.intencao_base.id] = c
        while len(self._compilados) > MAX_COMPILADOS:
            self._compilados.popitem(last=False)

    def manifesto(self) -> dict[str, Any]:
        m = super().manifesto()
        m["sem_geometria_fixa"] = True
        m["proponente"] = {"papel": PAPEL_PLANEJADOR, "modelo": self.servico.ref, "effort": self.servico.effort,
                           "prompt_hash": self.servico.prompt.hash}
        m["revisor"] = (None if self.revisor is None else
                        {"papel": PAPEL_REVISOR_PLANO, "modelo": self.revisor.ref, "effort": self.revisor.effort,
                         "prompt_hash": self.revisor.prompt.hash})
        m["prazos"] = {"proponente_s": self.prazos.proponente_s, "revisor_s": self.prazos.revisor_s,
                       "validade_s": self.prazos.validade_s}
        m["planejador"] = self.cfg.model_dump(mode="json")
        m["espelhado"] = self.espelhado
        return m

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        pz = self.prazos
        kw: dict[str, Any] = {"validade_s": pz.validade_s, "desvio_max_pts": float("inf")}
        obt = await self.servico.obter(ctx, self.modo, pz)
        lat_ns = int(obt.chamada.total_ms * 1e6)
        chamadas = [RegistroChamada(obt.chamada, obt.dono, PAPEL_PLANEJADOR, compartilhada=obt.compartilhada)]
        carimbos = [CarimboLatencia(etapa="proposta_ia", braco=self.id, t_ini_ns=ctx.t0_ns, t_fim_ns=ctx.t0_ns + lat_ns,
                                    injetado=obt.chamada.modelo_servido.endswith("FALSO"))]
        kw.update(chamadas=chamadas, carimbos=carimbos, latencia_ns=lat_ns)
        p = obt.plano_trade
        if p is None:
            return self._bloqueada(ctx, (f"ia:{obt.motivo}", *obt.violacoes), **kw)
        proposta_json: dict[str, Any] = p.model_dump(mode="json") | {"compartilhada": obt.compartilhada,
                                                                     "dono_chamada": obt.dono}
        kw.update(proposta=proposta_json, reavaliar_em_s=p.reavaliar_em_s, reavaliar_se=tuple(p.reavaliar_se),
                  p_alvo=p.p_alvo)
        validade_ns = ctx.t0_ns + int(pz.validade_s * NS)
        pos = ctx.posicao
        com_posicao = (pos.quantidade or 0) > 0

        # --- acoes que NUNCA esperam revisao ------------------------------------------------------------------
        if p.acao == Acao.NAO_OPERAR:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", **kw)
        if p.acao == Acao.ENCERRAR:
            it = self._intencao(ctx, Acao.ENCERRAR, stop=None, alvo=None, tempo_max_s=None, validade_ns=validade_ns,
                                posicao_ticket=pos.ticket, quantidade_t0=pos.quantidade)
            return self._resultado(ctx, Acao.ENCERRAR, "saida", intencao=it, **kw)
        if p.acao == Acao.MANTER:
            if p.ajuste is None:
                return self._resultado(ctx, Acao.MANTER, "manter_sem_ajuste", **kw)
            gestao = None
            if p.ajuste.substituir_gestao is not None:
                try:
                    gestao = compilar_gestao(p.ajuste.substituir_gestao, tick_pontos=ctx.tick_pontos, cfg=self.cfg,
                                             espelhado=self.espelhado, reavaliar_em_s=p.reavaliar_em_s,
                                             reavaliar_se=p.reavaliar_se)
                except ErroCompilacao as e:
                    return self._bloqueada(ctx, (f"compilacao:{e}",), acao=Acao.MANTER, **kw)
            it = self._intencao(ctx, Acao.MANTER, stop=None, alvo=None, tempo_max_s=None, validade_ns=validade_ns,
                                ajuste=p.ajuste, posicao_ticket=pos.ticket, quantidade_t0=pos.quantidade,
                                gestao=gestao)
            return self._resultado(ctx, Acao.MANTER, "ajuste", intencao=it, **kw)
        if p.acao not in (Acao.COMPRAR, Acao.VENDER):  # fail-closed (o validador ja o recusaria)
            return self._bloqueada(ctx, ("acao_desconhecida",), **kw)

        # --- entradas ------------------------------------------------------------------------------------------
        if com_posicao:  # a chamada pareada nasceu de outro dossie; este braco ja tem posicao
            return self._bloqueada(ctx, (MOTIVO_POSICAO_ABERTA,), acao=p.acao, **kw)
        if p.manter_plano_vigente and ctx.plano_vigente:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=(MOTIVO_PLANO_VIGENTE,), **kw)
        if ctx.envelope is None:  # o validador ja recusa; defesa em profundidade
            return self._bloqueada(ctx, ("sem_envelope",), acao=p.acao, **kw)
        lat_s = lat_ns / NS
        if lat_s >= min(int(p.expira_s), int(self.cfg.expira_max_s)):
            return self._bloqueada(ctx, (f"ia:{RESPOSTA_TARDIA}", "plano_expirou_na_chegada"), acao=p.acao, **kw)
        try:
            c = compilar_plano(p, ref_t0=ctx.preco_ref, t0_ns=ctx.t0_ns, tick_pontos=ctx.tick_pontos,
                               env=ctx.envelope, cfg=self.cfg, braco=self.id, ciclo_id=ctx.ciclo_id,
                               espelhado=self.espelhado, custo_ida_volta_pontos=ctx.custo_ida_volta_pontos)
        except ErroCompilacao as e:
            return self._bloqueada(ctx, (f"compilacao:{e}",), acao=p.acao, **kw)
        proposta_json["compilado"] = c.resumo
        if self.revisor is not None:  # PC: revisao SO de entradas, antes de armar
            prazo = pz.prazo_revisor(lat_s)
            if prazo <= 0:
                return self._bloqueada(ctx, (f"ia:{SEM_TEMPO_REVISAO}",), acao=p.acao, **kw)
            rev = await self.revisor.revisar(ctx, c.resumo, prazo)
            lat_rev_ns = int(rev.chamada.total_ms * 1e6)
            chamadas.append(RegistroChamada(rev.chamada, self.id, PAPEL_REVISOR_PLANO))
            carimbos.append(CarimboLatencia(etapa="revisao_ia", braco=self.id, t_ini_ns=ctx.t0_ns + lat_ns,
                                            t_fim_ns=ctx.t0_ns + lat_ns + lat_rev_ns,
                                            injetado=rev.chamada.modelo_servido.endswith("FALSO")))
            kw["latencia_ns"] = lat_ns + lat_rev_ns
            if rev.revisao is not None:
                kw["revisao"] = rev.revisao.model_dump(mode="json")
            if rev.motivo:
                return self._bloqueada(ctx, (f"ia:{rev.motivo}",), acao=p.acao, **kw)
            if not revisao_plano_aprova(rev.revisao):
                return self._bloqueada(ctx, (DIVERGENCIA,), acao=p.acao, **kw)
        self._guardar(c)
        kw["plano"] = c.plano_abs
        kw["validade_s"] = (c.plano_abs.expira_ns - ctx.t0_ns) / NS
        return self._resultado(ctx, p.acao, "plano", intencao=c.intencao_base, **kw)
