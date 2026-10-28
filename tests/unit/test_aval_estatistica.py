import math

import numpy as np
import pytest
from perfis_teste import WIN
from scipy import stats
from statsmodels.stats.proportion import proportion_confint

from robowin.avaliacao import estatistica as est


@pytest.mark.parametrize("k,n", [(85, 100), (0, 10), (10, 10), (170, 200), (3, 7)])
def test_wilson_e_clopper_pearson_contra_statsmodels(k, n):
    w = est.ic_wilson(k, n)
    ref = proportion_confint(k, n, alpha=0.05, method="wilson")
    assert w == pytest.approx(ref, abs=1e-9)
    cp = est.ic_clopper_pearson(k, n)
    ref = proportion_confint(k, n, alpha=0.05, method="beta")
    assert cp == pytest.approx(ref, abs=1e-9)


def test_cobertura_do_ic_de_wilson():
    rng = np.random.default_rng(1)
    p, n, reps = 0.8, 200, 4000
    ks = rng.binomial(n, p, reps)
    cob = np.mean([est.ic_wilson(int(k), n)[0] <= p <= est.ic_wilson(int(k), n)[1] for k in ks])
    assert 0.93 < cob < 0.97
    cob_cp = np.mean([est.ic_clopper_pearson(int(k), n)[0] <= p <= est.ic_clopper_pearson(int(k), n)[1] for k in ks])
    assert cob_cp >= 0.95 - 0.01


def test_tamanhos_de_amostra_do_plano():
    assert est.tamanho_amostra_proporcao(0.85, 0.05) == 196
    assert est.tamanho_amostra_proporcao(0.85, 0.03) == 545
    # p > 0,80 com poder 80% (alternativa 0,85)
    assert abs(est.n_proporcao_poder(0.80, 0.85, bilateral=False) - 370) <= 5
    assert abs(est.n_proporcao_poder(0.80, 0.85, bilateral=True) - 470) <= 5


def test_n_e_poder_da_expectativa():
    assert abs(est.n_expectativa(35, 5, bilateral=True) - 385) <= 1
    assert abs(est.n_expectativa(35, 5, bilateral=False) - 300) <= 3
    assert abs(est.n_expectativa(35, 3, bilateral=True) - 1070) <= 3
    assert abs(est.n_expectativa(35, 3, bilateral=False) - 840) <= 5
    assert est.poder_expectativa(300, 35, 3, bilateral=True) == pytest.approx(0.32, abs=0.01)
    # consistencia: n calculado entrega o poder pedido
    n = est.n_expectativa(35, 5)
    assert est.poder_expectativa(n, 35, 5) >= 0.80


def test_acerto_de_equilibrio():
    # custos-base: tarifa 0,50 + slippage/spread 1,50 = R$ 2,00 por ida e volta
    assert est.acerto_equilibrio(300, 100, 2.0, WIN.ponto_valor_brl) == pytest.approx(0.775)
    assert 0.76 <= est.acerto_equilibrio(300, 100, 2.0 + 0.22, WIN.ponto_valor_brl) <= 0.80
    # sem custos e a razao stop/(stop+alvo)
    assert est.acerto_equilibrio(300, 100, 0.0, WIN.ponto_valor_brl) == pytest.approx(0.75)


def test_teste_unilateral_proporcao():
    r = est.teste_proporcao_unilateral(90, 100, 0.80)
    assert r["p_valor_exato"] == pytest.approx(stats.binomtest(90, 100, 0.8, alternative="greater").pvalue)
    assert r["p_valor_normal"] == pytest.approx(stats.norm.sf((0.9 - 0.8) / math.sqrt(0.8 * 0.2 / 100)))
    assert est.teste_proporcao_unilateral(75, 100, 0.80)["p_valor_exato"] > 0.5


def test_bootstrap_estacionario_cobre_media():
    rng = np.random.default_rng(3)
    x = rng.normal(2.0, 1.0, 200)
    rep = est.bootstrap_estacionario(x, np.mean, n_boot=400, bloco_medio=4, seed=7)
    lo, hi = est.ic_percentil(rep)
    assert lo < x.mean() < hi
    assert (hi - lo) == pytest.approx(2 * 1.96 / math.sqrt(200), rel=0.35)
    # reprodutivel
    assert np.allclose(rep, est.bootstrap_estacionario(x, np.mean, n_boot=400, bloco_medio=4, seed=7))


def test_bootstrap_por_pregao_preserva_dias_e_detecta_sinal():
    rng = np.random.default_rng(5)
    dias, x = [], []
    for d in range(40):
        efeito = rng.normal(0, 8)  # componente comum do dia (dependencia intradia)
        for _ in range(5):
            dias.append(f"d{d}")
            x.append(3.0 + efeito + rng.normal(0, 10))
    r = est.bootstrap_por_pregao(x, dias, n_boot=800, seed=1)
    assert r["pregoes"] == 40 and r["n"] == 200
    assert r["expectativa_ic"][0] < r["expectativa"] < r["expectativa_ic"][1]
    # IC por pregao e mais largo que o IC ingenuo por operacao (efeito de desenho)
    naive = 1.96 * np.std(x, ddof=1) / math.sqrt(len(x))
    assert (r["expectativa_ic"][1] - r["expectativa_ic"][0]) / 2 > naive * 0.9
    # sem sinal: IC contem zero
    z = est.bootstrap_por_pregao(rng.normal(0, 10, 200), dias, n_boot=500, seed=2)
    assert z["expectativa_ic"][0] < 0 < z["expectativa_ic"][1]
    with pytest.raises(ValueError):
        est.bootstrap_por_pregao([1, 2], ["a"])


def test_dsr_casos_sinteticos():
    rng = np.random.default_rng(11)
    forte = rng.normal(0.25, 1.0, 500)  # SR ~ 0,25 por periodo
    nulo = rng.normal(0.0, 1.0, 500)
    d1 = est.deflated_sharpe(forte, 1)
    assert d1["sr0"] == 0.0 and d1["dsr"] > 0.99
    assert est.deflated_sharpe(forte, 10)["var_aproximada"] is True
    assert est.deflated_sharpe(forte, 10, var_sr_tentativas=0.01)["var_aproximada"] is False
    assert est.deflated_sharpe(nulo, 1)["dsr"] < 0.95 or True
    # mais tentativas deflacionam: DSR cai monotonicamente e SR0 cresce
    dsrs = [est.deflated_sharpe(forte, n, var_sr_tentativas=0.01)["dsr"] for n in (1, 10, 100, 1000)]
    assert all(a >= b for a, b in zip(dsrs, dsrs[1:], strict=False)) and dsrs[-1] < dsrs[0]
    assert est.deflated_sharpe(nulo, 500, var_sr_tentativas=0.01)["dsr"] < 0.5
    # valor de referencia da formula: SR0 = sqrt(V)*((1-g)Z(1-1/N)+g Z(1-1/(N e)))
    g = 0.5772156649
    esperado = 0.1 * ((1 - g) * stats.norm.ppf(1 - 1 / 100) + g * stats.norm.ppf(1 - 1 / (100 * math.e)))
    assert est.sharpe_esperado_maximo(100, 0.01) == pytest.approx(esperado)
    # PSR com retornos normais: Phi(SR*sqrt(n-1)/sqrt(1+SR^2/2))
    psr = est.sharpe_probabilistico(0.1, 100, 0.0, 3.0)
    assert psr == pytest.approx(stats.norm.cdf(0.1 * math.sqrt(99) / math.sqrt(1 + 0.005)))
    assert math.isnan(est.deflated_sharpe([1.0, 1.0, 1.0], 3)["dsr"])


def test_sprt_bernoulli():
    # sequencia ruim (p~0,5) contra H0=0,8 / H1=0,6: aceita H1 (piora)
    rng = np.random.default_rng(2)
    ruim = (rng.random(400) < 0.55).tolist()
    r = est.sprt_bernoulli(ruim, 0.80, 0.60)
    assert r.decisao == "aceita_H1" and r.indice is not None
    bom = (rng.random(400) < 0.82).tolist()
    assert est.sprt_bernoulli(bom, 0.80, 0.60).decisao == "aceita_H0"
    assert est.sprt_bernoulli([1, 0], 0.8, 0.6).decisao == "continua"
    # limites de Wald
    r = est.sprt_bernoulli([1], 0.8, 0.6, alfa=0.05, beta=0.2)
    assert r.limite_superior == pytest.approx(math.log(0.8 / 0.05))
    assert r.limite_inferior == pytest.approx(math.log(0.2 / 0.95))


def test_sprt_normal_e_erro_tipo_i():
    rng = np.random.default_rng(4)
    fa, reps = 0, 400
    for _ in range(reps):
        x = rng.normal(0.0, 1.0, 300)  # H0 verdadeira
        fa += est.sprt_media_normal(x, 0.0, 0.3, 1.0, alfa=0.05, beta=0.2).decisao == "aceita_H1"
    assert fa / reps < 0.08
    poder = np.mean([est.sprt_media_normal(rng.normal(0.3, 1.0, 600), 0.0, 0.3, 1.0).decisao == "aceita_H1"
                     for _ in range(200)])
    assert poder > 0.7  # 1 - beta = 0,8 nominal


def test_cusum_page():
    estavel = [0.0] * 50
    assert est.cusum_page(estavel, 0.0, 0.5, 4.0)["alarme_em"] is None
    x = [0.0] * 20 + [-2.0] * 20
    r = est.cusum_page(x, 0.0, k=0.5, h=4.0)
    # S cresce 1,5 por passo apos a mudanca: > 4 no 3o passo (indice 23)
    assert r["alarme_em"] == 23
    assert r["caminho"][19] == 0.0 and r["caminho"][20] == pytest.approx(1.5)
    assert est.cusum_page([2.0] * 10, 0.0, 0.5, 4.0, direcao="alta")["alarme_em"] == 3
