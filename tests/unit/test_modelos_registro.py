"""Registro de modelos (versao, hashes, promocao com criterio pre-registrado, reversao) e CLI. SINTETICO."""
import json

import numpy as np
import pandas as pd
import pytest

from robowin.cli import main
from robowin.core.tipos import EntradaRegistroModelo
from robowin.modelos.microestrutura import Amostras, CriterioOOS, ModeloMicroestrutura, hash_codigo
from robowin.modelos.registro import ErroRegistro, ModeloVazado, PromocaoRecusada, RegistroModelos

NS = 1_000_000_000
CRIT = CriterioOOS(min_amostras=100, min_selecionadas=10)
BOM = {"n": 1000.0, "ganho_log_loss": 0.03, "ece_alvo": 0.02, "n_selecionadas": 200.0, "exp_sel_brl": 1.5,
       "ic_inf_exp_brl": 0.4}
MELHOR = {**BOM, "exp_sel_brl": 2.0, "ganho_log_loss": 0.04, "ic_inf_exp_brl": 0.9}
RUIM = {**BOM, "ic_inf_exp_brl": -0.3}


def amostras_aleatorias(n=400, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.int64) * 5 * NS
    X = pd.DataFrame({"fluxo_imb_30s": rng.standard_normal(n), "livro_imb_5": rng.standard_normal(n),
                      "rv_5m": rng.random(n)})
    y = rng.integers(0, 3, n)
    return Amostras(t, t + 60 * NS, X, y, y.copy(), rng.standard_normal(n), rng.standard_normal(n))


def entrada(versao, ate=1000):
    return EntradaRegistroModelo(nome="micro", versao=versao, hash_dados="d" * 8, hash_codigo="c" * 8, status="em_validacao",
                                 treinado_ate_ns=ate)


def novo(tmp_path, versoes=("v1",), origem="historico_real"):
    reg = RegistroModelos(tmp_path / "reg.json")
    for v in versoes:
        reg.registrar(entrada(v), origem=origem, criterio_hash=CRIT.hash())
    return reg


def aprovar(reg, v, metricas, periodo="P1"):
    return reg.registrar_oos("micro", v, periodo, metricas, inicio_segmento_ns=5000, criterio=CRIT)


def test_registro_modelo_treinado_guarda_hashes_e_nao_nasce_validado(tmp_path):
    am = amostras_aleatorias()
    m = ModeloMicroestrutura("logistica")
    reg = RegistroModelos(tmp_path / "r.json")
    with pytest.raises(ErroRegistro):
        reg.registrar_modelo(m, "v0")  # nao treinado
    m.treinar(am, origem="sintetico", criterio=CRIT)
    e = reg.registrar_modelo(m, "v1")
    assert e.status == "em_validacao" and e.hash_dados == am.hash_dados() and e.hash_codigo == hash_codigo()
    assert e.treinado_ate_ns == int(am.t_fim_ns.max())
    with pytest.raises(ErroRegistro):
        reg.registrar_modelo(m, "v1")  # versao imutavel
    with pytest.raises(ErroRegistro):
        reg.registrar(entrada("vx").model_copy(update={"status": "validado_oos"}), origem="historico_real")


def test_persistencia_em_arquivo_json(tmp_path):
    reg = novo(tmp_path)
    aprovar(reg, "v1", BOM)
    reg.promover("micro", "v1", "P1", CRIT)
    outro = RegistroModelos(tmp_path / "reg.json")  # recarrega do disco
    assert outro.campeao("micro") == "v1" and outro.entrada("micro", "v1").status == "validado_oos"
    assert json.loads((tmp_path / "reg.json").read_text())["modelos"]["micro"]["campeao"] == "v1"


def test_promocao_exige_oos_registrado_e_criterio_atingido(tmp_path):
    reg = novo(tmp_path, ("v1", "v2"))
    with pytest.raises(PromocaoRecusada, match="sem metrica OOS"):
        reg.promover("micro", "v1", "P1", CRIT)
    assert aprovar(reg, "v1", BOM) is True
    assert aprovar(reg, "v2", RUIM) is False
    assert reg.entrada("micro", "v2").status == "reprovado"
    with pytest.raises(PromocaoRecusada):
        reg.promover("micro", "v2", "P1", CRIT)
    reg.promover("micro", "v1", "P1", CRIT)
    assert reg.entrada("micro", "v1").status == "validado_oos" and reg.campeao("micro") == "v1"


def test_criterio_deve_ser_o_pre_registrado(tmp_path):
    reg = novo(tmp_path)
    aprovar(reg, "v1", BOM)
    frouxo = CriterioOOS(min_amostras=1, min_selecionadas=1, ic_inf_min_brl=-100.0)
    with pytest.raises(PromocaoRecusada, match="pre-registrado"):
        reg.promover("micro", "v1", "P1", frouxo)


def test_dados_sinteticos_nunca_sao_promovidos(tmp_path):
    reg = novo(tmp_path, origem="sintetico")
    aprovar(reg, "v1", BOM)
    with pytest.raises(PromocaoRecusada, match="sintetico"):
        reg.promover("micro", "v1", "P1", CRIT)
    assert reg.campeao("micro") is None


def test_holdout_so_uma_vez_por_periodo(tmp_path):
    reg = novo(tmp_path)
    aprovar(reg, "v1", BOM)
    with pytest.raises(ErroRegistro, match="holdout"):
        aprovar(reg, "v1", MELHOR)


def test_registro_impede_modelo_treinado_depois_do_inicio_do_segmento(tmp_path):
    reg = novo(tmp_path)
    reg.verificar_sem_vazamento("micro", "v1", 1000)  # igual: ok
    with pytest.raises(ModeloVazado):
        reg.verificar_sem_vazamento("micro", "v1", 999)
    with pytest.raises(ModeloVazado):
        reg.registrar_oos("micro", "v1", "P1", BOM, inicio_segmento_ns=500, criterio=CRIT)
    assert reg.oos("micro", "v1") == {}


def test_desafiante_so_vira_campeao_se_superar_o_campeao_no_mesmo_periodo(tmp_path):
    reg = novo(tmp_path, ("v1", "v2", "v3"))
    aprovar(reg, "v1", BOM)
    reg.promover("micro", "v1", "P1", CRIT)
    # v2 aprovado no criterio, mas campeao v1 ainda nao avaliado no periodo P2 -> comparacao impossivel
    aprovar(reg, "v2", MELHOR, "P2")
    with pytest.raises(PromocaoRecusada, match="mesmo periodo"):
        reg.promover("micro", "v2", "P2", CRIT)
    # com o campeao avaliado no mesmo periodo e v2 melhor -> promove
    aprovar(reg, "v1", BOM, "P2")
    reg.promover("micro", "v2", "P2", CRIT)
    assert reg.campeao("micro") == "v2" and reg.historico("micro") == ["v1"]
    # v3 pior que o campeao v2 no mesmo periodo -> recusado
    aprovar(reg, "v2", MELHOR, "P3")
    aprovar(reg, "v3", BOM, "P3")
    with pytest.raises(PromocaoRecusada, match="nao supera"):
        reg.promover("micro", "v3", "P3", CRIT)
    assert reg.campeao("micro") == "v2"


def test_reversao_por_troca_de_ponteiro(tmp_path):
    reg = novo(tmp_path, ("v1", "v2"))
    aprovar(reg, "v1", BOM)
    reg.promover("micro", "v1", "P1", CRIT)
    aprovar(reg, "v1", BOM, "P2")
    aprovar(reg, "v2", MELHOR, "P2")
    reg.promover("micro", "v2", "P2", CRIT)
    assert reg.reverter("micro") == "v1" and reg.campeao("micro") == "v1"
    # nenhum artefato apagado
    assert reg.entrada("micro", "v2").versao == "v2"
    assert any("revertida" in e for e in reg.eventos("micro", "v2"))
    with pytest.raises(ErroRegistro):
        reg.reverter("micro")  # nada mais para reverter


def test_espelho_em_banco():
    class BancoFalso:
        def __init__(self):
            self.linhas = []

        def inserir(self, tabela, linha):
            self.linhas.append((tabela, linha))

        def consultar(self, sql, params=()):
            return []

    b = BancoFalso()
    reg = RegistroModelos(banco=b)
    reg.registrar(entrada("v1"), origem="historico_real")
    assert b.linhas[0][0] == "modelos" and b.linhas[0][1]["versao"] == "v1"


def test_cli_treinar_sinteticos_e_rotulado_e_nao_valida(tmp_path, capsys):
    arq = tmp_path / "registro.json"
    rc = main(["treinar", "--dados", "sinteticos", "--ticks", "60000", "--dias", "3", "--registro", str(arq)])
    saida = capsys.readouterr().out
    assert rc == 0 and "SINTETICO" in saida and "holdout" in saida
    reg = RegistroModelos(arq)
    d = json.loads(arq.read_text())["modelos"]
    nome = next(iter(d))
    versao = next(iter(d[nome]["versoes"]))
    assert reg.entrada(nome, versao).status in ("em_validacao", "reprovado")  # jamais validado_oos
    assert reg.campeao(nome) is None
