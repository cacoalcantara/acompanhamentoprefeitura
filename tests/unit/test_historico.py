"""Simulacao historica walk-forward causal (`robowin historico`): compactacao causal, treino so com pregoes < D,
replay causal (perturbar o futuro nao muda o passado), contaminacao/provedor e fluxo ponta a ponta (sem rede,
sem chave de API: provedor FALSO)."""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest
from b3_fixture import escrever_zip
from perfis_teste import WIN

from robowin.cli import main
from robowin.dados.b3_negocios import ConfigB3, importar_arquivo
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.dados.ticks import ArmazenamentoTicks
from robowin.historico.compactacao import compactar_janelas, disponibilidade_ns, ticks_no_instante
from robowin.historico.runner import (
    ParamsHistorico,
    carregar_dia,
    contaminacao,
    decidir_provedor,
    simular_dia,
)
from robowin.historico.treino import amostras_do_dia, juntar_amostras, treinar_para_dia
from robowin.modelos.walkforward import VazamentoDetectado

DIAS = [dt.date(2026, 10, 21), dt.date(2026, 10, 22), dt.date(2026, 10, 23)]
CFG = ConfigB3(atraso_entrega_ms=50.0)
KW = {"n": 2500, "inicio_s": 9 * 3600, "fim_s": 10 * 3600 + 30 * 60}


def _armazem(tmp_path, nome="arm", perturbar_ultimo_apos_s=None):
    arm = ArmazenamentoTicks(tmp_path / nome)
    for i, d in enumerate(DIAS):
        pert = perturbar_ultimo_apos_s if i == len(DIAS) - 1 else None
        z = escrever_zip(tmp_path / f"zips_{nome}", "WINZ26", d, seed=10 + i, cancelar=3, perturbar_apos_s=pert, **KW)
        importar_arquivo(z, arm, WIN, CFG)
    return arm


@pytest.fixture(scope="module")
def arm_mod(tmp_path_factory):
    return _armazem(tmp_path_factory.mktemp("hist"))


def test_compactacao_conserva_volume_extremos_e_e_causal(arm_mod):
    tab = arm_mod.ler_tabela("WINZ26", DIAS[0])
    c, est = compactar_janelas(tab, 1000)
    assert est.saida < est.entrada and c["volume"].sum() == pytest.approx(tab["volume"].sum())
    assert (np.diff(disponibilidade_ns(c)) >= 0).all()
    w = disponibilidade_ns(tab) // 1_000_000_000
    wc_emit = disponibilidade_ns(c)
    for col, fn in (("last", "max"), ("last", "min")):  # extremos de cada janela chegam intactos
        orig = tab.groupby(w)[col].agg(fn)
        assert set(orig.to_numpy()) <= set(c[col].to_numpy())
    # desequilibrio por lado exato
    for flag in (32, 64):
        assert c[(c["flags"] & flag) != 0]["volume"].sum() == pytest.approx(tab[(tab["flags"] & flag) != 0]["volume"].sum())
    # causal: perturbar a partir de T nao muda nenhum evento anterior a T
    T = int(np.quantile(disponibilidade_ns(tab), 0.6))
    pert = tab.copy()
    fut = disponibilidade_ns(pert) >= T
    pert.loc[fut, "last"] += 500.0
    pert.loc[fut, "volume"] *= 7
    c2, _ = compactar_janelas(pert, 1000)
    a = c[wc_emit < T].reset_index(drop=True)
    b = c2[disponibilidade_ns(c2) < T].reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    tk = ticks_no_instante(c)
    assert all(x.t_utc_ns == x.t_recebido_ns for x in tk) and len(tk) == len(c)
    assert compactar_janelas(tab, 0)[1].fator == 1.0


def test_treino_do_dia_nunca_ve_o_dia_nem_o_futuro(arm_mod):
    am = {d: amostras_do_dia(carregar_dia(arm_mod, "WINZ26", d, 1000).ticks, WIN) for d in DIAS}
    assert all(a is not None and len(a) > 50 for a in am.values())
    m1, info1 = treinar_para_dia(DIAS[1], am, WIN, dias_treino=5)
    assert info1.dias_treino == [DIAS[0].isoformat()] and m1.status == "em_validacao"
    from robowin.dados.ticks import dia_ns
    assert m1.treinado_ate_ns < dia_ns(DIAS[1])
    # mudar o dia D e o futuro nao muda o modelo de D
    outro = dict(am)
    outro[DIAS[1]] = juntar_amostras([am[DIAS[2]], am[DIAS[2]]])
    outro[DIAS[2]] = None
    m1b, _ = treinar_para_dia(DIAS[1], outro, WIN, dias_treino=5)
    assert m1b.hash_dados == m1.hash_dados
    m0, info0 = treinar_para_dia(DIAS[0], am, WIN, dias_treino=5)
    assert m0.status == "nao_treinado" and info0.dias_treino == [] and "sem pregao anterior" in info0.motivo
    # amostra do proprio dia D escondida sob uma data anterior: detectada
    with pytest.raises(VazamentoDetectado):
        treinar_para_dia(DIAS[1], {DIAS[0]: am[DIAS[1]]}, WIN, dias_treino=5)


def test_contaminacao_e_provedor(monkeypatch):
    assert contaminacao(dt.date(2026, 6, 30), "2026-06") == "contaminado"
    assert contaminacao(dt.date(2026, 7, 1), "2026-06") == "limpo"
    assert contaminacao(dt.date(2026, 6, 15), "2026-06-15") == "contaminado"
    refs = ["anthropic:claude-opus-5-5", "openai:gpt-6.1-sol"]
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    assert decidir_provedor("auto", refs)[0] == "falso"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    p, motivo = decidir_provedor("auto", refs)
    assert p == "falso" and "openai" in motivo  # so real com TODAS as chaves
    from robowin.historico.runner import ErroHistorico
    with pytest.raises(ErroHistorico):
        decidir_provedor("real", refs)
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    assert decidir_provedor("auto", refs)[0] == "real"
    assert decidir_provedor("falso", refs)[0] == "falso"


def _rodar_dia(tmp_path, arm, nome, modelo):
    from robowin.core.config import carregar_toml
    from robowin.decisao.simulacao import RAIZ_CONFIG
    from robowin.ia.catalogo import Catalogo
    p = ParamsHistorico(armazem=arm.raiz, saida=tmp_path / nome, provedor="falso")
    dados = carregar_dia(arm, "WINZ26", DIAS[-1], 1000)
    return simular_dia(p, dados, perfil=WIN, cfg=carregar_toml(RAIZ_CONFIG / "robowin.toml"),
                       catalogo=Catalogo.carregar(RAIZ_CONFIG / "modelos.toml"), modelo=modelo, piscina=None,
                       provedor="falso", corte="2026-06")


def test_replay_causal_perturbar_o_futuro_nao_muda_decisoes_anteriores(tmp_path, arm_mod):
    T_s = 10 * 3600  # 10:00 (Brasilia): o "futuro" do pregao a partir dai e trocado
    pert = _armazem(tmp_path, "pert", perturbar_ultimo_apos_s=T_s)
    am = {DIAS[0]: amostras_do_dia(carregar_dia(arm_mod, "WINZ26", DIAS[0], 1000).ticks, WIN)}
    modelo, _ = treinar_para_dia(DIAS[-1], am, WIN, dias_treino=5)
    a = _rodar_dia(tmp_path, arm_mod, "a", modelo)
    b = _rodar_dia(tmp_path, pert, "b", modelo)
    T_ns = int(dt.datetime(2026, 10, 23, 13, 0, tzinfo=dt.UTC).timestamp()) * 10**9
    margem = 120 * 10**9  # respostas de IA pedidas antes de T podem ser entregues depois (latencia injetada)
    chave = lambda r: (r["t_ns"], r["braco"], r["acao"], r["resultado"], r["hash_dossie"])  # noqa: E731
    antes_a = [chave(r) for r in a.decisoes if r["t_ns"] < T_ns - margem]
    antes_b = [chave(r) for r in b.decisoes if r["t_ns"] < T_ns - margem]
    assert antes_a and antes_a == antes_b
    assert [chave(r) for r in a.decisoes] != [chave(r) for r in b.decisoes]  # o futuro mudou de fato


def test_historico_ponta_a_ponta(tmp_path, arm_mod, capsys):
    saida = tmp_path / "saida"
    cod = main(["historico", "--ativo", "WIN", "--armazem", str(arm_mod.raiz), "--saida", str(saida),
                "--provedor", "falso", "--n-boot", "50", "--bracos", "quant,claude_gpt"])
    out = capsys.readouterr().out
    assert cod == 0, out
    md = (saida / "consolidado.md").read_text(encoding="utf-8")
    assert "lado agressor INFERIDO" in md and "NAO prova lucratividade" in md and "FALSO" in md
    assert "AMOSTRA INSUFICIENTE" in md and "limpo" in md and "## Latencias" in md and "IC de Wilson" in md
    c = json.loads((saida / "consolidado.json").read_text(encoding="utf-8"))
    assert [p["data"] for p in c["pregoes"]] == [d.isoformat() for d in DIAS]
    assert c["provedor"] == "falso" and c["limpos"] == 3 and c["corte_llm"]
    assert c["pregoes"][0]["modelo"] == "nao_treinado" and c["pregoes"][1]["modelo"] == "em_validacao"
    assert set(c["totais_por_braco"]) == {"quant", "claude_gpt", "nao_operar", "controle_aleatorio",
                                          "controle_aleatorio_cond"}  # pedidos + controles (sempre)
    assert c["totais_por_braco"]["claude_gpt"]["chamadas_ia"] > 0
    assert sum(t["decisoes"] for t in c["totais_por_braco"].values()) > 0
    assert "controle_aleatorio" in c["relatorio"]["bracos"]
    for d in DIAS:
        j = json.loads((saida / "dias" / d.isoformat() / "relatorio.json").read_text(encoding="utf-8"))
        assert all(x < d.isoformat() for x in j["treino"]["dias_treino"])
        assert j["maquina_do_tempo"]["disponivel"] is False  # < 20 sessoes anteriores: indisponivel, honesto
        assert (saida / "dias" / d.isoformat() / "robowin.db").exists()
    # nao sobrescreve resultados
    assert main(["historico", "--ativo", "WIN", "--armazem", str(arm_mod.raiz), "--saida", str(saida),
                 "--provedor", "falso", "--dias", "1"]) == 2


def test_historico_recusa_armazem_sintetico_ou_sem_marcador(tmp_path):
    sint = ArmazenamentoTicks(tmp_path / "sint")
    gerar(ConfigSintetico.do_perfil(WIN, n_ticks=200)).gravar(sint)
    assert main(["historico", "--armazem", str(sint.raiz), "--saida", str(tmp_path / "s1"), "--provedor", "falso"]) == 2
    vazio = ArmazenamentoTicks(tmp_path / "vazio")
    assert main(["historico", "--armazem", str(vazio.raiz), "--saida", str(tmp_path / "s2"), "--provedor", "falso"]) == 2
