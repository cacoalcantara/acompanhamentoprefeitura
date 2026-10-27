"""Gerador MINIMO de dados SINTETICOS para demonstracao e testes (nao e o gerador do M2).

*** TUDO AQUI E SINTETICO. Nenhum resultado obtido com estes dados prova qualquer lucratividade. ***

Modelo: um fluxo latente persistente f (AR(1)) governa (i) a probabilidade de agressao compradora,
(ii) o desequilibrio do livro e (iii) -- somente se `forca_sinal > 0` -- uma pequena deriva do preco.
Com `forca_sinal = 0` as features sao informativas sobre f, mas f nao prediz o preco: nao ha edge.
"""
from __future__ import annotations

import math

import numpy as np

from robowin.core.tipos import LivroSnapshot, NivelBook, Tick
from robowin.features.pipeline import calcular_offline

from .microestrutura import Amostras, construir_amostras
from .rotulagem import ConfigBarreiras, Cotacoes, Custos

ROTULO = "SINTETICO"
INICIO_NS = 1_773_147_600 * 1_000_000_000  # um instante qualquer dentro de um pregao ficticio (10:00 em SP)
TICK = 5.0


def gerar(n_ticks: int = 100_000, seed: int = 1, forca_sinal: float = 0.05, ruido_pts: float = 1.5,
          phi: float = 0.995, preco0: float = 130_000.0, n_dias: int = 1
          ) -> tuple[list[Tick], list[LivroSnapshot]]:
    """`n_dias` sessoes ficticias de 10:00 em diante (o contexto intradiario fica estacionario entre dias)."""
    rng = np.random.default_rng(seed)
    por_dia = n_ticks // n_dias
    ticks: list[Tick] = []
    livros: list[LivroSnapshot] = []
    p = preco0
    for d in range(n_dias):
        p = _sessao(rng, d, por_dia, forca_sinal, ruido_pts, phi, p, ticks, livros)
    return ticks, livros


def _sessao(rng, dia: int, n: int, forca_sinal: float, ruido_pts: float, phi: float, p: float,
            ticks: list[Tick], livros: list[LivroSnapshot]) -> float:
    dts = np.maximum(rng.exponential(0.25, n), 0.001)
    ts = INICIO_NS + dia * 86_400 * 1_000_000_000 + np.cumsum((dts * 1e9).astype(np.int64))
    inov = rng.standard_normal(n)
    ruido = rng.standard_normal(n)
    u_neg = rng.random(n)
    u_lado = rng.random(n)
    vols = rng.choice([1.0, 2.0, 3.0, 5.0, 10.0], size=n)
    ruido_livro = rng.standard_normal((n // 4 + 10, 10))
    f = 0.0
    prox_livro = int(ts[0])
    k_livro = 0
    for i in range(n):
        f_ant = f
        f = phi * f + math.sqrt(1 - phi * phi) * inov[i]
        p += forca_sinal * f_ant + ruido_pts * ruido[i]  # f_ant: so o passado do fluxo move o preco
        bid = math.floor(p / TICK) * TICK
        ask = bid + TICK
        t = int(ts[i])
        if u_neg[i] < 0.6:
            compra = u_lado[i] < 1.0 / (1.0 + math.exp(-1.2 * f))
            ticks.append(Tick(t_servidor_ms=t // 1_000_000, t_utc_ns=t, t_recebido_ns=t + 1_000_000,
                              bid=bid, ask=ask, last=ask if compra else bid, volume=float(vols[i]),
                              flags=(32 if compra else 64) | 8))
        else:
            ticks.append(Tick(t_servidor_ms=t // 1_000_000, t_utc_ns=t, t_recebido_ns=t + 1_000_000,
                              bid=bid, ask=ask, last=0.0, volume=0.0, flags=6))
        if t >= prox_livro:
            prox_livro = t + 1_000_000_000
            r = ruido_livro[k_livro % len(ruido_livro)]
            k_livro += 1
            niveis = []
            for j in range(5):
                niveis.append(NivelBook(preco=bid - TICK * j, volume=float(max(1, round(
                    20 * math.exp(0.5 * f + 0.3 * r[j])))), lado="compra"))
                niveis.append(NivelBook(preco=ask + TICK * j, volume=float(max(1, round(
                    20 * math.exp(-0.5 * f + 0.3 * r[5 + j])))), lado="venda"))
            livros.append(LivroSnapshot(t_utc_ns=t, niveis=tuple(niveis)))
    return p


def montar_amostras(ticks: list[Tick], livros: list[LivroSnapshot], cfg: ConfigBarreiras | None = None,
                    custos: Custos | None = None, passo_s: int = 5, aquecimento_s: int = 600) -> Amostras:
    """Instantes de decisao a cada `passo_s`, apos o aquecimento de cada sessao e com horizonte completo."""
    cfg = cfg or ConfigBarreiras()
    custos = custos or Custos()
    ns = 1_000_000_000
    t = np.array([k.t_utc_ns for k in ticks], dtype=np.int64)
    quebras = np.flatnonzero(np.diff(t) > 3600 * ns) + 1  # nova sessao quando ha lacuna > 1h
    ini = np.concatenate([[0], quebras])
    fim_ = np.concatenate([quebras, [len(t)]]) - 1
    instantes: list[int] = []
    for i0, i1 in zip(ini, fim_, strict=True):
        instantes += list(range(int(t[i0]) + aquecimento_s * ns, int(t[i1]) - int(cfg.tempo_s * ns) - 10 * ns,
                                passo_s * ns))
    vetores = calcular_offline(ticks, livros, instantes)
    return construir_amostras(vetores, Cotacoes.de_ticks(ticks), cfg, custos)
