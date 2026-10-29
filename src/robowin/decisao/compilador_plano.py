"""Compilador do ``PlanoTradeIA`` (braco planejador, docs/16 WP5) para a DSL de plano condicional que ja existe.

A IA entrega a decisao inteira em PONTOS, UNIDADES e SEGUNDOS; aqui ela vira, por codigo puro e deterministico:

* ``intencao_base``: Intencao condicional (quantidade, stop, alvo, tempo, gestao compilada, ``ancora_t0`` e
  ``p_alvo``) com validade = expiracao do plano;
* ``plano_abs``: ``PlanoCondicional`` ABSOLUTO (expiracao em ns). A faixa de execucao assimetrica vira
  invalidacoes relativas a referencia de t0 (compra: ``preco_rompe_acima(+favor)`` e ``preco_rompe_abaixo(-contra)``;
  venda: o espelho). Sem gatilhos ("agora") o gatilho e ``janela_tempo(expira_s)``: entra no primeiro tick depois de
  armado, se o preco ainda estiver dentro da faixa;
* ``gestao``: ``PlanoGestaoCompilado`` (regras na grade do tick, executadas a cada tick por decisao/gestao.py);
* ``resumo``: o plano compilado SEM precos absolutos (vai ao revisor do plano).

Arredondamento sempre conservador: stop e alvo para BAIXO na grade (stop mais curto, alvo mais perto) e a faixa
para dentro. Nao conhece limites: este modulo nao importa ``robowin.risco`` e nunca le ``ConfigRisco``; o envelope
(derivado, so leitura) e usado so para descrever o plano. O veto e sempre do motor de risco.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from robowin.core.tipos import (
    Acao,
    Condicao,
    ConfigPlanejador,
    EnvelopeDecisao,
    Intencao,
    PlanoCondicional,
    PlanoGestaoCompilado,
    PlanoTradeIA,
    RegraCompilada,
    RegraGestao,
)

from .dsl_plano import CONDICOES_PRECO, descrever

NS = 1_000_000_000
VERSAO_COMPILADOR = 1
REAVALIAR_SE_VALIDOS = ("perto_do_stop", "perto_do_alvo", "metade_do_tempo", "regra_disparou", "lucro_acima_stop")
_EPS = 1e-9


class ErroCompilacao(ValueError):
    """Plano que nao pode ser compilado (o validador da IA deveria te-lo recusado antes)."""


@dataclass(frozen=True)
class PlanoCompilado:
    intencao_base: Intencao
    plano_abs: PlanoCondicional
    gestao: PlanoGestaoCompilado
    resumo: dict[str, Any]


# --------------------------------------------------------------------------------------------------
# grade do tick
# --------------------------------------------------------------------------------------------------
def _finito(nome: str, v: float | None) -> None:
    if v is not None and not math.isfinite(float(v)):
        raise ErroCompilacao(f"{nome} nao finito")


def para_baixo(pontos: float, tick: float) -> float:
    """Maior multiplo do tick <= pontos (sem ruido de ponto flutuante)."""
    return round(math.floor(pontos / tick + _EPS) * tick, 10)


def para_cima(pontos: float, tick: float) -> float:
    """Menor multiplo do tick >= pontos."""
    return round(math.ceil(pontos / tick - _EPS) * tick, 10)


def mais_proximo(pontos: float, tick: float) -> float:
    return round(round(pontos / tick) * tick, 10)


def passo_minimo(cfg: ConfigPlanejador, tick_pontos: float, espelhado: bool) -> float:
    ticks = cfg.passo_min_ticks_espelhado if espelhado else cfg.passo_min_ticks
    return round(max(int(ticks), 1) * tick_pontos, 10)


# --------------------------------------------------------------------------------------------------
# gestao
# --------------------------------------------------------------------------------------------------
def _compilar_regra(r: RegraGestao, tick: float) -> RegraCompilada:
    for nome in ("mfe_pontos", "distancia_pontos"):
        _finito(nome, getattr(r, nome))
    mfe = None if r.mfe_pontos is None else max(para_cima(float(r.mfe_pontos), tick), 0.0)
    dist = None
    if r.distancia_pontos is not None:
        if r.distancia_pontos <= 0:
            raise ErroCompilacao(f"{r.tipo}: distancia_pontos deve ser positiva")
        dist = max(para_baixo(float(r.distancia_pontos), tick), tick)
    if r.apos_s is not None and r.apos_s < 0:
        raise ErroCompilacao(f"{r.tipo}: apos_s negativo")
    if r.folga_ticks is not None and r.folga_ticks < 0:
        raise ErroCompilacao(f"{r.tipo}: folga_ticks negativa")
    if r.quantidade is not None and r.quantidade < 1:
        raise ErroCompilacao(f"{r.tipo}: quantidade deve ser >= 1")
    if r.extensao_s is not None and r.extensao_s < 0:
        raise ErroCompilacao(f"{r.tipo}: extensao_s negativa")
    if r.tipo in ("trailing", "apertar_apos") and dist is None:
        raise ErroCompilacao(f"{r.tipo}: distancia_pontos obrigatoria")
    if r.tipo in ("sem_progresso", "apertar_apos") and r.apos_s is None:
        raise ErroCompilacao(f"{r.tipo}: apos_s obrigatorio")
    if r.tipo == "parcial" and r.quantidade is None:
        raise ErroCompilacao("parcial: quantidade obrigatoria")
    acao_venc = r.acao_vencimento
    if r.tipo == "ao_vencer_tempo" and acao_venc is None:
        acao_venc = "encerrar"
    folga = None if r.folga_ticks is None else round(int(r.folga_ticks) * tick, 10)
    if r.tipo == "breakeven" and folga is None:
        folga = 0.0
    return RegraCompilada(tipo=r.tipo, mfe_pontos=mfe, apos_s=r.apos_s, distancia_pontos=dist, folga_pontos=folga,
                          quantidade=r.quantidade, acao_vencimento=acao_venc, extensao_s=r.extensao_s)


def compilar_gestao(regras: Sequence[RegraGestao], *, tick_pontos: float, cfg: ConfigPlanejador,
                    espelhado: bool, reavaliar_em_s: int | None, reavaliar_se: Sequence[str]) -> PlanoGestaoCompilado:
    """Regras da IA -> regras na grade do tick (mfe para cima, distancias para baixo e >= 1 tick, folga em pontos).

    ``passo_min_pontos = (espelhado ? passo_min_ticks_espelhado : passo_min_ticks) x tick``; ``reavaliar_em_s`` nunca
    abaixo de ``cfg.reavaliar_min_s``; ``reavaliar_se`` so com os nomes conhecidos, sem repeticao."""
    if not (tick_pontos > 0 and math.isfinite(tick_pontos)):
        raise ErroCompilacao("tick_pontos invalido")
    if len(regras) > cfg.max_regras:
        raise ErroCompilacao(f"regras de gestao acima do maximo ({len(regras)} > {cfg.max_regras})")
    comp = tuple(_compilar_regra(r, tick_pontos) for r in regras)
    rav = None if reavaliar_em_s is None else max(int(reavaliar_em_s), int(cfg.reavaliar_min_s))
    rse = tuple(dict.fromkeys(s for s in reavaliar_se if s in REAVALIAR_SE_VALIDOS))
    return PlanoGestaoCompilado(regras=comp, passo_min_pontos=passo_minimo(cfg, tick_pontos, espelhado),
                                intervalo_ajuste_s=float(cfg.intervalo_ajuste_s), reavaliar_em_s=rav,
                                reavaliar_se=rse, versao_dsl=1)


# --------------------------------------------------------------------------------------------------
# plano
# --------------------------------------------------------------------------------------------------
def _gatilho(c: Condicao, tick: float) -> Condicao:
    if c.ate_ns is not None:
        raise ErroCompilacao("ate_ns proibido no planejador (use janela_tempo com valor)")
    _finito(f"gatilho {c.tipo}", c.valor)
    if c.tipo in CONDICOES_PRECO:
        return Condicao(tipo=c.tipo, valor=mais_proximo(float(c.valor or 0.0), tick))
    if c.tipo == "janela_tempo" and (c.valor is None or c.valor <= 0):
        raise ErroCompilacao("janela_tempo exige valor positivo (segundos)")
    return c


def faixa_como_invalidacoes(compra: bool, contra: float, favor: float) -> tuple[Condicao, Condicao]:
    """Faixa de execucao -> invalidacoes relativas a ref_t0 (o preco nao pode sair da faixa antes de entrar)."""
    if compra:
        return (Condicao(tipo="preco_rompe_acima", valor=favor), Condicao(tipo="preco_rompe_abaixo", valor=-contra))
    return (Condicao(tipo="preco_rompe_abaixo", valor=-favor), Condicao(tipo="preco_rompe_acima", valor=contra))


def compilar_plano(p: PlanoTradeIA, *, ref_t0: float, t0_ns: int, tick_pontos: float,
                   env: EnvelopeDecisao, cfg: ConfigPlanejador, braco: str, ciclo_id: str,
                   espelhado: bool = False, custo_ida_volta_pontos: float = 0.0) -> PlanoCompilado:
    """Compila uma ENTRADA do planejador (COMPRAR/VENDER). Levanta ``ErroCompilacao`` em plano incompilavel."""
    if p.acao not in (Acao.COMPRAR, Acao.VENDER):
        raise ErroCompilacao(f"so entradas sao compiladas (acao={p.acao})")
    if not (tick_pontos > 0 and math.isfinite(tick_pontos)):
        raise ErroCompilacao("tick_pontos invalido")
    if not (math.isfinite(ref_t0) and ref_t0 > 0):
        raise ErroCompilacao("ref_t0 invalido")
    for nome in ("stop_pontos", "alvo_pontos", "faixa_contra_pontos", "faixa_favor_pontos"):
        _finito(nome, getattr(p, nome))
    if p.stop_pontos is None or p.alvo_pontos is None:
        raise ErroCompilacao("stop_pontos e alvo_pontos obrigatorios")
    stop = para_baixo(float(p.stop_pontos), tick_pontos)
    alvo = para_baixo(float(p.alvo_pontos), tick_pontos)
    if stop < tick_pontos or alvo < tick_pontos:
        raise ErroCompilacao("stop e alvo precisam ter ao menos um tick")
    if p.quantidade < 1:
        raise ErroCompilacao("quantidade deve ser >= 1")
    if p.tempo_max_s is not None and p.tempo_max_s <= 0:
        raise ErroCompilacao("tempo_max_s deve ser positivo")
    contra = para_baixo(max(float(p.faixa_contra_pontos or 0.0), 0.0), tick_pontos)
    favor = para_baixo(max(float(p.faixa_favor_pontos or 0.0), 0.0), tick_pontos)
    expira_s = min(int(p.expira_s), int(cfg.expira_max_s))
    if expira_s <= 0:
        raise ErroCompilacao("expira_s deve ser positivo")
    compra = p.acao == Acao.COMPRAR
    gatilhos = tuple(_gatilho(c, tick_pontos) for c in p.gatilhos) or (
        Condicao(tipo="janela_tempo", valor=float(expira_s)),)
    expira_ns = int(t0_ns) + expira_s * NS
    plano_abs = PlanoCondicional(gatilhos=gatilhos, invalidacoes=faixa_como_invalidacoes(compra, contra, favor),
                                 expira_ns=expira_ns)
    gestao = compilar_gestao(p.gestao, tick_pontos=tick_pontos, cfg=cfg, espelhado=espelhado,
                             reavaliar_em_s=p.reavaliar_em_s, reavaliar_se=p.reavaliar_se)
    it = Intencao(
        id=f"{ciclo_id}:{braco}", braco=braco, acao=p.acao, quantidade=int(p.quantidade), stop_pontos=stop,
        alvo_pontos=alvo, tempo_max_s=p.tempo_max_s, validade_ns=expira_ns, modo="condicional", plano=plano_abs,
        preco_referencia=float(ref_t0), t0_ns=int(t0_ns), gestao=gestao, ancora_t0=float(ref_t0), p_alvo=p.p_alvo)
    return PlanoCompilado(intencao_base=it, plano_abs=plano_abs, gestao=gestao,
                          resumo=resumo_plano(it, gestao, env=env, contra=contra, favor=favor, expira_s=expira_s,
                                              gatilhos=gatilhos, tick_pontos=tick_pontos,
                                              custo_ida_volta_pontos=custo_ida_volta_pontos,
                                              expor=cfg.expor_envelope))


def acerto_equilibrio_pontos(stop: float, alvo: float, custo: float) -> float | None:
    """Acerto minimo para empatar liquido: (stop + custo) / (stop + alvo), tudo em pontos por unidade."""
    den = stop + alvo
    return None if den <= 0 else round((stop + custo) / den, 4)


def resumo_plano(it: Intencao, gestao: PlanoGestaoCompilado, *, env: EnvelopeDecisao, contra: float, favor: float,
                 expira_s: int, gatilhos: Sequence[Condicao], tick_pontos: float,
                 custo_ida_volta_pontos: float, expor: str = "envelope") -> dict[str, Any]:
    """Plano compilado SEM precos absolutos (so pontos relativos, unidades e segundos): vai ao revisor do plano.

    Com ``expor="nenhum"`` o envelope do resumo fica reduzido (como no dossie): so ``entradas_possiveis``,
    ``stop_min_pontos`` e ``dist_min_ajuste_pontos``."""
    stop = float(it.stop_pontos or 0.0)
    alvo = float(it.alvo_pontos or 0.0)
    q = it.quantidade
    stop_max_q = env.stop_max_pontos_por_q[q - 1] if 1 <= q <= len(env.stop_max_pontos_por_q) else None
    return {
        "versao_compilador": VERSAO_COMPILADOR,
        "acao": it.acao.value,
        "quantidade": q,
        "stop_pontos": stop,
        "alvo_pontos": alvo,
        "stop_resultado_pontos": -stop,
        "alvo_resultado_pontos": alvo,
        "tempo_max_s": it.tempo_max_s,
        "faixa_contra_pontos": contra,
        "faixa_favor_pontos": favor,
        "stop_no_pior_caso_da_faixa_pontos": round(stop + favor, 10),
        "alvo_no_pior_caso_da_faixa_pontos": round(alvo - favor, 10),
        "gatilhos": [descrever(c) for c in gatilhos],
        "expira_s": expira_s,
        "gestao": [r.model_dump(exclude_none=True) for r in gestao.regras],
        "passo_min_pontos": gestao.passo_min_pontos,
        "reavaliar_em_s": gestao.reavaliar_em_s,
        "reavaliar_se": list(gestao.reavaliar_se),
        "p_alvo": it.p_alvo,
        "custo_ida_volta_pontos": round(custo_ida_volta_pontos, 10),
        "acerto_equilibrio": acerto_equilibrio_pontos(stop, alvo, custo_ida_volta_pontos),
        "tick_pontos": tick_pontos,
        "envelope": envelope_resumido(env) if expor == "nenhum" else {
            "unidades_max": env.unidades_max, "stop_max_pontos_para_q": stop_max_q,
            "stop_min_pontos": env.stop_min_pontos, "dist_min_ajuste_pontos": env.dist_min_ajuste_pontos,
            "tempo_max_s": env.tempo_max_s, "entradas_possiveis": env.entradas_possiveis},
    }


def envelope_resumido(env: EnvelopeDecisao) -> dict[str, Any]:
    """Versao ``expor_envelope="nenhum"``: so o que nao revela limite (docs/16 1.3)."""
    return {"entradas_possiveis": env.entradas_possiveis, "stop_min_pontos": env.stop_min_pontos,
            "dist_min_ajuste_pontos": env.dist_min_ajuste_pontos}
