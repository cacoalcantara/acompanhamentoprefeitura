import numpy as np
import pytest

from robowin.execucao.politicas import AGREDIR, PASSIVA, PoliticaExecucao, PoliticaPadrao
from robowin.rl.bandit import BanditThompsonLinear
from robowin.rl.ips import estimar_ips, razao_trajetoria
from robowin.rl.politica import PoliticaAtribuicaoAleatoria, PoliticaBandit, contexto
from robowin.rl.q_tabular import Discretizador, QTabularFinito, induzir_para_tras

# ---------------------------------------------------------------- bandit ---------------------------------


def test_bandit_converge_no_braco_melhor():
    rng = np.random.default_rng(0)
    b = BanditThompsonLinear(3, 2, seed=1, sigma=0.5, piso=0.02)
    theta = [np.array([0.0, 0.0]), np.array([1.0, 0.0]), np.array([0.2, 0.0])]  # braco 1 e melhor sempre
    escolhas = []
    for _ in range(1500):
        x = np.array([1.0, rng.normal()])
        a, p = b.escolher(x)
        assert 0 < p <= 1
        b.atualizar(x, a, float(theta[a] @ x + rng.normal(0, 0.5)))
        escolhas.append(a)
    assert np.mean(np.array(escolhas[-300:]) == 1) > 0.9
    assert b.media_prevista(np.array([1.0, 0.0]), 1) == pytest.approx(1.0, abs=0.15)


def test_bandit_contextual_aprende_braco_dependente_do_contexto():
    rng = np.random.default_rng(2)
    b = BanditThompsonLinear(2, 2, seed=3, sigma=0.3, piso=0.05)
    for _ in range(2500):
        c = rng.uniform(-1, 1)
        x = np.array([1.0, c])
        a, _ = b.escolher(x)
        r = (c if a == 1 else -c) + rng.normal(0, 0.3)  # braco 1 melhor se c > 0
        b.atualizar(x, a, r)
    assert b.probabilidades(np.array([1.0, 0.8])).argmax() == 1
    assert b.probabilidades(np.array([1.0, -0.8])).argmax() == 0


def test_bandit_deterministico():
    def run():
        b = BanditThompsonLinear(3, 2, seed=4)
        out = []
        for i in range(50):
            x = np.array([1.0, i % 3])
            a, p = b.escolher(x)
            b.atualizar(x, a, float(a == 2))
            out.append((a, p))
        return out
    assert run() == run()


# ---------------------------------------------------------------- IPS ------------------------------------


def test_ips_nao_viesado_em_caso_sintetico_conhecido():
    mu = np.array([1.0, 2.0, 5.0])         # recompensa media por acao (sem contexto)
    p_log = np.array([0.5, 0.3, 0.2])
    p_alvo = np.array([0.1, 0.1, 0.8])
    verdade = float(p_alvo @ mu)
    estimativas = []
    for s in range(200):
        rng = np.random.default_rng(100 + s)
        a = rng.choice(3, size=400, p=p_log)
        r = mu[a] + rng.normal(0, 1.0, size=400)
        estimativas.append(estimar_ips(r, p_log[a], p_alvo[a]).estimativa)
    media = float(np.mean(estimativas))
    erro = float(np.std(estimativas) / np.sqrt(len(estimativas)))
    assert abs(media - verdade) < 4 * erro


def test_ips_clipping_reduz_pesos_e_enviesa_para_baixo():
    rng = np.random.default_rng(0)
    a = rng.choice(2, size=5000, p=[0.95, 0.05])
    r = np.where(a == 1, 3.0, 1.0)
    p_log = np.where(a == 1, 0.05, 0.95)
    p_alvo = np.where(a == 1, 0.9, 0.1)  # peso 18 nos raros
    livre = estimar_ips(r, p_log, p_alvo)
    clip = estimar_ips(r, p_log, p_alvo, clip=5.0)
    assert clip.frac_clipada > 0 and clip.estimativa < livre.estimativa
    assert livre.estimativa == pytest.approx(0.9 * 3 + 0.1 * 1, rel=0.1)
    assert clip.ess > 0


def test_razao_trajetoria_e_validacoes():
    assert razao_trajetoria([0.5, 0.5], [1.0, 0.25]) == pytest.approx(1.0)
    assert razao_trajetoria([0.5, 1.0], [0.25, 1.0]) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        razao_trajetoria([0.0], [1.0])
    with pytest.raises(ValueError):
        estimar_ips([1.0], [0.0], [1.0])


# ---------------------------------------------------------------- Q tabular --------------------------------


def test_q_tabular_converge_para_solucao_exata_de_mdp_conhecido():
    S, A, H = 3, 2, 3
    P = np.zeros((S, A, S))
    R = np.zeros((S, A))
    P[0, 0, 1] = P[0, 1, 2] = 1.0
    P[1, 0, 0] = P[1, 1, 2] = 1.0
    P[2, 0, 2] = P[2, 1, 0] = 1.0
    R[0] = [1.0, 0.0]
    R[1] = [0.0, 2.0]
    R[2] = [0.5, 3.0]
    exato = induzir_para_tras(P, R, H)
    q = QTabularFinito(S, A, H, alfa=0.3, seed=0)
    rng = np.random.default_rng(1)
    for _ in range(4000):
        s = int(rng.integers(S))
        for h in range(H):
            a = int(rng.integers(A))
            s2 = int(P[s, a].argmax())
            q.atualizar(h, s, a, float(R[s, a]), s2, terminal=h == H - 1)
            s = s2
    assert np.allclose(q.Q[:H], exato[:H], atol=1e-6)
    assert q.gulosa(0, 0) == int(exato[0, 0].argmax())


def test_discretizador_e_propensao_epsilon_gulosa():
    d = Discretizador([[1.5], [-1.0, 1.0]])
    assert d.n_estados == 6 and d([1.0, 0.0]) != d([2.0, 0.0]) and d([2.0, 5.0]) == 5
    q = QTabularFinito(6, 3, 2, seed=0)
    q.Q[0, 0, 2] = 1.0
    p = q.probabilidades(0, 0, 0.3)
    assert p.sum() == pytest.approx(1.0) and p[2] == pytest.approx(0.7 + 0.1)


# ---------------------------------------------------------------- politicas -------------------------------


def test_politica_bandit_registra_propensao_exata():
    pol = PoliticaBandit(BanditThompsonLinear(3, 5, seed=0))
    estado = {"spread_ticks": 1.0, "desequilibrio": 0.2, "tempo_restante": 0.8, "deriva_ticks": 0.0}
    esc = pol.escolher(estado)
    assert esc in (AGREDIR, PASSIVA, "aguardar") and 0 < pol.ultima_propensao <= 1
    assert isinstance(pol, PoliticaExecucao)
    assert contexto(estado).shape == (5,)


def test_atribuicao_aleatoria_por_ordem_mae_registra_propensao():
    rl = PoliticaBandit(BanditThompsonLinear(3, 5, seed=0))
    pol = PoliticaAtribuicaoAleatoria(rl, PoliticaPadrao(), p_rl=0.3, seed=7)
    estado = {"spread_ticks": 1.0, "tempo_restante": 1.0}
    bracos = []
    for i in range(1000):
        bracos.append(pol.iniciar_ordem_mae(f"m{i}"))
        pol.escolher(estado)
        pol.escolher(estado)
    assert abs(np.mean([b == "rl" for b in bracos]) - 0.3) < 0.05
    assert pol.iniciar_ordem_mae("m3") == bracos[3]  # atribuicao estavel por ordem-mae
    assert len(pol.registros) == 2000
    for rg in pol.registros:
        assert rg.braco in ("rl", "padrao") and 0 < rg.propensao_atribuicao < 1 and 0 < rg.propensao_acao <= 1
        assert rg.propensao == pytest.approx(rg.propensao_atribuicao * rg.propensao_acao)
        assert rg.braco == pol.atribuicao[rg.ordem_mae_id]
        if rg.braco == "padrao":
            assert rg.escolha == AGREDIR and rg.propensao_acao == 1.0
    pol2 = PoliticaAtribuicaoAleatoria(rl, PoliticaPadrao(), p_rl=0.3, seed=7)  # determinismo por seed
    assert [pol2.iniciar_ordem_mae(f"m{i}") for i in range(1000)] == bracos


def test_saida_protecao_nao_e_randomizada_e_agride():
    rl = PoliticaBandit(BanditThompsonLinear(3, 5, seed=0))
    pol = PoliticaAtribuicaoAleatoria(rl, seed=1)
    pol.iniciar_ordem_mae("stop-1")
    for _ in range(20):
        assert pol.escolher({"saida_protecao": 1.0}) == AGREDIR
    assert all(r.braco == "protecao" and r.propensao == 1.0 for r in pol.registros)
    assert rl.escolher({"saida_protecao": 1.0}) == AGREDIR
    with pytest.raises(ValueError):
        PoliticaAtribuicaoAleatoria(rl, p_rl=1.0)
