"""Pontuacao de p_alvo (Brier, BSS, ECE, Murphy) e recalibracao walk-forward sem vazamento."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from robowin.avaliacao.calibracao_ia import (
    p_cal_walk_forward,
    p_teorico,
    pontuar,
    recalibrador_walk_forward,
    resolvidas,
)
from robowin.modelos.calibracao import MIN_CALIBRACAO, decomposicao_brier

NS = 1_000_000_000


def _t(dia: int, s: int = 0) -> int:
    # 13h UTC (10h em Sao Paulo) do dia `dia` de outubro de 2026
    return int(dt.datetime(2026, 10, dia, 13, tzinfo=dt.UTC).timestamp()) * NS + s * NS


def _prev(p, y, t0, *, stop=30.0, alvo=30.0, res=None, inc=0.5):
    return {"p_alvo": p, "y_alvo": y, "t0_ns": t0, "resolvido_ns": t0 + 60 * NS if res is None else res,
            "stop_pts": stop, "alvo_pts": alvo, "incerteza": inc}


def test_valores_conhecidos_brier_e_bss():
    # dia 1: p=0.8 y=1, p=0.2 y=0 ; dia 2: p=0.8 y=1, p=0.8 y=0
    pv = [_prev(0.8, 1, _t(5)), _prev(0.2, 0, _t(5, 1)), _prev(0.8, 1, _t(6)), _prev(0.8, 0, _t(6, 1))]
    r = pontuar(pv, n_boot=200, seed=1)
    assert r["n"] == 4 and r["n_pregoes"] == 2
    assert r["brier"] == pytest.approx((0.04 + 0.04 + 0.04 + 0.64) / 4)
    # teorico s/(s+a) = 0.5 => Brier de referencia 0.25 em toda linha
    assert r["brier_ref_teorico"] == pytest.approx(0.25)
    assert r["bss_teorico"] == pytest.approx(1 - 0.19 / 0.25)
    # taxa-base causal: dia 1 sem passado (fora); dia 2 usa a frequencia do dia 1 = 0.5
    assert r["n_ref_taxa_base"] == 2
    assert r["brier_ref_taxa_base"] == pytest.approx(0.25)
    assert r["bss_taxa_base"] == pytest.approx(1 - ((0.04 + 0.64) / 2) / 0.25)
    assert r["ic95_bss_teorico"] is not None and r["amostra_insuficiente"]


def test_previsoes_sem_desfecho_ou_p_sao_ignoradas():
    pv = [_prev(0.5, None, _t(5)), _prev(None, 1, _t(5)), _prev(1.5, 1, _t(5)), _prev(0.4, 0, _t(5))]
    assert len(resolvidas(pv)) == 1
    assert pontuar([])["n"] == 0


def test_p_teorico():
    assert p_teorico(30, 60) == pytest.approx(1 / 3)
    assert p_teorico(None, 60) is None and p_teorico(0, 10) is None


def test_decomposicao_murphy_exata_com_p_constante_por_faixa():
    rng = np.random.default_rng(3)
    p = rng.choice([0.15, 0.45, 0.75], 500)
    y = (rng.random(500) < p).astype(float)
    d = decomposicao_brier(p, y)
    assert d["brier"] == pytest.approx(d["confiabilidade"] - d["resolucao"] + d["incerteza"])
    assert abs(d["residuo"]) < 1e-12


def test_recalibrador_encolhe_antes_do_minimo():
    pv = [_prev(0.9, 1 if i % 4 == 0 else 0, _t(5, i)) for i in range(20)]
    t = _t(7)
    r = recalibrador_walk_forward(pv, t, n0=20)
    assert r.metodo == "encolhimento" and r.n == 20
    assert r.taxa_base == pytest.approx(0.25) and r.peso_bruto == pytest.approx(0.5)
    assert r(0.9) == pytest.approx(0.5 * 0.9 + 0.5 * 0.25)
    vazio = recalibrador_walk_forward(pv, _t(5) - 1)
    assert vazio.n == 0 and vazio(0.9) == pytest.approx(0.5)


def test_recalibrador_isotonico_depois_do_minimo():
    rng = np.random.default_rng(0)
    n = MIN_CALIBRACAO + 50
    p = rng.random(n)
    y = (rng.random(n) < 0.3).astype(int)  # p bruto sem informacao: calibrado deve ficar perto de 0.3
    pv = [_prev(float(p[i]), int(y[i]), _t(5, i)) for i in range(n)]
    r = recalibrador_walk_forward(pv, _t(8))
    assert r.metodo == "isotonica" and r.n == n
    assert abs(float(np.mean(r.transformar(p))) - y.mean()) < 0.05


def test_sem_vazamento_mudar_desfechos_futuros_nao_altera_p_cal():
    rng = np.random.default_rng(5)
    n = MIN_CALIBRACAO + 100
    base = [_prev(float(rng.random()), int(rng.random() < 0.4), _t(5, 10 * i)) for i in range(n)]
    t = base[MIN_CALIBRACAO + 20]["t0_ns"]
    futuro = [dict(r, y_alvo=1 - r["y_alvo"]) if r["resolvido_ns"] >= t else r for r in base]
    # previsao feita antes de t mas resolvida DEPOIS de t tambem e futuro (desfecho desconhecido em t)
    grade = np.linspace(0, 1, 21)
    a = recalibrador_walk_forward(base, t).transformar(grade)
    b = recalibrador_walk_forward(futuro, t).transformar(grade)
    assert np.array_equal(a, b)
    pa, pb = p_cal_walk_forward(base), p_cal_walk_forward(futuro)
    k = next(i for i, r in enumerate(base) if r["t0_ns"] >= t)
    assert pa[:k + 1] == pb[:k + 1]


def test_previsao_sem_resolucao_registrada_nao_entra():
    pv = [_prev(0.9, 1, _t(5, i), res=None) for i in range(5)]
    for r in pv:
        r["resolvido_ns"] = None
    assert recalibrador_walk_forward(pv, _t(9)).n == 0
