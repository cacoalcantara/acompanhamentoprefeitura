"""Recuperacao de um sinal fraco CONHECIDO e ausencia de edge em ruido puro. Dados 100% SINTETICOS.

O gerador (robowin.modelos.demo_sintetico) tem um fluxo latente persistente; com forca_sinal > 0 ele
desloca levemente o preco (sinal fraco conhecido); com forca_sinal = 0 nao ha relacao com o preco.
"""
import numpy as np
import pandas as pd
import pytest

from robowin.core.tipos import VetorFeatures
from robowin.features.registro import FeatureProibida
from robowin.modelos.demo_sintetico import gerar, montar_amostras
from robowin.modelos.microestrutura import (
    Amostras,
    CriterioAusente,
    CriterioNaoPreRegistrado,
    CriterioOOS,
    HoldoutJaUsado,
    ModeloMicroestrutura,
    ModeloNaoTreinado,
    VazamentoOOS,
    metricas_oos,
)
from robowin.modelos.walkforward import executar_walkforward

NS = 1_000_000_000
EMBARGO = 60 * NS
CRITERIO = CriterioOOS()


@pytest.fixture(scope="module")
def com_sinal():
    return montar_amostras(*gerar(200_000, seed=1, forca_sinal=0.1, n_dias=5))


@pytest.fixture(scope="module")
def forte():
    """Sinal mais forte (ainda sintetico) para exercitar os caminhos de status com poucos dados."""
    return montar_amostras(*gerar(140_000, seed=1, forca_sinal=0.2, n_dias=4))


@pytest.fixture(scope="module")
def sem_sinal():
    return montar_amostras(*gerar(200_000, seed=1, forca_sinal=0.0, n_dias=5))


def _prior(am):
    return {lado: np.bincount(am.y(lado), minlength=3) / len(am) for lado in ("compra", "venda")}


def _wf(am, tipo):
    wf = executar_walkforward(am, lambda: ModeloMicroestrutura(tipo), n_dobras=4, embargo_ns=EMBARGO)
    sub = am.subconjunto(wf.idx_oos)
    pred = wf.pred.loc[wf.idx_oos].reset_index(drop=True)
    m = metricas_oos(pred, sub, _prior(am.subconjunto(wf.dobras[0].idx_treino)), CRITERIO)
    return wf, sub, pred, m


def _corr_direcao(pred, sub):
    return float(np.corrcoef(pred.e_brl_compra - pred.e_brl_venda, sub.pnl_compra - sub.pnl_venda)[0, 1])


def _treino_oos(am, frac=0.6):
    corte = int(len(am) * frac)
    ini_oos = int(am.t_ns[corte])
    tr = am.subconjunto(np.flatnonzero((np.arange(len(am)) < corte) & (am.t_fim_ns <= ini_oos - EMBARGO)))
    return tr, am.subconjunto(np.arange(corte, len(am))), ini_oos


# ---- o pipeline recupera o sinal conhecido -------------------------------------------------------
@pytest.mark.parametrize("tipo", ["lightgbm", "logistica"])
def test_recupera_sinal_fraco_conhecido_fora_da_amostra(com_sinal, tipo):
    wf, sub, pred, m = _wf(com_sinal, tipo)
    assert _corr_direcao(pred, sub) > 0.15
    assert m["ganho_log_loss"] > 0.005  # melhor que o prior fora da amostra
    assert m["ece_alvo"] < 0.06
    # a faixa mais confiante ganha mais que a menos confiante
    d = (pred.e_brl_compra - pred.e_brl_venda).to_numpy()
    q_alto, q_baixo = d >= np.quantile(d, 0.8), d <= np.quantile(d, 0.2)
    liq = (sub.pnl_compra - sub.pnl_venda)
    assert liq[q_alto].mean() > liq[q_baixo].mean() + 2.0
    # purga/embargo respeitados em todas as dobras
    for d_, ate in zip(wf.dobras, wf.treinado_ate_ns, strict=True):
        assert ate <= d_.teste_ini_ns - EMBARGO


def test_lightgbm_com_sinal_atinge_o_criterio_pre_registrado(com_sinal):
    _, _, _, m = _wf(com_sinal, "lightgbm")
    ok, motivos = CRITERIO.aprova(m)
    assert m["ic_inf_exp_brl"] > 0 and m["exp_sel_brl"] > 0, m
    assert ok, motivos


def test_features_informativas_apontam_para_o_fluxo(com_sinal):
    dif = com_sinal.pnl_compra - com_sinal.pnl_venda
    for c in ("fluxo_imb_30s", "livro_imb_5"):
        assert np.corrcoef(np.nan_to_num(com_sinal.X[c]), dif)[0, 1] > 0.1


# ---- sem sinal nao ha edge -----------------------------------------------------------------------
@pytest.mark.parametrize("tipo", ["lightgbm", "logistica"])
def test_ruido_puro_nao_gera_edge_falso(sem_sinal, tipo):
    _, sub, pred, m = _wf(sem_sinal, tipo)
    assert abs(_corr_direcao(pred, sub)) < 0.08
    assert m["ganho_log_loss"] < 0.005  # nada de ganho de informacao
    assert m["exp_base_brl"] < 0  # custos tornam a expectativa ingenua negativa
    assert m["ic_inf_exp_brl"] <= 0  # nunca "lucro" significativo
    ok, _ = CRITERIO.aprova(m)
    assert not ok


def test_ruido_puro_holdout_unico_reprova(sem_sinal):
    tr, oos, _ = _treino_oos(sem_sinal)
    mod = ModeloMicroestrutura("lightgbm")
    mod.treinar(tr, origem="historico_real", criterio=CRITERIO)
    mod.avaliar_oos(oos, CRITERIO)
    assert mod.status == "reprovado"


# ---- honestidade de status -----------------------------------------------------------------------
def test_status_nunca_validado_sem_criterio_e_sem_dados_reais(forte):
    tr, oos, _ = _treino_oos(forte)
    mod = ModeloMicroestrutura("lightgbm")
    assert mod.status == "nao_treinado"
    with pytest.raises(ModeloNaoTreinado):
        mod.prever(VetorFeatures(t_ns=0, valores={}))
    with pytest.raises(ModeloNaoTreinado):
        mod.avaliar_oos(oos, CRITERIO)
    mod.treinar(tr, origem="sintetico", criterio=CRITERIO)
    assert mod.status == "em_validacao"
    v = mod.prever(VetorFeatures(t_ns=int(oos.t_ns[0]), valores=oos.X.iloc[0].to_dict()))
    assert v["validado"] == 0.0 and abs(v["p_alvo_compra"] + v["p_stop_compra"] + v["p_tempo_compra"] - 1) < 1e-9
    assert {"e_brl_compra", "e_brl_venda", "p_alvo_venda", "p_stop_venda", "p_tempo_venda"} <= set(v)
    m = mod.avaliar_oos(oos, CRITERIO)
    assert CRITERIO.aprova(m)[0]  # passou no criterio, mas...
    assert mod.status == "em_validacao"  # ...dados sinteticos nunca recebem o selo
    assert mod.status != "validado_oos"


def test_validado_oos_so_com_origem_real_e_criterio(forte):
    # a 'origem' real e apenas declarada aqui para exercitar o caminho; os dados continuam sinteticos.
    tr, oos, _ = _treino_oos(forte)
    mod = ModeloMicroestrutura("lightgbm")
    mod.treinar(tr, origem="historico_real", criterio=CRITERIO)
    mod.avaliar_oos(oos, CRITERIO)
    assert mod.status == "validado_oos"
    assert mod.prever(VetorFeatures(t_ns=0, valores=oos.X.iloc[0].to_dict()))["validado"] == 1.0


def test_criterio_deve_ser_pre_registrado_holdout_unico_e_sem_vazamento(com_sinal):
    tr, oos, _ = _treino_oos(com_sinal)
    sem_criterio = ModeloMicroestrutura("logistica")
    sem_criterio.treinar(tr, origem="historico_real")
    with pytest.raises(CriterioAusente):
        sem_criterio.avaliar_oos(oos, CRITERIO)
    assert sem_criterio.status == "em_validacao"

    mod = ModeloMicroestrutura("logistica")
    mod.treinar(tr, origem="historico_real", criterio=CRITERIO)
    outro = CriterioOOS(ganho_log_loss_min=-1.0, ic_inf_min_brl=-1e9)  # afrouxado depois de ver os dados
    with pytest.raises(CriterioNaoPreRegistrado):
        mod.avaliar_oos(oos, outro)
    with pytest.raises(VazamentoOOS):  # holdout que se sobrepoe ao treino
        mod.avaliar_oos(com_sinal.subconjunto(np.arange(len(com_sinal) // 3)), CRITERIO)
    assert mod.status == "em_validacao"
    mod.avaliar_oos(oos, CRITERIO)
    with pytest.raises(HoldoutJaUsado):
        mod.avaliar_oos(oos, CRITERIO)


def test_recusa_feature_fora_do_registro_no_treino(com_sinal):
    tr, _, _ = _treino_oos(com_sinal)
    X = tr.X.copy()
    X["rsi_14"] = 50.0  # lista-de-proibicao: tentativa deliberada de indicador tecnico
    ruim = Amostras(tr.t_ns, tr.t_fim_ns, X, tr.y_compra, tr.y_venda, tr.pnl_compra, tr.pnl_venda)
    with pytest.raises(FeatureProibida):
        ModeloMicroestrutura("logistica").treinar(ruim)
    mod = ModeloMicroestrutura("logistica")
    mod.treinar(tr)
    with pytest.raises(FeatureProibida):
        mod.prever_lote(pd.DataFrame({"macd": [0.0]}))  # lista-de-proibicao


def test_treino_e_deterministico(com_sinal):
    tr, oos, _ = _treino_oos(com_sinal)
    a, b = ModeloMicroestrutura("lightgbm"), ModeloMicroestrutura("lightgbm")
    a.treinar(tr)
    b.treinar(tr)
    pd.testing.assert_frame_equal(a.prever_lote(oos.X), b.prever_lote(oos.X))
    assert a.hash_dados == b.hash_dados == tr.hash_dados()
