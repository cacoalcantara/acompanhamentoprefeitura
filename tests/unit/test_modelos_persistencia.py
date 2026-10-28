"""Artefato persistente do modelo (salvar/carregar seguro, versionado, por ativo). Dados 100% SINTETICOS."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from perfis_teste import WDO, WIN, amostras

from robowin.core.instrumentos import AtivoDivergente
from robowin.decisao.cli_cmds import _modelo_do_ativo
from robowin.modelos.demo_sintetico import gerar
from robowin.modelos.microestrutura import CriterioOOS, ModeloMicroestrutura
from robowin.modelos.persistencia import (
    ErroArtefato,
    carregar_artefato,
    carregar_para_ativo,
    carregar_versao,
    registrar_e_salvar,
    salvar_artefato,
)
from robowin.modelos.registro import RegistroModelos
from robowin.modelos.rotulagem import ConfigBarreiras, Custos

CRIT = CriterioOOS(min_amostras=100, min_selecionadas=10)
BOM = {"n": 1000.0, "ganho_log_loss": 0.03, "ece_alvo": 0.02, "n_selecionadas": 200.0, "exp_sel_brl": 1.5,
       "ic_inf_exp_brl": 0.4}
MELHOR = {**BOM, "exp_sel_brl": 2.0, "ganho_log_loss": 0.04, "ic_inf_exp_brl": 0.9}
BARREIRAS = ConfigBarreiras.do_perfil(WIN)
CUSTOS = Custos.do_perfil(WIN)


@pytest.fixture(scope="module")
def am():
    return amostras(WIN, *gerar(WIN, 60_000, seed=1, forca_sinal=0.2, n_dias=3))


def treinar(am, tipo="logistica", origem="sintetico", seed=7, ativo="WIN"):
    m = ModeloMicroestrutura(tipo, seed=seed)
    m.treinar(am, origem=origem, criterio=CRIT)
    m.ativo = ativo
    return m


@pytest.fixture(scope="module", params=["lightgbm", "logistica"])
def modelo(request, am):
    return treinar(am, request.param)


def reg_com(tmp_path, modelo, versao="v1"):
    reg = RegistroModelos(tmp_path / "WIN" / "registro.json")
    registrar_e_salvar(reg, modelo, versao, barreiras=BARREIRAS, custos=CUSTOS)
    return reg


def _arq(reg, modelo, versao="v1") -> Path:
    return reg.caminho.parent / "artefatos" / reg.artefato(modelo.nome, versao)["arquivo"]


# ---- ida e volta ---------------------------------------------------------------------------------
def test_ida_e_volta_reproduz_previsoes_identicas(modelo, am, tmp_path):
    reg = reg_com(tmp_path, modelo)
    novo = carregar_versao(reg, modelo.nome, "v1", "WIN")
    X = am.X.copy()
    X.iloc[::7, 0] = np.nan  # ausentes tambem devem dar o mesmo resultado
    a, b = modelo.prever_lote(X), novo.prever_lote(X)
    assert a.shape == b.shape and np.array_equal(a.to_numpy(), b.to_numpy())
    assert list(a.columns) == list(b.columns)
    v = am.X.iloc[3].to_dict()
    from robowin.core.tipos import VetorFeatures
    assert modelo.prever(VetorFeatures(t_ns=1, valores=v)) == novo.prever(VetorFeatures(t_ns=1, valores=v))
    # metadados completos
    assert novo.ativo == "WIN" and novo.origem_dados == "sintetico" and novo.status == "em_validacao"
    assert novo.hash_dados == modelo.hash_dados and novo.treinado_ate_ns == modelo.treinado_ate_ns
    assert novo.janela_treino_ns == modelo.janela_treino_ns and novo.janela_treino_ns[0] < novo.janela_treino_ns[1]
    assert novo.hash_criterio_pre_registrado == CRIT.hash() and novo.colunas == modelo.colunas
    assert novo.config_barreiras["alvo_pontos"] == BARREIRAS.alvo_pontos
    assert novo.custos["slippage_stop_pontos"] == CUSTOS.slippage_stop_pontos
    # idempotente: salvar o carregado produz o mesmo conteudo
    salvar_artefato(novo, tmp_path / "x.json", versao="v1", barreiras=BARREIRAS, custos=CUSTOS)
    assert np.array_equal(carregar_artefato(tmp_path / "x.json", ativo="WIN").prever_lote(X).to_numpy(), a.to_numpy())


def test_formato_nao_usa_pickle(modelo, tmp_path):
    reg = reg_com(tmp_path, modelo)
    bruto = _arq(reg, modelo).read_bytes()
    doc = json.loads(bruto)
    assert doc["formato"] and doc["sha256"] and bruto[:1] == b"{"
    assert b"pickle" not in bruto.lower()


# ---- adulteracao / corrupcao ---------------------------------------------------------------------
def test_conteudo_adulterado_com_hash_antigo_recusa(modelo, tmp_path):
    reg = reg_com(tmp_path, modelo)
    arq = _arq(reg, modelo)
    doc = json.loads(arq.read_text())
    doc["payload"]["estado"]["payoff"]["compra"][0] += 10.0
    arq.write_text(json.dumps(doc))
    with pytest.raises(ErroArtefato):
        carregar_artefato(arq, ativo="WIN")
    r = carregar_para_ativo(reg, modelo.nome, "WIN")
    assert r.modelo is None and "hash" in r.motivo


def test_payload_e_hash_reescritos_sao_barrados_pelo_registro(modelo, tmp_path):
    from robowin.modelos.persistencia import _canonico, _sha
    reg = reg_com(tmp_path, modelo)
    arq = _arq(reg, modelo)
    doc = json.loads(arq.read_text())
    doc["payload"]["estado"]["payoff"]["compra"][0] += 10.0
    doc["sha256"] = _sha(_canonico(doc["payload"]))  # atacante recalcula o hash interno
    arq.write_text(json.dumps(doc))
    assert carregar_artefato(arq, ativo="WIN") is not None  # internamente consistente...
    r = carregar_para_ativo(reg, modelo.nome, "WIN")  # ...mas o registro guarda o sha do arquivo original
    assert r.modelo is None and "registrado" in r.motivo


def test_hash_de_dados_ou_codigo_divergente_do_registro_recusa(modelo, tmp_path):
    reg = reg_com(tmp_path, modelo)
    arq = _arq(reg, modelo)
    e = reg.entrada(modelo.nome, "v1")
    for campo in ("hash_dados", "hash_codigo"):
        ruim = e.model_copy(update={campo: "0" * 64})
        with pytest.raises(ErroArtefato, match="diverge"):
            carregar_artefato(arq, ativo="WIN", entrada=ruim)


def test_hash_do_codigo_atual_divergente_recusa(modelo, tmp_path, monkeypatch):
    reg = reg_com(tmp_path, modelo)
    monkeypatch.setattr("robowin.modelos.persistencia.hash_codigo", lambda: "f" * 64)
    r = carregar_para_ativo(reg, modelo.nome, "WIN")
    assert r.modelo is None and "codigo" in r.motivo


@pytest.mark.parametrize("lixo", [b"", b"\x00\xff nao e json", b"{", b"[]", b'{"formato": "outro"}',
                                  json.dumps({"formato": "robowin-modelo-microestrutura", "versao_formato": 1,
                                              "sha256": "x", "payload": {"meta": 1}}).encode()])
def test_arquivo_corrompido_recusa_sem_excecao_nao_tratada(modelo, tmp_path, lixo):
    reg = reg_com(tmp_path, modelo)
    _arq(reg, modelo).write_bytes(lixo)
    r = carregar_para_ativo(reg, modelo.nome, "WIN")
    assert r.modelo is None and r.motivo
    with pytest.raises(ErroArtefato):  # API estrita: so ErroArtefato, nunca KeyError/JSONDecodeError
        carregar_artefato(_arq(reg, modelo), ativo="WIN")


def test_arquivo_ausente_e_registro_corrompido(modelo, tmp_path):
    reg = reg_com(tmp_path, modelo)
    _arq(reg, modelo).unlink()
    assert carregar_para_ativo(reg, modelo.nome, "WIN").modelo is None
    assert carregar_para_ativo(tmp_path / "nao_existe.json", modelo.nome, "WIN").modelo is None
    ruim = tmp_path / "ruim.json"
    ruim.write_text("{nao json")
    r = carregar_para_ativo(ruim, modelo.nome, "WIN")
    assert r.modelo is None and r.motivo


def test_features_fora_do_registro_permitido_recusa(modelo, tmp_path):
    from robowin.modelos.persistencia import _canonico, _sha
    arq = tmp_path / "a.json"
    salvar_artefato(modelo, arq, versao="v1")
    doc = json.loads(arq.read_text())
    doc["payload"]["meta"]["features"][0] = "sma_20"
    doc["sha256"] = _sha(_canonico(doc["payload"]))
    arq.write_text(json.dumps(doc))
    with pytest.raises(ErroArtefato):
        carregar_artefato(arq, ativo="WIN")


# ---- ativo ---------------------------------------------------------------------------------------
def test_ativo_diferente_recusa(modelo, tmp_path):
    reg = reg_com(tmp_path, modelo)
    with pytest.raises(AtivoDivergente):
        carregar_versao(reg, modelo.nome, "v1", WDO.simbolo_base)
    with pytest.raises(AtivoDivergente):
        carregar_artefato(_arq(reg, modelo), ativo=WDO.simbolo_base)
    r = carregar_para_ativo(reg, modelo.nome, WDO.simbolo_base)
    assert r.modelo is None and "WIN" in r.motivo


def test_modelo_sem_ativo_nao_e_salvo(am, tmp_path):
    m = treinar(am, ativo="")
    with pytest.raises(ErroArtefato):
        salvar_artefato(m, tmp_path / "a.json", versao="v1")
    with pytest.raises(ErroArtefato):
        salvar_artefato(ModeloMicroestrutura("logistica"), tmp_path / "b.json", versao="v1")  # nao treinado


# ---- origem / selo -------------------------------------------------------------------------------
@pytest.mark.parametrize("origem", ["sintetico", "contaminado", "desconhecida"])
def test_origem_sem_selo_nunca_vira_validado(am, tmp_path, origem):
    m = treinar(am, origem=origem)
    reg = reg_com(tmp_path, m)
    assert reg.entrada(m.nome, "v1").status == "em_validacao"
    # forcar o status no objeto e no registro: nada disso carrega como validado
    m._status = "validado_oos"
    with pytest.raises(ErroArtefato):
        salvar_artefato(m, tmp_path / "forcado.json", versao="v2")
    bruto = json.loads(reg.caminho.read_text())
    bruto["modelos"][m.nome]["versoes"]["v1"]["entrada"]["status"] = "validado_oos"
    bruto["modelos"][m.nome]["campeao"] = "v1"
    reg.caminho.write_text(json.dumps(bruto))
    r = carregar_para_ativo(reg.caminho, m.nome, "WIN")
    assert r.modelo is None and "validado_oos" in r.motivo


def test_origem_sintetica_carrega_como_exploratorio_rotulado(modelo, tmp_path):
    reg = reg_com(tmp_path, modelo)
    r = carregar_para_ativo(reg, modelo.nome, "WIN")
    assert r.carregado and r.papel == "exploratorio" and r.versao == "v1"
    assert r.modelo.status == "em_validacao" and r.modelo.origem_dados == "sintetico"
    assert r.modelo.prever(__import__("robowin.core.tipos", fromlist=["x"]).VetorFeatures(
        t_ns=1, valores={}))["validado"] == 0.0


def test_reprovado_nao_e_carregado(modelo, tmp_path):
    reg = reg_com(tmp_path, modelo)
    reg.reprovar(modelo.nome, "v1", "teste")
    r = carregar_para_ativo(reg, modelo.nome, "WIN")
    assert r.modelo is None


# ---- campeao e reversao --------------------------------------------------------------------------
def test_reversao_por_ponteiro_recarrega_o_anterior(am, tmp_path):
    m1, m2 = treinar(am, "logistica", "historico_real", seed=1), treinar(am, "lightgbm", "historico_real", seed=2)
    m2.nome = m1.nome = "microestrutura_x"  # mesmo grupo de versoes
    reg = RegistroModelos(tmp_path / "WIN" / "registro.json")
    registrar_e_salvar(reg, m1, "v1")
    registrar_e_salvar(reg, m2, "v2")
    reg.registrar_oos(m1.nome, "v1", "P1", BOM, inicio_segmento_ns=m1.treinado_ate_ns + 1, criterio=CRIT)
    reg.registrar_oos(m1.nome, "v2", "P1", MELHOR, inicio_segmento_ns=m1.treinado_ate_ns + 1, criterio=CRIT)
    reg.promover(m1.nome, "v1", "P1", CRIT)
    reg.promover(m1.nome, "v2", "P1", CRIT)
    r2 = carregar_para_ativo(reg, m1.nome, "WIN")
    assert r2.versao == "v2" and r2.papel == "campeao" and r2.modelo.status == "validado_oos"
    assert np.array_equal(r2.modelo.prever_lote(am.X).to_numpy(), m2.prever_lote(am.X).to_numpy())
    assert reg.reverter(m1.nome) == "v1"
    r1 = carregar_para_ativo(RegistroModelos(reg.caminho), m1.nome, "WIN")  # registro relido do disco
    assert r1.versao == "v1" and r1.modelo.status == "validado_oos"
    assert np.array_equal(r1.modelo.prever_lote(am.X).to_numpy(), m1.prever_lote(am.X).to_numpy())
    assert not np.array_equal(r1.modelo.prever_lote(am.X).to_numpy(), r2.modelo.prever_lote(am.X).to_numpy())


# ---- BracoQuant / sessao simulada ----------------------------------------------------------------
def test_sem_artefato_quant_segue_nao_treinado_e_nao_opera(tmp_path, capsys):
    from test_decisao_comum import ctx_basico, dossie_basico

    from robowin.decisao.bracos.base import Geometria
    from robowin.decisao.bracos.quant import BracoQuant
    m = _modelo_do_ativo("WIN", "lightgbm", str(tmp_path / "inexistente" / "registro.json"))
    assert m.status == "nao_treinado" and m.ativo == "WIN"
    assert "nao_treinado" in capsys.readouterr().out
    dossie = dossie_basico(extra_modelos={m.nome: {"status": m.status, "saida": {}}})
    r = asyncio.run(BracoQuant("quant", modelo=m.nome, geometria=Geometria(30, 30, 60)).adecidir(ctx_basico(dossie)))
    assert r.resultado == "bloqueada" and r.intencao is None
    assert "modelo_status_inadequado:nao_treinado" in r.motivos


def test_cli_treinar_grava_artefato_no_registro_do_ativo(tmp_path):
    from robowin.cli import main
    registro = tmp_path / "WIN" / "registro.json"
    assert main(["treinar", "--dados", "sinteticos", "--ticks", "60000", "--dias", "3", "--tipo", "logistica",
                 "--registro", str(registro)]) == 0
    reg = RegistroModelos(registro)
    nome = "microestrutura_logistica"
    v = reg.versoes(nome)[0]
    assert reg.entrada(nome, v).ativo == "WIN" and reg.artefato(nome, v)
    assert reg.entrada(nome, v).status in ("em_validacao", "reprovado") and reg.campeao(nome) is None
    if reg.entrada(nome, v).status == "reprovado":  # reprovado nunca e carregado: quant segue nao_treinado
        assert carregar_para_ativo(reg, nome, "WIN").modelo is None
    else:
        assert carregar_versao(reg, nome, v, "WIN").status == "em_validacao"


def test_quant_sai_de_nao_treinado_para_exploratorio_com_artefato_sintetico(am, tmp_path):
    from robowin.core.config import ErroConfig
    from robowin.decisao.simulacao import ParamsSimulacao, executar_sessao, montar_sessao
    registro = tmp_path / "WIN" / "registro.json"
    reg = RegistroModelos(registro)
    registrar_e_salvar(reg, treinar(am, "logistica"), "v1", barreiras=BARREIRAS, custos=CUSTOS)

    def params(ativo, saida, rm):
        return ParamsSimulacao(seed=11, n_ticks=3000, saida=tmp_path / saida, registro_modelos=rm,
                               tipo_modelo="logistica", ativo=ativo)
    s = montar_sessao(params("WIN", "s", registro))
    assert s.modelo.status == "em_validacao" and s.modelo.origem_dados == "sintetico"
    resumo = asyncio.run(executar_sessao(s))
    assert "exploratorio" in resumo["quant"]["rotulos"]
    # registro de outro ativo / ausente => recusa explicita (sem cair em silencio num modelo treinado na hora)
    with pytest.raises(ErroConfig, match="recusado"):
        montar_sessao(params("WDO", "s2", registro))
    with pytest.raises(ErroConfig, match="recusado"):
        montar_sessao(params("WIN", "s3", tmp_path / "nada.json"))
