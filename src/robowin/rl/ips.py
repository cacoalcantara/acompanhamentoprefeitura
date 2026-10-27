"""Avaliacao off-policy por IPS (inverse propensity scoring) com propensoes REGISTRADAS.

V_hat(pi) = media_i( w_i * r_i ),  w_i = pi(a_i|x_i) / mu(a_i|x_i), com clipping w_i <= clip (introduz vies
para BAIXO em recompensas positivas, trocando vies por variancia; clip=None => nao viesado se mu>0 onde pi>0).
Para trajetorias, w_i e o produto das razoes por passo (`razao_trajetoria`). SNIPS (auto-normalizado) tambem
e devolvido, junto com o tamanho efetivo de amostra (ESS) como diagnostico de suporte.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ResultadoIPS:
    estimativa: float
    erro_padrao: float
    snips: float
    ess: float
    n: int
    frac_clipada: float
    peso_medio: float


def razao_trajetoria(p_log: Sequence[float], p_alvo: Sequence[float]) -> float:
    if len(p_log) != len(p_alvo):
        raise ValueError("tamanhos diferentes")
    if any(p <= 0 for p in p_log):
        raise ValueError("propensao registrada <= 0: sem suporte")
    return float(np.prod(np.asarray(p_alvo, dtype=float) / np.asarray(p_log, dtype=float)))


def estimar_ips(recompensas: Sequence[float], p_log: Sequence[float], p_alvo: Sequence[float],
                clip: float | None = None) -> ResultadoIPS:
    """Cada elemento = uma decisao (ou uma trajetoria, com p_* ja multiplicadas)."""
    r = np.asarray(recompensas, dtype=float)
    mu = np.asarray(p_log, dtype=float)
    pi = np.asarray(p_alvo, dtype=float)
    if not (len(r) == len(mu) == len(pi)) or len(r) == 0:
        raise ValueError("entradas vazias ou de tamanhos diferentes")
    if np.any(mu <= 0):
        raise ValueError("propensao registrada <= 0")
    w = pi / mu
    clipada = np.zeros(len(w), dtype=bool)
    if clip is not None:
        clipada = w > clip
        w = np.minimum(w, clip)
    x = w * r
    n = len(r)
    se = float(x.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan")
    soma_w = float(w.sum())
    snips = float((w * r).sum() / soma_w) if soma_w > 0 else float("nan")
    ess = float(soma_w**2 / (w**2).sum()) if (w**2).sum() > 0 else 0.0
    return ResultadoIPS(float(x.mean()), se, snips, ess, n, float(clipada.mean()), float(w.mean()))
