"""Vazamento de futuro: perturbacao futura, causalidade de normalizacoes/regimes/features, anonimizacao."""
from __future__ import annotations

import bisect
import datetime as dt

import numpy as np
import pytest

from robowin.backtest import (
    BracoSonda,
    ConfigBacktest,
    ControleAleatorio,
    FonteMemoria,
    MotorBacktest,
    dossie_sem_datas,
    perturbar_futuro,
    verificar_causal,
    verificar_perturbacao_futura,
)
from robowin.backtest.anonimizacao import verificar_anonimizacao_rapida
from robowin.backtest.replay import disponivel_tick
from robowin.backtest.vazamento import (
    features_causais,
    pipeline_incremental,
    zscore_expansivo,
    zscore_global,
)
from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.tipos import Acao, Intencao
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.ia.dossie import montar_dossie, verificar_anonimizacao

S = 1_000_000_000
DADOS = gerar(ConfigSintetico(seed=11, n_ticks=3000, livro_a_cada_ticks=10))
T_CORTE = dt_utc_ns(dt.datetime(2026, 3, 10, 13, 30))
CFG = ConfigBacktest(seed=3, intervalo_decisao_s=30.0)


def executar(ticks, livros, bracos=None):
    bracos = bracos() if bracos else [BracoSonda(p_operar=0.4), ControleAleatorio(4, p_entrada=0.1)]
    return MotorBacktest(FonteMemoria(ticks, livros, sintetico=True), bracos, CFG).executar()


# ============================================================================================== perturbacao
def test_perturbacao_futura_decisoes_identicas_ate_t():
    rel = verificar_perturbacao_futura(executar, DADOS.ticks, DADOS.livros, T_CORTE, semente=1)
    assert rel.ok, rel.texto()
    assert rel.n_comparados > 200 and rel.perturbacao_efetiva
    assert rel.n_ticks_perturbados > 100 and rel.n_livros_perturbados > 10


def test_perturbacao_so_toca_o_que_chega_depois_de_t():
    t_p, l_p = perturbar_futuro(DADOS.ticks, DADOS.livros, T_CORTE, semente=2)
    for a, b in zip(DADOS.ticks, t_p, strict=True):
        assert (a == b) == (disponivel_tick(a) <= T_CORTE) or a == b
        if disponivel_tick(a) <= T_CORTE:
            assert a == b
    assert all(a == b for a, b in zip(DADOS.livros, l_p, strict=True) if a.t_utc_ns <= T_CORTE)
    assert any(a != b for a, b in zip(DADOS.ticks, t_p, strict=True))


class BracoVazador:
    """Deliberadamente VAZA: olha o preco 10 minutos adiante na propria lista de ticks."""
    def __init__(self, ticks):
        self.id = "vazador"
        self._t = [t.t_utc_ns for t in ticks]
        self._p = [t.bid for t in ticks]
        self._n = 0

    def decidir(self, t_ns, dossie):
        i = bisect.bisect_right(self._t, t_ns)
        j = bisect.bisect_right(self._t, t_ns + 600 * S)
        self._n += 1
        if i == 0 or j >= len(self._t):
            return None
        acao = Acao.COMPRAR if self._p[j] > self._p[i - 1] else Acao.NAO_OPERAR
        return Intencao(id=f"v-{self._n}", braco=self.id, acao=acao, stop_pontos=100.0, alvo_pontos=100.0,
                        validade_ns=t_ns + 60 * S)


def test_perturbacao_detecta_braco_que_vaza_o_futuro():
    rel = verificar_perturbacao_futura(
        lambda t, lv: executar(t, lv, bracos=lambda: [BracoVazador(t)]), DADOS.ticks, DADOS.livros, T_CORTE, semente=1)
    assert not rel.ok and any("divergencia ate t" in d or "difere" in d for d in rel.divergencias)


def test_perturbacao_vazia_e_rejeitada():
    ultimo = max(disponivel_tick(t) for t in DADOS.ticks)
    rel = verificar_perturbacao_futura(executar, DADOS.ticks, DADOS.livros, ultimo + 1)
    assert not rel.ok and rel.n_ticks_perturbados == 0 and not rel.perturbacao_efetiva


def test_perturbacao_de_backfill_usa_disponibilidade_e_nao_t_evento():
    """Tick com t_evento antes de t mas recebido depois e FUTURO para o sistema: perturba-lo nao pode mudar o passado."""
    ticks = list(DADOS.ticks)
    k = next(i for i, t in enumerate(ticks) if t.t_utc_ns > T_CORTE - 600 * S)
    ticks[k] = ticks[k].model_copy(update={"t_recebido_ns": T_CORTE + 120 * S})  # chega 2 min apos t
    assert ticks[k].t_utc_ns <= T_CORTE < disponivel_tick(ticks[k])
    rel = verificar_perturbacao_futura(executar, ticks, DADOS.livros, T_CORTE, semente=5)
    assert rel.ok, rel.texto()
    t_p, _ = perturbar_futuro(ticks, DADOS.livros, T_CORTE, semente=5)
    assert t_p[k] != ticks[k]


# ============================================================================================== causalidade
def test_features_do_pipeline_sao_causais():
    instantes = list(range(T_CORTE - 3600 * S, T_CORTE + 1, 60 * S))
    assert features_causais(DADOS.ticks, DADOS.livros, instantes, T_CORTE, semente=3) == []


def test_pipeline_incremental_ignora_o_futuro():
    t_p, l_p = perturbar_futuro(DADOS.ticks, DADOS.livros, T_CORTE, semente=4)
    a = pipeline_incremental(DADOS.ticks, DADOS.livros, T_CORTE)
    b = pipeline_incremental(t_p, l_p, T_CORTE)
    assert a.keys() == b.keys() and all(a[k] == b[k] or (a[k] != a[k] and b[k] != b[k]) for k in a)


def _serie(n=400, seed=0):
    return list(np.cumsum(np.random.default_rng(seed).normal(0, 1, n)) + 100.0)


def regime_causal(x):
    """Regime = acima/abaixo da mediana EXPANSIVA do passado (causal)."""
    return [float(v > np.median(x[:i + 1])) for i, v in enumerate(x)]


def regime_nao_causal(x):
    """Regime = acima/abaixo da mediana da amostra TODA (vaza o futuro)."""
    m = float(np.median(x))
    return [float(v > m) for v in x]


def vol_norm_causal(x):
    a = np.asarray(x)
    return [float(np.std(a[max(0, i - 20):i + 1])) for i in range(len(a))]


@pytest.mark.parametrize("fn", [zscore_expansivo, regime_causal, vol_norm_causal])
def test_normalizacoes_e_regimes_causais_passam(fn):
    assert verificar_causal(fn, _serie(), pontos=range(30, 380, 25)) == []


@pytest.mark.parametrize("fn", [zscore_global, regime_nao_causal])
def test_normalizacoes_e_regimes_nao_causais_sao_detectados(fn):
    assert len(verificar_causal(fn, _serie(), pontos=range(30, 380, 25))) > 5


# ============================================================================================== anonimizacao
class Coletor:
    id = "coletor"

    def __init__(self):
        self.coletas = []

    def decidir(self, t_ns, dossie):
        self.coletas.append((t_ns, dossie))
        return None


def test_dossie_do_backtest_nao_contem_datas_nem_niveis_absolutos():
    c = Coletor()
    MotorBacktest(FonteMemoria(DADOS.ticks, DADOS.livros, sintetico=True), [c], CFG).executar()
    assert len(c.coletas) > 100
    niveis = sorted({t.bid for t in DADOS.ticks})
    for t, d in c.coletas[::10]:
        assert dossie_sem_datas(d, t, niveis) == [], (t, d)
    texto = repr(c.coletas[-1][1])
    assert "2026" not in texto and "SINT" not in texto and "WIN" not in texto


def test_detector_de_datas_pega_violacoes():
    t = dt_utc_ns(dt.datetime(2026, 3, 10, 10, 0))
    base = {"movimento": {"ret_60s_pts": 5.0}, "regime": "normal"}
    assert dossie_sem_datas(base, t) == []
    assert dossie_sem_datas({**base, "regime": "pregao de 2026-03-10"}, t)
    assert dossie_sem_datas({**base, "regime": "10/03/2026"}, t)
    assert dossie_sem_datas({**base, "meta": {"x": t}}, t)
    assert dossie_sem_datas({**base, "meta": {"x": t // 1_000_000}}, t)
    assert dossie_sem_datas({**base, "data": "x"}, t)           # chave proibida
    assert dossie_sem_datas({**base, "preco": 130000.0}, t)      # nivel absoluto


@pytest.mark.parametrize("seed", range(8))
def test_verificador_rapido_equivale_ao_original(seed):
    rng = np.random.default_rng(seed)
    precos = [float(130000 + 5 * k) for k in rng.integers(-50, 50, 200)]
    folhas = [float(rng.choice([rng.uniform(-300, 300), rng.choice(precos) + rng.choice([0, 0.3, 0.5, 0.7, 5])]))
              for _ in range(40)]
    d = {"a": {f"f{i}": v for i, v in enumerate(folhas)}, "regime": str(rng.choice(["normal", "em 2026", "WINJ26", "x"])),
         "lista": [1, 2.5, 130000.4, 5e18], "k": {"data": 1} if seed % 2 else {}}
    assert verificar_anonimizacao_rapida(d, precos) == verificar_anonimizacao(d, precos)


def test_dossie_real_passa_nos_dois_verificadores():
    ticks = [(i * S, 130000.0 + 5 * (i % 9)) for i in range(1, 1200)]
    d = montar_dossie(t0_ns=1199 * S, ticks=ticks, spread_pontos=5.0)
    assert verificar_anonimizacao_rapida(d, [p for _, p in ticks]) == verificar_anonimizacao(d, [p for _, p in ticks]) == []
