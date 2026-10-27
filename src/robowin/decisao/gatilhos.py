"""Gatilhos de decisao (plano 5.2, passo 1): periodico ou por evento.

Eventos: limiar do modelo quant (E[R$ liq] acima do limiar), z-score do StatArb, noticia relevante e evento
de posicao (abertura/fechamento). A CADENCIA sai do uso medido de tokens (custo medio por ciclo, media
movel) e do orcamento de IA: o teto diario de chamadas e ``floor(verba restante do dia / custo medio)`` e o
periodo efetivo espalha as chamadas restantes pelo que falta do pregao. O INTERVALO MINIMO entre chamadas e
fixo. Nada aqui depende de IA: e codigo deterministico, testavel com relogio simulado.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from robowin.core.tipos import Base
from robowin.ia.orcamento import OrcamentoIA
from robowin.risco.tempo import dia_sp

NS = 1_000_000_000


class ConfigGatilhos(Base):
    periodo_s: float = 240.0  # cadencia periodica base (~4 min)
    intervalo_min_s: float = 10.0  # FIXO: nenhuma chamada antes disto, venha de onde vier
    rearme_evento_s: float = 60.0  # um mesmo evento (limiar/z-score) so re-dispara depois disto
    e_brl_min: float = 1.0  # limiar do modelo: max(E[R$ liq] compra, venda) acima disto
    z_statarb_min: float = 2.0
    noticia_relevancia_min: float = 0.7
    evento_posicao: bool = True
    max_chamadas_dia: int | None = 400  # teto fixo opcional (alem do derivado do orcamento)
    custo_medio_inicial_usd: float = 0.02
    suavizacao_custo: float = 0.2  # peso da observacao nova na media movel do custo por ciclo


@dataclass(frozen=True)
class EstadoGatilho:
    t_ns: int
    modelo_saida: dict[str, float] = field(default_factory=dict)
    z_statarb: float = 0.0
    noticias_novas: Sequence[float] = ()  # relevancia das noticias que ficaram disponiveis desde a ultima avaliacao
    eventos_posicao: Sequence[str] = ()
    segundos_restantes: float = 3600.0  # ate o fim da janela de entradas


@dataclass(frozen=True)
class Disparo:
    nome: str  # periodico | limiar_modelo | zscore_statarb | noticia_relevante | evento_posicao
    detalhe: str = ""


class GerenciadorGatilhos:
    def __init__(self, cfg: ConfigGatilhos | None = None, orcamento: OrcamentoIA | None = None) -> None:
        self.cfg = cfg or ConfigGatilhos()
        self.orcamento = orcamento
        self.custo_medio_usd = self.cfg.custo_medio_inicial_usd
        self.ultimo_ns: int | None = None
        self._ultimo_evento: dict[str, int] = {}
        self._dia = None
        self.chamadas_dia = 0
        self.bloqueios_orcamento = 0

    # ---- uso medido ------------------------------------------------------------------------------
    def registrar_custo(self, custo_usd: float) -> None:
        """Atualiza o custo medio por ciclo (media movel exponencial) com o custo REAL medido."""
        if custo_usd > 0:
            a = self.cfg.suavizacao_custo
            self.custo_medio_usd = (1 - a) * self.custo_medio_usd + a * custo_usd

    def _rolar_dia(self, t_ns: int) -> None:
        d = dia_sp(t_ns)
        if d != self._dia:
            self._dia = d
            self.chamadas_dia = 0

    def chamadas_restantes(self, t_ns: int) -> int:
        """Teto diario DERIVADO: o menor entre o teto fixo e o que a verba do dia ainda paga."""
        self._rolar_dia(t_ns)
        lim = math.inf
        if self.cfg.max_chamadas_dia is not None:
            lim = self.cfg.max_chamadas_dia - self.chamadas_dia
        if self.orcamento is not None:
            o = self.orcamento
            verba = min(o.cota_dia - o.gasto_hoje - o.reservado, o.disponivel_mes)
            lim = min(lim, math.floor(max(verba, 0.0) / max(self.custo_medio_usd, 1e-9)))
        return int(max(0, lim)) if lim != math.inf else 10**9

    def teto_diario(self, t_ns: int) -> int:
        """Total de chamadas permitidas hoje (ja feitas + restantes)."""
        r = self.chamadas_restantes(t_ns)
        return self.chamadas_dia + (r if r < 10**9 else 10**9)

    def intervalo_periodico_s(self, t_ns: int, segundos_restantes: float) -> float:
        """Periodo efetivo: nunca menor que o base nem que o minimo; espalha as chamadas que a verba paga."""
        restantes = self.chamadas_restantes(t_ns)
        if restantes <= 0:
            return math.inf
        espalhado = segundos_restantes / restantes if segundos_restantes > 0 else 0.0
        return max(self.cfg.periodo_s, self.cfg.intervalo_min_s, espalhado)

    # ---- avaliacao -------------------------------------------------------------------------------
    def avaliar(self, est: EstadoGatilho) -> tuple[Disparo, ...]:
        """Disparos neste instante (vazio = nao chamar). NAO consome a chamada: use ``marcar_chamada``."""
        t = est.t_ns
        if self.chamadas_restantes(t) <= 0:
            self.bloqueios_orcamento += 1
            return ()
        if self.ultimo_ns is not None and t - self.ultimo_ns < self.cfg.intervalo_min_s * NS:
            return ()
        out: list[Disparo] = []
        c = self.cfg
        if c.evento_posicao and est.eventos_posicao:
            out.append(Disparo("evento_posicao", ",".join(est.eventos_posicao)))
        if est.noticias_novas and max(est.noticias_novas) >= c.noticia_relevancia_min:
            out.append(Disparo("noticia_relevante", f"relevancia={max(est.noticias_novas):.2f}"))
        e = max(est.modelo_saida.get("e_brl_compra", -math.inf), est.modelo_saida.get("e_brl_venda", -math.inf))
        if e > c.e_brl_min and self._rearmado("limiar_modelo", t):
            out.append(Disparo("limiar_modelo", f"e_brl={e:.2f}"))
        if abs(est.z_statarb) >= c.z_statarb_min and self._rearmado("zscore_statarb", t):
            out.append(Disparo("zscore_statarb", f"z={est.z_statarb:.2f}"))
        per = self.intervalo_periodico_s(t, est.segundos_restantes)
        if not out and (self.ultimo_ns is None or t - self.ultimo_ns >= per * NS):
            out.append(Disparo("periodico", f"intervalo={per:.0f}s"))
        for d in out:
            if d.nome in ("limiar_modelo", "zscore_statarb"):
                self._ultimo_evento[d.nome] = t
        return tuple(out)

    def _rearmado(self, nome: str, t_ns: int) -> bool:
        u = self._ultimo_evento.get(nome)
        return u is None or t_ns - u >= self.cfg.rearme_evento_s * NS

    def marcar_chamada(self, t_ns: int) -> None:
        self._rolar_dia(t_ns)
        self.ultimo_ns = t_ns
        self.chamadas_dia += 1
