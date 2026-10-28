"""Rotulagem por tripla barreira, walk-forward com purga/embargo, calibracao e HAR-RV. Dados SINTETICOS."""
import numpy as np
import pytest
from perfis_teste import WIN

from robowin.modelos import rotulagem as rot
from robowin.modelos.calibracao import PISO_PROB, CalibradorIsotonico, ece
from robowin.modelos.har import HARRV
from robowin.modelos.walkforward import (
    Dobra,
    VazamentoDetectado,
    dividir_walkforward,
    mascara_treino,
    verificar_sem_vazamento,
)

NS = 1_000_000_000
CFG = rot.ConfigBarreiras(alvo_pontos=30, stop_pontos=30, tempo_s=60, atraso_ns=0, ponto_valor_brl=WIN.ponto_valor_brl)
SEM_CUSTO = rot.Custos(tarifas_corretagem_brl=0.0, slippage_stop_pontos=0.0)
CUSTO = rot.Custos(tarifas_corretagem_brl=0.5, slippage_stop_pontos=5.0)


def cot(pontos):
    """pontos: lista (t_s, bid, ask)."""
    a = np.array(pontos, dtype=float)
    return rot.Cotacoes((a[:, 0] * NS).astype(np.int64), a[:, 1], a[:, 2])


def serie(bids, passo=10, spread=5.0):
    return cot([(i * passo, b, b + spread) for i, b in enumerate(bids)])


def test_compra_alvo_stop_tempo():
    # entrada: ask = 105 em t=0
    alvo = serie([100, 110, 120, 140, 140, 140, 140])  # bid 140 >= 105+30
    r = rot.rotular_um(alvo, 0, +1, CFG, CUSTO)
    assert r and r.classe == rot.ALVO and r.t_fim_ns == 30 * NS and r.pnl_pontos == 30
    assert r.pnl_liquido_brl == pytest.approx(30 * 0.2 - 0.5)
    stop = serie([100, 90, 80, 70, 70, 70, 70])  # bid 70 <= 105-30
    r = rot.rotular_um(stop, 0, +1, CFG, CUSTO)
    assert r.classe == rot.STOP and r.pnl_pontos == -35  # stop 30 + slippage 5
    assert r.pnl_liquido_brl == pytest.approx(-35 * 0.2 - 0.5)
    tempo = serie([100, 102, 99, 103, 101, 100, 104, 100])  # t=60 -> bid 104 (indice 6)
    r = rot.rotular_um(tempo, 0, +1, CFG, CUSTO)
    assert r.classe == rot.TEMPO and r.t_fim_ns == 60 * NS
    assert r.pnl_pontos == pytest.approx(104 - 105)  # paga o spread na saida


def test_venda_simetrica():
    q = serie([100, 90, 80, 60, 60, 60, 60])  # ask = bid+5; entrada no bid=100; alvo: ask <= 70
    r = rot.rotular_um(q, 0, -1, CFG, CUSTO)
    assert r.classe == rot.ALVO and r.pnl_pontos == 30
    q = serie([100, 110, 120, 140, 140, 140, 140])  # stop: ask >= 130
    r = rot.rotular_um(q, 0, -1, CFG, CUSTO)
    assert r.classe == rot.STOP


def test_custos_reduzem_resultado_e_coerencia():
    q = serie([100, 110, 120, 140, 140, 140, 140])
    a = rot.rotular_um(q, 0, +1, CFG, SEM_CUSTO)
    b = rot.rotular_um(q, 0, +1, CFG, CUSTO)
    assert a.pnl_liquido_brl - b.pnl_liquido_brl == pytest.approx(0.5)
    # geometria do plano (stop 300, alvo 100): acerto de equilibrio na faixa de 76-78%
    cfg = rot.ConfigBarreiras(alvo_pontos=100, stop_pontos=300, ponto_valor_brl=WIN.ponto_valor_brl)
    assert 0.75 < rot.acerto_equilibrio(cfg, CUSTO) < 0.78
    # com esse acerto a expectativa e ~0: p*alvo - (1-p)*(stop+slip) - custo = 0
    p = rot.acerto_equilibrio(cfg, CUSTO)
    e = p * rot.pnl_brl(100, cfg, CUSTO) + (1 - p) * rot.pnl_brl(-305, cfg, CUSTO)
    assert e == pytest.approx(0.0, abs=1e-9)


def test_stop_tem_prioridade_no_mesmo_tick():
    q = serie([100, 100, 200, 100, 100, 100, 100])
    q = rot.Cotacoes(q.t_ns, q.bid, q.ask)
    # um unico tick onde bid >= alvo e ... nao pode tocar os dois no mesmo lado; checa ordem temporal
    q2 = serie([100, 60, 140, 100, 100, 100, 100])
    assert rot.rotular_um(q2, 0, +1, CFG, CUSTO).classe == rot.STOP  # stop (60) vem antes do alvo (140)


def test_rotulo_so_conhecido_apos_o_horizonte_e_sem_dados_futuros():
    q = serie([100, 102, 99, 103, 101, 100, 104, 100])
    r = rot.rotular_um(q, 0, +1, CFG, CUSTO)
    assert rot.conhecidos_ate([r], 59 * NS) == [] and rot.conhecidos_ate([r], 60 * NS) == [r]
    # horizonte alem dos dados -> sem rotulo
    assert rot.rotular_um(serie([100, 101, 102]), 0, +1, CFG, CUSTO) is None
    # alterar cotacoes DEPOIS de t_fim nao muda o rotulo
    alvo = serie([100, 110, 120, 140, 140, 140, 140, 140])
    r1 = rot.rotular_um(alvo, 0, +1, CFG, CUSTO)
    b2 = alvo.bid.copy()
    b2[5:] = 0.0
    r2 = rot.rotular_um(rot.Cotacoes(alvo.t_ns, b2, b2 + 5), 0, +1, CFG, CUSTO)
    assert (r1.classe, r1.t_fim_ns, r1.pnl_pontos) == (r2.classe, r2.t_fim_ns, r2.pnl_pontos)


def test_lacuna_de_cotacoes_nao_rotula():
    q = cot([(0, 100, 105), (10, 100, 105), (20, 100, 105), (500, 100, 105)])  # buraco ate t=500
    assert rot.rotular_um(q, 0, +1, CFG, CUSTO) is None


# ---- walk-forward --------------------------------------------------------------------------------
def _amostras(n=1000, seed=0, horizonte_max=120):
    rng = np.random.default_rng(seed)
    t0 = np.sort(rng.integers(0, 10_000, n)) * NS
    t1 = t0 + rng.integers(10, horizonte_max, n) * NS
    return t0.astype(np.int64), t1.astype(np.int64)


def test_purga_nunca_treina_com_rotulo_que_termina_depois_do_teste():
    t0, t1 = _amostras()
    for emb in (0, 30 * NS, 200 * NS):
        dobras = dividir_walkforward(t0, t1, n_dobras=5, embargo_ns=emb)
        assert len(dobras) == 5
        for d in dobras:
            assert t1[d.idx_treino].max() <= d.teste_ini_ns - emb
            assert np.intersect1d(d.idx_treino, d.idx_teste).size == 0
            assert t0[d.idx_teste].min() == d.teste_ini_ns


def test_purga_remove_exatamente_as_amostras_sobrepostas():
    t0, t1 = _amostras()
    d = dividir_walkforward(t0, t1, n_dobras=4, embargo_ns=0)[1]
    passado = np.flatnonzero(t0 < d.teste_ini_ns)
    purgados = np.setdiff1d(passado, d.idx_treino)
    assert len(purgados) > 0  # ha sobreposicao real a remover
    assert np.all(t1[purgados] > d.teste_ini_ns)
    # sem purga haveria vazamento (o teste detecta)
    ingenuo = Dobra(passado, d.idx_teste, d.teste_ini_ns, d.teste_fim_ns)
    with pytest.raises(VazamentoDetectado):
        verificar_sem_vazamento(ingenuo, t0, t1)


def test_embargo_remove_mais_amostras_e_dobras_nao_se_sobrepoem():
    t0, t1 = _amostras()
    a = dividir_walkforward(t0, t1, 5, embargo_ns=0)
    b = dividir_walkforward(t0, t1, 5, embargo_ns=300 * NS)
    assert all(len(y.idx_treino) < len(x.idx_treino) for x, y in zip(a, b, strict=True))
    for x, y in zip(a, a[1:], strict=False):
        assert x.teste_fim_ns < y.teste_ini_ns and np.intersect1d(x.idx_teste, y.idx_teste).size == 0


def test_mascara_treino_tambem_purga_treino_posterior_ao_teste():
    t0 = np.array([0, 10, 50, 90, 100, 150], dtype=np.int64) * NS
    t1 = t0 + 15 * NS
    m = mascara_treino(t0, t1, 40 * NS, 95 * NS, embargo_ns=10 * NS)
    # antes: t1<=30 -> idx 0,1 (t1=15,25). depois: t0>=105 -> idx 5. idx 2,3,4 purgados.
    assert m.tolist() == [True, True, False, False, False, True]


def test_janela_deslizante_de_treino():
    t0, t1 = _amostras()
    d = dividir_walkforward(t0, t1, 3, janela_treino_ns=2000 * NS)[-1]
    assert t0[d.idx_treino].min() >= d.teste_ini_ns - 2000 * NS


# ---- calibracao ----------------------------------------------------------------------------------
def test_calibracao_isotonica_corrige_excesso_de_confianca_e_soma_um():
    rng = np.random.default_rng(1)
    n = 6000
    v = rng.dirichlet([2, 2, 2], n)  # probabilidades verdadeiras
    y = np.array([rng.choice(3, p=p) for p in v])
    sup = v ** 3 / (v ** 3).sum(axis=1, keepdims=True)  # modelo superconfiante
    cal = CalibradorIsotonico(3).ajustar(sup[: n // 2], y[: n // 2])
    q = cal.transformar(sup[n // 2:])
    assert q.sum(axis=1) == pytest.approx(np.ones(len(q)))
    assert q.min() >= PISO_PROB / 3
    yy = (y[n // 2:] == 0).astype(float)
    assert ece(q[:, 0], yy) < ece(sup[n // 2:, 0], yy)
    assert ece(q[:, 0], yy) < 0.05


def test_calibracao_com_poucos_dados_e_identidade():
    p = np.full((50, 3), 1 / 3)
    cal = CalibradorIsotonico(3).ajustar(p, np.zeros(50, int))
    assert not cal.ajustado
    assert cal.transformar(p) == pytest.approx(p)


# ---- HAR-RV --------------------------------------------------------------------------------------
def test_har_rv_recupera_dinamica_e_usa_so_o_passado():
    rng = np.random.default_rng(0)
    n = 400
    rv = np.empty(n)
    rv[:22] = 1.0
    for t in range(21, n - 1):
        d, w, m = rv[t], rv[t - 4:t + 1].mean(), rv[t - 21:t + 1].mean()
        rv[t + 1] = max(0.05 + 0.35 * d + 0.25 * w + 0.20 * m + 0.05 * rng.standard_normal(), 0.01)
    har = HARRV().ajustar(rv[:300])
    assert har.coef[1] + har.coef[2] + har.coef[3] == pytest.approx(0.8, abs=0.15)
    prev = np.array([har.prever(rv[:t]) for t in range(300, n)])
    real = rv[300:n]
    assert np.mean(np.abs(prev - real)) < 0.06  # ruido do processo tem desvio 0.05
    # a previsao em t nao depende de valores posteriores
    a = har.prever(rv[:350])
    rv2 = rv.copy()
    rv2[350:] = 999.0
    assert har.prever(rv2[:350]) == a
    with pytest.raises(ValueError):
        HARRV().ajustar(rv[:20])
