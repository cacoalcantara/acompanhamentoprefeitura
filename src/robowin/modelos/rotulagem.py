"""Rotulagem por tripla barreira (alvo / stop / tempo) coerente com stop, alvo, tempo e CUSTOS.

Para cada instante de decisao t e cada lado, simula a operacao: entrada na primeira cotacao com
t >= t + atraso (compra paga o ask, venda recebe o bid), saida na primeira barreira tocada.
O rotulo so e CONHECIDO em `t_fim_ns` (toque da barreira ou fim do horizonte); o purgado do
walk-forward usa esse instante. Amostras cujo horizonte ultrapassa o fim dos dados nao sao rotuladas.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import numpy as np

from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import Tick

ALVO, STOP, TEMPO = 0, 1, 2
NOMES_CLASSES = ("alvo", "stop", "tempo")
NS = 1_000_000_000


@dataclass(frozen=True)
class ConfigBarreiras:
    alvo_pontos: float
    stop_pontos: float
    ponto_valor_brl: float
    tempo_s: float = 60.0
    atraso_ns: int = 300_000_000  # latencia ate a entrada
    quantidade: int = 1
    gap_max_s: float = 30.0  # lacuna maxima de cotacoes tolerada na entrada e na saida por tempo

    @classmethod
    def do_perfil(cls, perfil: PerfilInstrumento, **kw: Any) -> ConfigBarreiras:
        """Barreiras no tamanho padrao do ativo (em TICKS do perfil, convertidos em pontos)."""
        kw.setdefault("alvo_pontos", perfil.alvo_padrao_pontos)
        kw.setdefault("stop_pontos", perfil.stop_padrao_pontos)
        kw.setdefault("ponto_valor_brl", perfil.ponto_valor_brl)
        return cls(**kw)


@dataclass(frozen=True)
class Custos:
    tarifas_corretagem_brl: float  # ida e volta, por contrato
    slippage_stop_pontos: float  # saida a mercado no stop paga 1 tick adicional

    @classmethod
    def do_perfil(cls, perfil: PerfilInstrumento, d: dt.date | None = None) -> Custos:
        """Tarifas B3 do perfil (2 lados, vigentes em `d`; padrao: a mais recente) ou comissao por lote (forex/CFD)
        + 1 tick de slippage no stop."""
        return cls(tarifas_corretagem_brl=2 * perfil.custo_lado_unidade(d), slippage_stop_pontos=perfil.tick_pontos)


@dataclass(frozen=True)
class Cotacoes:
    t_ns: np.ndarray
    bid: np.ndarray
    ask: np.ndarray

    @staticmethod
    def de_ticks(ticks: list[Tick]) -> Cotacoes:
        q = [(t.t_utc_ns, t.bid, t.ask) for t in ticks if t.bid > 0 and t.ask > 0 and t.ask >= t.bid]
        if not q:
            return Cotacoes(np.zeros(0, np.int64), np.zeros(0), np.zeros(0))
        a = np.array(q, dtype=float)
        return Cotacoes(a[:, 0].astype(np.int64), a[:, 1], a[:, 2])


@dataclass(frozen=True)
class Rotulo:
    lado: int  # +1 compra, -1 venda
    classe: int  # ALVO / STOP / TEMPO
    t_decisao_ns: int
    t_entrada_ns: int
    t_fim_ns: int  # instante em que o rotulo passa a ser conhecido
    pnl_pontos: float
    pnl_liquido_brl: float


def pnl_brl(pontos: float, cfg: ConfigBarreiras, custos: Custos) -> float:
    return pontos * cfg.ponto_valor_brl * cfg.quantidade - custos.tarifas_corretagem_brl * cfg.quantidade


def acerto_equilibrio(cfg: ConfigBarreiras, custos: Custos) -> float:
    """Taxa de acerto de equilibrio ignorando saidas por tempo: p*(alvo) = (stop_efetivo + c)/(alvo + stop_efetivo)."""
    c = custos.tarifas_corretagem_brl / (cfg.ponto_valor_brl * cfg.quantidade)  # custo fixo em pontos
    perda = cfg.stop_pontos + custos.slippage_stop_pontos
    return (perda + c) / (cfg.alvo_pontos + perda)


def rotular_um(q: Cotacoes, t_decisao_ns: int, lado: int, cfg: ConfigBarreiras, custos: Custos) -> Rotulo | None:
    """Rotulo da operacao; None se o horizonte ultrapassa os dados (rotulo ainda desconhecido)."""
    i0 = int(np.searchsorted(q.t_ns, t_decisao_ns + cfg.atraso_ns, side="left"))
    if i0 >= len(q.t_ns):
        return None
    t_ent = int(q.t_ns[i0])
    if t_ent - t_decisao_ns > cfg.atraso_ns + int(cfg.gap_max_s * NS):
        return None  # sem cotacao perto do instante de entrada (fim de sessao)
    t_end = t_ent + int(cfg.tempo_s * NS)
    if q.t_ns[-1] < t_end:
        return None  # sem dados ate o fim do horizonte
    j1 = int(np.searchsorted(q.t_ns, t_end, side="right"))  # cotacoes com t <= t_end
    if t_end - int(q.t_ns[j1 - 1]) > int(cfg.gap_max_s * NS):
        return None  # lacuna no fim do horizonte (sessao encerrou): nao rotula
    if lado > 0:
        entrada = q.ask[i0]
        saida_px = q.bid[i0 + 1:j1]
        hit_alvo = saida_px >= entrada + cfg.alvo_pontos
        hit_stop = saida_px <= entrada - cfg.stop_pontos
        ult = q.bid[j1 - 1]
    else:
        entrada = q.bid[i0]
        saida_px = q.ask[i0 + 1:j1]
        hit_alvo = saida_px <= entrada - cfg.alvo_pontos
        hit_stop = saida_px >= entrada + cfg.stop_pontos
        ult = q.ask[j1 - 1]
    ia = int(np.argmax(hit_alvo)) if hit_alvo.any() else None
    is_ = int(np.argmax(hit_stop)) if hit_stop.any() else None
    # empate no mesmo tick: o stop tem prioridade (conservador)
    if ia is not None and (is_ is None or ia < is_):
        classe, t_fim, pts = ALVO, int(q.t_ns[i0 + 1 + ia]), cfg.alvo_pontos
    elif is_ is not None:
        classe, t_fim, pts = STOP, int(q.t_ns[i0 + 1 + is_]), -(cfg.stop_pontos + custos.slippage_stop_pontos)
    else:
        classe, t_fim = TEMPO, t_end
        pts = float((ult - entrada) * lado)
    return Rotulo(lado, classe, t_decisao_ns, t_ent, t_fim, float(pts), pnl_brl(pts, cfg, custos))


def rotular(q: Cotacoes, instantes_ns: list[int], cfg: ConfigBarreiras, custos: Custos
            ) -> tuple[list[int], list[Rotulo], list[Rotulo]]:
    """Rotulos de compra e venda. Devolve (indices rotulados, rotulos de compra, rotulos de venda)."""
    idx: list[int] = []
    rc: list[Rotulo] = []
    rv: list[Rotulo] = []
    for k, t in enumerate(instantes_ns):
        c = rotular_um(q, t, +1, cfg, custos)
        v = rotular_um(q, t, -1, cfg, custos)
        if c is None or v is None:
            continue
        idx.append(k)
        rc.append(c)
        rv.append(v)
    return idx, rc, rv


def conhecidos_ate(rotulos: list[Rotulo], t_ns: int) -> list[Rotulo]:
    """Somente os rotulos ja resolvidos no instante t_ns."""
    return [r for r in rotulos if r.t_fim_ns <= t_ns]
