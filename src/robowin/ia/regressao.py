"""Regressao estatistica de modelos: bateria fixa de casos, k repeticoes, faixas de tolerancia.

Serve para detectar mudanca silenciosa sob o mesmo alias (ex.: GPT-6, sem snapshot datado): sinaliza
mudanca do modelo servido e queda de taxa de validade/concordancia fora das faixas.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import Acao, PropostaIA
from robowin.ia.dossie import montar_dossie
from robowin.ia.prompts import carregar_prompt, mensagem_proponente
from robowin.ia.validadores import validar_proposta


@dataclass(frozen=True)
class Caso:
    id: str
    dossie: dict[str, Any]
    acoes_aceitas: frozenset[Acao]
    modo: str = "imediato"


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


async def rodar_regressao(
    provedor: Any, modelo_ref: str, *, perfil: PerfilInstrumento, casos: list[Caso] | None = None, k: int = 5, faixas: Faixas | None = None,
    fixavel: bool = True, servido_anterior: str | None = None, prazo_s: float = 40.0, effort: str | None = None,
    max_tokens: int = 4096, sistema: str | None = None,
) -> ResultadoRegressao:
    casos = casos if casos is not None else bateria_padrao(perfil)
    faixas = faixas or Faixas()
    sistema = sistema if sistema is not None else carregar_prompt("proponente").texto
    _, modelo = modelo_ref.split(":", 1)
    por_caso: dict[str, ResultadoCaso] = {}
    servidos: Counter[str] = Counter()
    custo = 0.0
    for caso in casos:
        rc = ResultadoCaso(caso.id)
        msg = mensagem_proponente(caso.dossie, caso.modo)
        for _ in range(k):
            obj, ch = await provedor.gerar_estruturado(
                modelo=modelo, sistema=sistema, mensagem=msg, schema=PropostaIA, prazo_s=prazo_s,
                effort=effort, max_tokens=max_tokens)
            rc.n += 1
            custo += ch.custo_usd
            if ch.modelo_servido:
                servidos[ch.modelo_servido] += 1
            if not ch.valida or not isinstance(obj, PropostaIA):
                continue
            rc.validas += 1
            if validar_proposta(obj, caso.dossie, modo=caso.modo, stop_max_pontos=perfil.stop_max_pontos).ok:
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
