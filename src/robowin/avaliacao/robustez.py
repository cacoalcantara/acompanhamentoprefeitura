"""Robustez da comparacao entre bracos/configuracoes: PBO (CSCV) e comprimento minimo do historico (MinTRL).

Complementa o que ja existe em ``estatistica.py`` (Sharpe deflacionado, bootstrap estacionario, Holm em
``comparacao.py``) e em ``modelos/walkforward.py`` (purga e embargo). Faltavam duas perguntas:

* **PBO** (Probabilidade de Overfitting do Backtest, Bailey-Borwein-Lopez de Prado-Zhu, 2017) por CSCV: se eu
  escolhesse o MELHOR braco na metade dos dias, ele continuaria acima da mediana na outra metade? PBO alto => a
  escolha do "vencedor" e mais sorte de selecao que habilidade. Com poucos bracos/dias o resultado e ruidoso e a
  funcao devolve o aviso, sem inventar um numero.
* **MinTRL**: quantas observacoes sao necessarias para o Sharpe observado superar um Sharpe de referencia com a
  confianca pedida, dado o assimetria/curtose dos retornos.

Reimplementado de forma independente a partir das formulas dos artigos (o repositorio Aurum, MIT, serviu de
inspiracao conceitual). Nada aqui prova lucratividade futura.
"""
from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
from scipy import stats

from robowin.core.tipos import Trade

from .metricas import pnl_por_dia

MAX_COMBINACOES = 20_000


def _sharpe_colunas(m: np.ndarray) -> np.ndarray:
    sd = m.std(axis=0, ddof=1)
    mu = m.mean(axis=0)
    return np.where(sd > 0, mu / np.where(sd > 0, sd, 1.0), 0.0)


def pbo_cscv(retornos: np.ndarray | Sequence[Sequence[float]], n_blocos: int = 8) -> dict[str, Any]:
    """PBO por CSCV. ``retornos``: matriz (T periodos x N bracos). Devolve {'pbo', 'n_combinacoes', 'logits', ...}
    ou {'pbo': None, 'motivo': ...} quando a amostra nao permite estimar."""
    m = np.asarray(retornos, dtype=float)
    if m.ndim != 2:
        raise ValueError("retornos deve ser uma matriz T x N")
    t, n = m.shape
    if n_blocos < 2 or n_blocos % 2:
        raise ValueError("n_blocos deve ser par e >= 2")
    if n < 2:
        return {"pbo": None, "motivo": "PBO exige ao menos 2 bracos/configuracoes", "n_bracos": n, "n_periodos": t}
    if t < n_blocos * 2:
        return {"pbo": None, "motivo": f"poucos periodos ({t}) para {n_blocos} blocos", "n_bracos": n, "n_periodos": t}
    if not np.all(np.isfinite(m)):
        raise ValueError("retornos com valores nao finitos")
    comb_total = math.comb(n_blocos, n_blocos // 2)
    if comb_total > MAX_COMBINACOES:
        raise ValueError(f"combinacoes demais ({comb_total}); reduza n_blocos")
    blocos = np.array_split(np.arange(t), n_blocos)
    logits: list[float] = []
    for ins in itertools.combinations(range(n_blocos), n_blocos // 2):
        fora = [b for b in range(n_blocos) if b not in ins]
        i_is = np.concatenate([blocos[b] for b in ins])
        i_os = np.concatenate([blocos[b] for b in fora])
        sr_is, sr_os = _sharpe_colunas(m[i_is]), _sharpe_colunas(m[i_os])
        melhor = int(np.argmax(sr_is))
        # rank relativo do melhor-no-treino entre os N no teste (1 = pior, N = melhor); empates pela media
        rank = stats.rankdata(sr_os)[melhor]
        omega = rank / (n + 1.0)
        logits.append(math.log(omega / (1.0 - omega)))
    lg = np.asarray(logits)
    return {"pbo": float((lg <= 0).mean()), "n_combinacoes": int(lg.size), "n_bracos": n, "n_periodos": t,
            "logit_mediano": float(np.median(lg)), "aviso": (
                "PBO e uma estimativa ruidosa com poucos bracos/dias; valores altos indicam que o melhor braco "
                "dentro da amostra nao se sustenta fora dela, nao que exista ou deixe de existir vantagem.")}


def pbo_entre_bracos(trades_por_braco: Mapping[str, Sequence[Trade]], n_blocos: int = 8) -> dict[str, Any]:
    """PBO tratando cada braco como uma 'configuracao' e o P&L liquido por pregao como retorno."""
    series = {b: pnl_por_dia(ts) for b, ts in trades_por_braco.items() if ts}
    if len(series) < 2:
        return {"pbo": None, "motivo": "menos de 2 bracos com operacoes"}
    dias = sorted({d for s in series.values() for d in s})
    mat = np.array([[series[b].get(d, 0.0) for b in sorted(series)] for d in dias], dtype=float)
    out = pbo_cscv(mat, n_blocos)
    out["bracos"] = sorted(series)
    return out


def min_track_record_length(sr: float, sr_ref: float = 0.0, assimetria: float = 0.0, curtose: float = 3.0,
                            confianca: float = 0.95) -> float:
    """MinTRL (Bailey & Lopez de Prado): observacoes necessarias para que o SR observado (por periodo) supere
    ``sr_ref`` com ``confianca``. ``curtose`` bruta (normal = 3). Devolve ``inf`` se sr <= sr_ref."""
    if not 0.5 < confianca < 1.0:
        raise ValueError("confianca deve estar em (0,5; 1)")
    if sr <= sr_ref:
        return math.inf
    z = float(stats.norm.ppf(confianca))
    var = 1.0 - assimetria * sr + (curtose - 1.0) / 4.0 * sr * sr
    if var <= 0:
        return math.inf
    return 1.0 + var * (z / (sr - sr_ref)) ** 2
