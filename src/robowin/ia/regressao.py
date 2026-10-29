"""Regressao estatistica de modelos: bateria fixa de casos, k repeticoes, faixas de tolerancia.

Serve para detectar mudanca silenciosa sob o mesmo alias (ex.: GPT-6, sem snapshot datado): sinaliza
mudanca do modelo servido e queda de taxa de validade/concordancia fora das faixas.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import Acao, ConfigPlanejador, EnvelopeDecisao, PlanoTradeIA, PropostaIA
from robowin.ia.dossie import montar_dossie
from robowin.ia.prompts import carregar_prompt, mensagem_planejador, mensagem_proponente
from robowin.ia.validadores import ResultadoValidacao, validar_plano_trade, validar_proposta

PAPEIS_REGRESSAO = ("proponente", "planejador")


@dataclass(frozen=True)
class Caso:
    id: str
    dossie: dict[str, Any]
    acoes_aceitas: frozenset[Acao]
    modo: str = "imediato"
    envelope: EnvelopeDecisao | None = None  # so no papel planejador (envelope sintetico do caso)


@dataclass(frozen=True)
class Faixas:
    taxa_valida_min: float = 0.9
    concordancia_modal_min: float = 0.6
    taxa_acao_aceita_min: float = 0.8


@dataclass
class ResultadoCaso:
    caso: str
    n: int = 0
    validas: int = 0
    regras_ok: int = 0
    acoes: dict[str, int] = field(default_factory=dict)
    aceitas: int = 0

    @property
    def concordancia_modal(self) -> float:
        tot = sum(self.acoes.values())
        return max(self.acoes.values()) / tot if tot else 0.0


@dataclass
class ResultadoRegressao:
    modelo: str
    fixavel: bool
    k: int
    por_caso: dict[str, ResultadoCaso]
    modelos_servidos: tuple[str, ...]
    mudou_modelo_servido: bool
    alias_nao_fixavel: bool
    violacoes: list[str]
    custo_usd: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.violacoes


def _ticks_sinteticos(perfil: PerfilInstrumento, drift_ticks_por_s: float, ruido_ticks: float, n: int = 1000
                      ) -> list[tuple[int, float]]:
    """Serie deterministica (sem aleatoriedade) para a bateria fixa, na escala (tick e preco) do ativo."""
    tk = perfil.tick_pontos
    out = []
    for i in range(n):
        osc = ((i * 37) % 11 - 5) * ruido_ticks * tk
        out.append((i * 1_000_000_000, perfil.preco_sintetico_ref + drift_ticks_por_s * tk * i + osc))
    return out


def bateria_padrao(perfil: PerfilInstrumento) -> list[Caso]:
    t0 = 999 * 1_000_000_000
    tk, base = perfil.tick_pontos, perfil.preco_sintetico_ref
    lim = {"contratos_restantes": 1, "perda_restante_dia_brl": 200.0}
    sem = frozenset({Acao.NAO_OPERAR, Acao.COMPRAR, Acao.VENDER})
    casos = [
        Caso("lateral_calmo", montar_dossie(t0_ns=t0, ticks=_ticks_sinteticos(perfil, 0.0, 0.2), spread_pontos=tk, tick_pontos=tk,
                                            regime="lateral", limites=lim, minutos_desde_abertura=120,
                                            minutos_para_zeragem=300),
             frozenset({Acao.NAO_OPERAR})),
        Caso("tendencia_alta_sem_posicao", montar_dossie(t0_ns=t0, ticks=_ticks_sinteticos(perfil, 0.16, 0.2), spread_pontos=tk, tick_pontos=tk,
                                                         regime="tendencia", limites=lim, minutos_desde_abertura=90,
                                                         minutos_para_zeragem=330), sem),
        Caso("dados_ausentes", montar_dossie(t0_ns=t0, ticks=[(t0 - 1_000_000_000, base), (t0, base + tk)], tick_pontos=tk,
                                              regime="indefinido", limites=lim),
             frozenset({Acao.NAO_OPERAR})),
        Caso("posicao_comprada_perdendo", montar_dossie(
            t0_ns=t0, ticks=_ticks_sinteticos(perfil, -0.12, 0.2), spread_pontos=tk, tick_pontos=tk, regime="tendencia", limites=lim,
            posicao={"lado": "compra", "quantidade": 1, "preco_medio": base, "aberta_ns": t0 - 300_000_000_000,
                     "stop_pontos": 24 * tk, "alvo_pontos": 40 * tk}),
             frozenset({Acao.ENCERRAR, Acao.MANTER})),
    ]
    return casos


def _envelope_sintetico(perfil: PerfilInstrumento, *, entradas_possiveis: bool = True) -> EnvelopeDecisao:
    """Envelope fixo da bateria (pontos/unidades/segundos, na escala do ativo); nunca vem de limites reais."""
    tk = perfil.tick_pontos
    return EnvelopeDecisao(
        unidades_max=1, stop_max_pontos_por_q=(perfil.stop_max_pontos,), stop_min_pontos=2 * tk,
        dist_min_ajuste_pontos=tk, tempo_max_s=300, entradas_possiveis=entradas_possiveis,
        motivos_sem_entrada=() if entradas_possiveis else ("perdas_seguidas",))


def _extras_planejador(perfil: PerfilInstrumento, env: EnvelopeDecisao, *, posicao: dict[str, Any] | None = None,
                       min_para_zeragem: int = 300) -> dict[str, Any]:
    tk = perfil.tick_pontos
    custo = 2 * tk
    env_d = env.model_dump(mode="json")
    return {
        "instrumento": {"tick_pontos": tk, "stop_dispara_por": "ultimo"},
        "custos": {"ida_volta_pontos_base": custo, "ida_volta_pontos_estresse": custo, "meio_spread_pontos": tk / 2,
                   "slippage_stop_medio_pontos": tk, "fonte_slippage": "reserva_perfil",
                   "formula_equilibrio": "(stop + custo + slippage_stop) / (stop + alvo)"},
        "envelope": env_d,
        "sessao": {"min_para_fim_entradas": max(0, min_para_zeragem - 15), "min_para_evento": None},
        "mercado": {"feed_ok": True, "leilao": False, "vol_extrema": False},
        "posicao": posicao or {"lado": None, "quantidade": 0},
        "plano_vigente": None,
        "retorno": {"ultima_decisao": "nenhuma"},
        "latencia_ia": {"p50_s": 8, "p95_s": 15},
    }


def _posicao_v2(perfil: PerfilInstrumento, ticks: list[tuple[int, float]], *, entrada: float, stop_ticks: int,
                alvo_ticks: int, aberta_s: int) -> dict[str, Any]:
    tk = perfil.tick_pontos
    resultado = round(ticks[-1][1] - entrada, 4)
    return {"lado": "compra", "quantidade": 1, "quantidade_inicial": 1, "resultado_pontos": resultado,
            "stop_resultado_pontos": -stop_ticks * tk, "alvo_resultado_pontos": alvo_ticks * tk,
            "tempo_aberta_s": aberta_s, "tempo_max_s": 300, "tempo_restante_s": 300 - aberta_s,
            "regras": [], "ajustes": 0, "parciais": 0}


def bateria_planejador(perfil: PerfilInstrumento) -> list[Caso]:
    """Bateria fixa do papel ``planejador`` (dossie v2 com envelope, custos e posicao sinteticos)."""
    t0 = 999 * 1_000_000_000
    tk = perfil.tick_pontos
    env, env_fechado = _envelope_sintetico(perfil), _envelope_sintetico(perfil, entradas_possiveis=False)

    def dossie(ticks: list[tuple[int, float]], env_c: EnvelopeDecisao, regime: str, *,
               posicao: dict[str, Any] | None = None, preco_medio: float | None = None) -> dict[str, Any]:
        pos_v1 = None if preco_medio is None else {"lado": "compra", "quantidade": 1, "preco_medio": preco_medio,
                                                     "aberta_ns": t0 - 120 * 1_000_000_000}
        return montar_dossie(t0_ns=t0, ticks=ticks, spread_pontos=tk, tick_pontos=tk, regime=regime,
                             minutos_desde_abertura=120, minutos_para_zeragem=300, posicao=pos_v1, versao=2,
                             extras=_extras_planejador(perfil, env_c, posicao=posicao))

    lateral, alta = _ticks_sinteticos(perfil, 0.0, 1), _ticks_sinteticos(perfil, 0.16, 1)
    queda, subida = _ticks_sinteticos(perfil, -0.12, 1), _ticks_sinteticos(perfil, 0.12, 1)
    entrada_q, entrada_s = queda[-120][1], subida[-120][1]
    sem = frozenset({Acao.NAO_OPERAR, Acao.COMPRAR, Acao.VENDER})
    gestao = frozenset({Acao.ENCERRAR, Acao.MANTER})
    return [
        Caso("plan_lateral_calmo", dossie(lateral, env, "lateral"), frozenset({Acao.NAO_OPERAR}), "condicional", env),
        Caso("plan_tendencia_sem_posicao", dossie(alta, env, "tendencia"), sem, "condicional", env),
        Caso("plan_sem_entrada_possivel", dossie(alta, env_fechado, "tendencia"), frozenset({Acao.NAO_OPERAR}),
             "condicional", env_fechado),
        Caso("plan_posicao_comprada_perdendo",
             dossie(queda, env, "tendencia", preco_medio=entrada_q,
                    posicao=_posicao_v2(perfil, queda, entrada=entrada_q, stop_ticks=24, alvo_ticks=40, aberta_s=120)),
             gestao, "condicional", env),
        Caso("plan_posicao_comprada_ganhando",
             dossie(subida, env, "tendencia", preco_medio=entrada_s,
                    posicao=_posicao_v2(perfil, subida, entrada=entrada_s, stop_ticks=24, alvo_ticks=40, aberta_s=120)),
             gestao, "condicional", env),
    ]


def _validar_planejador(obj: PlanoTradeIA, caso: Caso, perfil: PerfilInstrumento,
                        cfg: ConfigPlanejador) -> ResultadoValidacao:
    custos = caso.dossie.get("custos") or {}
    return validar_plano_trade(
        obj, caso.dossie, env=caso.envelope, cfg=cfg, tick_pontos=perfil.tick_pontos,
        custo_ida_volta_pontos=float(custos.get("ida_volta_pontos_estresse") or 0),
        slip_stop_medio_pontos=float(custos.get("slippage_stop_medio_pontos") or 0))


async def rodar_regressao(
    provedor: Any, modelo_ref: str, *, perfil: PerfilInstrumento, casos: list[Caso] | None = None, k: int = 5, faixas: Faixas | None = None,
    fixavel: bool = True, servido_anterior: str | None = None, prazo_s: float = 40.0, effort: str | None = None,
    max_tokens: int = 4096, sistema: str | None = None, papel: str = "proponente",
    cfg_planejador: ConfigPlanejador | None = None,
) -> ResultadoRegressao:
    """``papel``: ``proponente`` (PropostaIA, dossie v1) ou ``planejador`` (PlanoTradeIA, dossie v2 e
    ``validar_plano_trade`` com o envelope sintetico de cada caso)."""
    if papel not in PAPEIS_REGRESSAO:
        raise ValueError(f"papel sem bateria de regressao: {papel}")
    plan = papel == "planejador"
    casos = casos if casos is not None else (bateria_planejador(perfil) if plan else bateria_padrao(perfil))
    faixas = faixas or Faixas()
    cfg_plan = cfg_planejador or ConfigPlanejador()
    esquema: type[PropostaIA] | type[PlanoTradeIA] = PlanoTradeIA if plan else PropostaIA
    sistema = sistema if sistema is not None else carregar_prompt(papel).texto
    _, modelo = modelo_ref.split(":", 1)
    por_caso: dict[str, ResultadoCaso] = {}
    servidos: Counter[str] = Counter()
    custo = 0.0
    for caso in casos:
        rc = ResultadoCaso(caso.id)
        msg = mensagem_planejador(caso.dossie) if plan else mensagem_proponente(caso.dossie, caso.modo)
        for _ in range(k):
            obj, ch = await provedor.gerar_estruturado(
                modelo=modelo, sistema=sistema, mensagem=msg, schema=esquema, prazo_s=prazo_s,
                effort=effort, max_tokens=max_tokens)
            rc.n += 1
            custo += ch.custo_usd
            if ch.modelo_servido:
                servidos[ch.modelo_servido] += 1
            if not ch.valida or not isinstance(obj, esquema):
                continue
            rc.validas += 1
            if isinstance(obj, PlanoTradeIA):
                rc.regras_ok += _validar_planejador(obj, caso, perfil, cfg_plan).ok
            elif validar_proposta(obj, caso.dossie, modo=caso.modo, stop_max_pontos=perfil.stop_max_pontos).ok:
                rc.regras_ok += 1
            rc.acoes[obj.acao.value] = rc.acoes.get(obj.acao.value, 0) + 1
            rc.aceitas += obj.acao in caso.acoes_aceitas
        por_caso[caso.id] = rc
    viol: list[str] = []
    lista_servidos = tuple(sorted(servidos))
    mudou = len(lista_servidos) > 1 or (
        servido_anterior is not None and bool(lista_servidos) and lista_servidos[0] != servido_anterior)
    if mudou:
        viol.append(f"modelo servido mudou sob o mesmo alias: {lista_servidos} (anterior: {servido_anterior})")
    for rc in por_caso.values():
        if rc.n and rc.validas / rc.n < faixas.taxa_valida_min:
            viol.append(f"{rc.caso}: taxa de respostas validas {rc.validas}/{rc.n} abaixo da faixa")
        if rc.validas and rc.concordancia_modal < faixas.concordancia_modal_min:
            viol.append(f"{rc.caso}: concordancia modal {rc.concordancia_modal:.2f} abaixo da faixa")
        if rc.validas and rc.aceitas / rc.validas < faixas.taxa_acao_aceita_min:
            viol.append(f"{rc.caso}: acoes aceitas {rc.aceitas}/{rc.validas} abaixo da faixa")
    return ResultadoRegressao(
        modelo=modelo_ref, fixavel=fixavel, k=k, por_caso=por_caso, modelos_servidos=lista_servidos,
        mudou_modelo_servido=mudou, alias_nao_fixavel=not fixavel, violacoes=viol, custo_usd=custo)
