"""VaR/ES historico (monitoramento, nunca veto), impressao da ordem aprovada pelo risco, PSI e custo de execucao."""
from __future__ import annotations

import ast
import math
from pathlib import Path

import numpy as np
import pytest
from perfis_teste import WIN

from robowin.avaliacao.monitoramento import (
    PSI_ALERTA,
    RastreadorSlippage,
    classificar_psi,
    deriva_de_features,
    psi,
    psi_amostras,
)
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    AckOrdem,
    ComandoOrdem,
    ConfigRisco,
    DecisaoRisco,
    Intencao,
    Lado,
    Posicao,
)
from robowin.execucao.aprovacao import ErroOrdemNaoAprovada, RegistroAprovacoes, impressao_ordem
from robowin.execucao.oms import OMS
from robowin.execucao.roteador import Roteador
from robowin.risco.cauda import (
    backtest_var_deslizante,
    kupiec_pof,
    relatorio_cauda,
    var_es_historico,
)
from robowin.risco.motor import MotorRiscoPadrao

RAIZ = Path(__file__).resolve().parents[2] / "src" / "robowin"
INS = WIN.instrumento("WINZ26")


# ================================================================================ VaR / ES ==
def test_var_es_historico_em_serie_conhecida():
    pnl = list(range(-100, 100))  # 200 dias de -100 a +99
    var, es = var_es_historico(pnl, 0.95)
    assert var == pytest.approx(90.05, abs=0.1) and es >= var and es == pytest.approx(95.0, abs=1.0)
    assert var_es_historico([5.0, 6.0, 7.0], 0.95)[0] == 0.0  # sem dia de perda: VaR 0 (ganho)
    with pytest.raises(ValueError):
        var_es_historico([], 0.95)
    with pytest.raises(ValueError):
        var_es_historico([1.0], 0.2)


def test_var_ignora_nao_finitos():
    assert var_es_historico([float("nan"), -10.0, -20.0, 5.0, float("inf")], 0.9)[0] > 0


def test_kupiec_aceita_taxa_esperada_e_rejeita_taxa_muito_diferente():
    ok = kupiec_pof(500, 25, 0.95)  # 5% de violacoes = esperado
    assert ok["p_valor"] > 0.9 and ok["lr"] == pytest.approx(0.0, abs=0.01)
    ruim = kupiec_pof(500, 60, 0.95)  # 12%
    assert ruim["p_valor"] < 0.001
    zero = kupiec_pof(100, 0, 0.99)
    assert 0 <= zero["p_valor"] <= 1 and not math.isnan(zero["lr"])
    with pytest.raises(ValueError):
        kupiec_pof(10, 11)


def test_backtest_deslizante_nao_olha_o_dia_avaliado():
    rng = np.random.default_rng(0)
    pnl = rng.normal(0, 100, 400)
    r = backtest_var_deslizante(pnl, janela=60, nivel=0.95)
    assert r["n_obs"] == 340 and 0.0 <= r["taxa"] <= 0.15
    with pytest.raises(ValueError):
        backtest_var_deslizante(pnl[:50], janela=60)


def test_relatorio_cauda_avisa_amostra_curta_e_compara_com_limites_sem_vetar():
    cfg = ConfigRisco(perda_max_dia_brl=200.0)
    r = relatorio_cauda([-300.0, 50.0, -20.0, 80.0, -150.0], cfg)
    assert r.dias == 5 and r.pior_dia_brl == 300.0 and r.niveis["0.95"]["var_brl"] > 0
    assert any("amostra curta" in a for a in r.avisos) and any("passado" in a for a in r.avisos)
    assert any("informativo" in a or "perda_max_dia_brl" in a for a in r.alertas_limites)
    vazio = relatorio_cauda([])
    assert vazio.dias == 0 and "sem dias" in vazio.avisos[0]
    assert set(r.como_dict()) == {"dias", "niveis", "pior_dia_brl", "avisos", "alertas_limites"}


def test_cauda_e_so_monitoramento_nenhum_modulo_de_execucao_ou_risco_a_importa():
    """O VaR/ES nunca entra no caminho do veto: nem o motor, nem o OMS, nem o roteador, nem as decisoes."""
    alvos = [p for pasta in ("risco", "execucao", "decisao", "mt5") for p in (RAIZ / pasta).rglob("*.py")
             if p.name != "cauda.py"]
    for arq in alvos:
        for no in ast.walk(ast.parse(arq.read_text(encoding="utf-8"))):
            nomes = ([no.module or ""] if isinstance(no, ast.ImportFrom)
                     else [a.name for a in no.names] if isinstance(no, ast.Import) else [])
            mods = [n for n in nomes if n.endswith("cauda") or n.endswith("risco.cauda")]
            ids = [a.name for a in no.names] if isinstance(no, ast.ImportFrom) else []
            assert not mods and "cauda" not in ids, f"{arq} importa risco.cauda"


# ================================================================================ aprovacao da ordem ==
def cmd(**kw) -> ComandoOrdem:
    base = {"intencao_id": "i1", "magic": 1001, "lado": Lado.COMPRA, "tipo": "mercado", "quantidade": 1,
            "preco": None, "sl": 129_970.0, "tp": 130_040.0}
    base.update(kw)
    return ComandoOrdem(**base)


def test_impressao_muda_com_qualquer_campo_da_ordem():
    c = cmd()
    base = impressao_ordem(c)
    assert len(base) == 16 and impressao_ordem(cmd()) == base
    for kw in ({"sl": 129_965.0}, {"tp": 130_045.0}, {"quantidade": 2}, {"lado": Lado.VENDA}, {"tipo": "limitada"},
               {"preco": 130_000.0}, {"magic": 1002}, {"intencao_id": "i2"}):
        assert impressao_ordem(cmd(**kw)) != base, kw


def test_registro_so_aceita_aprovacao_de_risco_da_mesma_intencao_e_confere_a_ordem_exata():
    reg = RegistroAprovacoes()
    c = cmd()
    with pytest.raises(ErroOrdemNaoAprovada):
        reg.conferir(c)  # nunca aprovada
    with pytest.raises(ErroOrdemNaoAprovada):
        reg.aprovar(c, DecisaoRisco(intencao_id="i1", aprovada=False, motivos=("limite",)))
    with pytest.raises(ErroOrdemNaoAprovada):
        reg.aprovar(c, DecisaoRisco(intencao_id="outra", aprovada=True))
    reg.aprovar(c, DecisaoRisco(intencao_id="i1", aprovada=True))
    reg.conferir(c)
    with pytest.raises(ErroOrdemNaoAprovada):
        reg.conferir(cmd(sl=129_900.0))  # stop mais largo que o aprovado
    assert len(reg) == 1


def _roteador(exigir: bool):
    rel = RelogioSimulado(1_000_000_000_000)
    enviados: list[ComandoOrdem] = []

    def emissor(c: ComandoOrdem):
        enviados.append(c)
        return AckOrdem(retcode=10009, order=7)

    oms = OMS(rel, {"b1": 1001})
    motor = MotorRiscoPadrao(ConfigRisco(), INS)
    return Roteador("gateway_demo", INS, rel, oms, motor, emissor_gateway=emissor, exigir_aprovacao=exigir), enviados


def test_roteador_nao_envia_entrada_alterada_depois_do_veto_mas_nunca_trava_saida():
    rot, enviados = _roteador(True)
    # entrada montada pelo proprio roteador a partir de uma intencao aprovada: passa
    import datetime as dt

    from robowin.core.calendario_b3 import dt_utc_ns
    t = dt_utc_ns(dt.datetime(2026, 11, 10, 10, 0))
    it = Intencao(id="i1", braco="b1", acao=Acao.COMPRAR, stop_pontos=30.0, alvo_pontos=40.0, validade_ns=t + 10**11,
                  preco_referencia=130_000.0, t0_ns=t)
    rot.relogio.avancar_para(t)
    r = rot.executar_intencao(it, Posicao(simbolo="WINZ26"), t, {})
    assert r.decisao.aprovada, r.decisao.motivos
    assert len(enviados) == 1 and len(rot.aprovacoes) == 1
    # a MESMA intencao com ordem adulterada (stop mais largo) chamada direto no executor: recusada
    adulterada = r.cmd.model_copy(update={"sl": r.cmd.sl - 500.0})
    with pytest.raises(ErroOrdemNaoAprovada):
        rot.enviar(adulterada)
    # entrada sem nenhuma aprovacao: recusada
    with pytest.raises(ErroOrdemNaoAprovada):
        rot.enviar(cmd(intencao_id="nunca-passou-pelo-risco"))
    # saida (zeragem/stop/kill) nunca espera aprovacao
    assert rot.enviar(cmd(intencao_id="zerar-x", tipo="saida", lado=Lado.VENDA, sl=None, tp=None)).retcode == 10009


def test_sem_exigir_aprovacao_o_comportamento_antigo_e_preservado():
    rot, enviados = _roteador(False)
    rot.enviar(cmd(intencao_id="solto"))
    assert len(enviados) == 1


def test_orquestrador_backtest_e_espelho_exigem_aprovacao():
    for arq in ("decisao/orquestrador.py", "backtest/executor.py", "decisao/espelho.py", "mm/ponte_demo.py"):
        assert "exigir_aprovacao=True" in (RAIZ / arq).read_text(encoding="utf-8"), arq


# ================================================================================ PSI e slippage ==
def test_psi_zero_para_distribuicoes_iguais_e_alto_para_deslocamento():
    assert psi([10, 20, 30, 40], [10, 20, 30, 40]) == pytest.approx(0.0, abs=1e-9)
    assert psi([25, 25, 25, 25], [70, 10, 10, 10]) > PSI_ALERTA
    assert math.isnan(psi([0, 0], [1, 1]))
    with pytest.raises(ValueError):
        psi([1, 2], [1, 2, 3])


def test_psi_amostras_detecta_deslocamento_e_nao_dispara_para_a_mesma_distribuicao():
    rng = np.random.default_rng(1)
    ref = rng.normal(0, 1, 2000)
    assert classificar_psi(psi_amostras(ref, rng.normal(0, 1, 2000))) == "estavel"
    assert classificar_psi(psi_amostras(ref, rng.normal(1.5, 1, 2000))) == "alerta"
    assert math.isnan(psi_amostras(ref[:10], ref))  # referencia curta demais
    assert classificar_psi(float("nan")) == "indisponivel"


def test_deriva_de_features_pega_o_pior_nivel_e_ignora_features_sem_par():
    rng = np.random.default_rng(2)
    ref = {"a": rng.normal(0, 1, 1000), "b": rng.normal(0, 1, 1000), "so_na_ref": rng.normal(0, 1, 1000)}
    atu = {"a": rng.normal(0, 1, 1000), "b": rng.normal(3, 1, 1000)}
    d = deriva_de_features(ref, atu)
    assert set(d["psi"]) == {"a", "b"} and d["niveis"]["a"] == "estavel" and d["pior"] == "alerta"
    assert deriva_de_features({}, {})["pior"] == "indisponivel"


def test_rastreador_de_slippage_so_alerta_com_amostra_e_custo_muito_acima_do_esperado():
    r = RastreadorSlippage(janela=20, min_execucoes=5)
    for _ in range(4):
        r.registrar(1, 130_015.0, 130_000.0, INS.tick_pontos, esperado_ticks=1.0)
    assert r.alerta() is None  # poucas execucoes
    r.registrar(1, 130_015.0, 130_000.0, INS.tick_pontos, esperado_ticks=1.0)
    assert r.alerta() and "otimista" in r.alerta()  # 3 ticks reais vs 1 esperado
    ok = RastreadorSlippage(min_execucoes=3)
    for _ in range(5):
        ok.registrar(-1, 129_995.0, 130_000.0, INS.tick_pontos, esperado_ticks=1.0)  # venda: custo 1 tick
    assert ok.alerta() is None and ok.resumo()["razao"] == pytest.approx(1.0)
    with pytest.raises(ValueError):
        ok.registrar(0, 1.0, 1.0, INS.tick_pontos, 1.0)
