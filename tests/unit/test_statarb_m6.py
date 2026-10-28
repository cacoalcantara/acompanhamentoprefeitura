import numpy as np
import pytest

from robowin.cli import main
from robowin.core.protocolos import SinalStatArb
from robowin.core.tipos import Lado, Perna
from robowin.statarb.cointegracao import engle_granger, meia_vida_ou, residuo_cesta
from robowin.statarb.kalman import filtrar
from robowin.statarb.leadlag import hy_cov, lead_lag
from robowin.statarb.multiperna import abrir_sombra, pernas_de_hedge
from robowin.statarb.sinais import SinalStatArbPadrao
from robowin.statarb.sintetico import par_cointegrado, passeios_independentes, ticks_assincronos
from robowin.statarb.universo import Universo


class _Sim:
    def __init__(self, name):
        self.name = name


def test_universo_descarta_ausentes(caplog):
    u = Universo.criar(acoes=["PETR4", "XXXX3"]).validar([_Sim("IND"), _Sim("BOVA11"), _Sim("PETR4")])
    assert u.validos == ("IND", "BOVA11", "PETR4")
    assert set(u.descartados) == {"WDO", "IBOV", "XXXX3"}
    assert len(u.avisos) == 3 and "XXXX3" in u.avisos[-1]
    assert "IND" in u and "WDO" not in u
    assert Universo.criar().validar(None).validos == ()


def test_engle_granger_detecta_cointegrado():
    detec = [engle_granger(*par_cointegrado(1000, seed=s)).cointegrado for s in range(10)]
    assert sum(detec) >= 9
    r = engle_granger(*par_cointegrado(1000, seed=1))
    assert r.betas[0] == pytest.approx(1.5, abs=0.15)


def test_engle_granger_falso_positivo_controlado():
    fp = sum(engle_granger(*passeios_independentes(500, seed=s)).cointegrado for s in range(100))
    assert fp <= 12  # nominal 5%, folga estatistica


def test_meia_vida_ou_recuperada():
    phi = 0.95
    verdadeira = -np.log(2) / np.log(phi)
    y, x = par_cointegrado(5000, phi=phi, seed=3)
    hl = engle_granger(y, x).meia_vida
    assert hl == pytest.approx(verdadeira, rel=0.2)
    assert meia_vida_ou(np.cumsum(np.random.default_rng(0).normal(size=2000))) > 100


def test_residuo_de_cesta():
    r = np.random.default_rng(0)
    X = np.cumsum(r.normal(size=(1500, 2)), axis=0) + 50
    u = np.zeros(1500)
    for i in range(1, 1500):
        u[i] = 0.9 * u[i - 1] + r.normal()
    y = 3 + 2 * X[:, 0] - 1 * X[:, 1] + u
    c, b, res = residuo_cesta(y, X)
    assert b == pytest.approx([2, -1], abs=0.1)
    assert engle_granger(y, X).cointegrado


def test_kalman_converge_ao_beta():
    y, x = par_cointegrado(3000, beta=1.5, alpha=10, phi=0.9, seed=5)
    beta, alpha, _ = filtrar(y, x, delta=1e-6, var_obs=1.0)
    assert beta[-1] == pytest.approx(1.5, abs=0.05)
    assert np.mean(beta[-200:]) == pytest.approx(1.5, abs=0.05)


def test_kalman_segue_beta_variavel():
    r = np.random.default_rng(2)
    x = 100 + np.cumsum(r.normal(size=3000))
    b_real = np.where(np.arange(3000) < 1500, 1.0, 2.0)
    y = b_real * x + r.normal(0, 0.5, 3000)
    beta, _, _ = filtrar(y, x, delta=1e-4, var_obs=0.25)
    assert beta[-1] == pytest.approx(2.0, abs=0.15)


def test_hayashi_yoshida_recupera_lag():
    tx, x, ty, y = ticks_assincronos(3000.0, lag=3.0, seed=11)
    assert len(tx) > 2000 and not np.array_equal(tx[:50], ty[:50])  # realmente assincronos
    res = lead_lag(tx, x, ty, y, np.arange(-8.0, 8.01, 0.5))
    assert res.lag_otimo == pytest.approx(3.0, abs=1.0)
    assert res.correlacao > 0.5
    # sem defasagem verdadeira, lag ~ 0
    tx, x, ty, y = ticks_assincronos(3000.0, lag=0.0, seed=12)
    assert abs(lead_lag(tx, x, ty, y, np.arange(-8.0, 8.01, 0.5)).lag_otimo) <= 1.0


def test_hy_cov_caso_simples():
    # mesmos instantes: HY reduz a covariancia realizada
    t = np.arange(6.0)
    x = np.array([0, 1, 3, 2, 4, 5.0])
    y = np.array([0, 2, 2, 3, 7, 6.0])
    assert hy_cov(t, x, t, y) == pytest.approx(float(np.sum(np.diff(x) * np.diff(y))))


def _treinado(y, x, h):
    s = SinalStatArbPadrao(alvo="WIN", referencias=("REF",))
    assert s.status == "nao_treinado"
    s.treinar({"WIN": y[:h], "REF": x[:h]})
    assert s.status == "em_validacao"
    s.validar_oos({"WIN": y[h:2 * h], "REF": x[h:2 * h]}, origem_dados="historico_real", lucrativo=True)
    return s


def test_validado_oos_exige_origem_real_e_lucratividade():
    y, x = par_cointegrado(3000, seed=2)
    oos = {"WIN": y[1000:2000], "REF": x[1000:2000]}
    for kw in ({}, {"origem_dados": "sintetico", "lucrativo": True}, {"origem_dados": "historico_real"},
               {"origem_dados": "historico_real", "lucrativo": False}):
        s = SinalStatArbPadrao(alvo="WIN", referencias=("REF",))
        s.treinar({"WIN": y[:1000], "REF": x[:1000]})
        s.validar_oos(oos, **kw)
        assert s.status == "em_validacao"
        assert s.sinal(0, {"WIN": float(y[2000]) + 50, "REF": float(x[2000])})["direcional_alvo"] == 0.0


def test_sinal_nao_treinado_nao_e_direcional():
    y, x = par_cointegrado(600, seed=1)
    s = SinalStatArbPadrao(alvo="WIN", referencias=("REF",))
    p: SinalStatArb = s  # conformidade estrutural (checada por tipo)
    assert p.nome == "statarb_padrao"
    outs = [s.sinal(i, {"WIN": float(y[i]), "REF": float(x[i])}) for i in range(600)]
    assert s.status == "nao_treinado"
    assert all(o["direcional_alvo"] == 0.0 and o["validado"] == 0.0 for o in outs)


def test_sinal_validado_e_direcao():
    y, x = par_cointegrado(3000, seed=2)
    s = _treinado(y, x, 1000)
    assert s.status == "validado_oos"
    # WIN muito acima do justo -> venda (direcional negativo)
    s.sinal(0, {"WIN": float(y[2000]), "REF": float(x[2000])})
    alto = s.sinal(1, {"WIN": float(y[2000]) + 15.0, "REF": float(x[2000])})
    assert alto["z"] > 2 and alto["direcional_alvo"] < 0


def test_sinal_reprovado_em_passeios():
    a, b = passeios_independentes(3000, seed=4)
    s = SinalStatArbPadrao(alvo="WIN", referencias=("REF",))
    s.treinar({"WIN": a[:1000], "REF": b[:1000]})
    s.validar_oos({"WIN": a[1000:2000], "REF": b[1000:2000]})
    assert s.status in ("reprovado",)
    assert s.sinal(0, {"WIN": float(a[0]), "REF": float(b[0])})["direcional_alvo"] == 0.0


def test_causalidade_futuro_nao_muda_sinal():
    y, x = par_cointegrado(2400, seed=7)
    y2 = y.copy()
    y2[1700:] += np.random.default_rng(1).normal(0, 50, 700)  # futuro alterado
    x2 = x.copy()
    x2[1700:] *= 1.3
    s1, s2 = _treinado(y, x, 800), _treinado(y, x, 800)
    for i in range(1600, 1700):
        o1 = s1.sinal(i, {"WIN": float(y[i]), "REF": float(x[i])})
        o2 = s2.sinal(i, {"WIN": float(y2[i]), "REF": float(x2[i])})
        assert o1 == o2
    # e vale para o filtro puro
    b1, _, z1 = filtrar(y, x)
    b2, _, z2 = filtrar(y2, x2)
    assert np.array_equal(b1[:1700], b2[:1700]) and np.array_equal(z1[:1700], z2[:1700])


def test_multiperna_sombra():
    pernas = pernas_de_hedge("WINZ26", "BOVA11", 1.2, Lado.COMPRA)
    assert pernas[1].lado == Lado.VENDA and isinstance(pernas[0], Perna)
    c = abrir_sombra(pernas, {"WINZ26": 130000.0, "BOVA11": 130.0})
    pnl = c.pnl({"WINZ26": 130100.0, "BOVA11": 131.0}, {"WINZ26": 0.2, "BOVA11": 1.0})
    assert pnl == pytest.approx(100 * 0.2 - 1.0)
    with pytest.raises(KeyError):
        abrir_sombra(pernas, {"WINZ26": 1.0})


def test_cli_demo(capsys):
    assert main(["statarb", "demo", "--n", "1200"]) == 0
    out = capsys.readouterr().out
    assert "SINTETICO" in out and "Hayashi-Yoshida" in out
