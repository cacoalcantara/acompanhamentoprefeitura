"""Vetor de features por ciclo de decisao (tabela ``features_ciclo``): gravacao pelo Orquestrador (lista permitida,
t_ns/sessao/ciclo/bracos), PSI do monitoramento usando a tabela como fonte padrao e banco antigo sem a tabela."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

import numpy as np
import pytest
from test_decisao_comum import BracoRoteirizado, alimentar, montar_orq, serie

from robowin.avaliacao import monitoramento_banco as mb
from robowin.core.esquema_banco import DDL, TABELAS
from robowin.dados import features_ciclo as fc
from robowin.dados.banco import BancoSQLite
from robowin.dados.ticks import dia_ns
from robowin.decisao.registro import Registro
from robowin.features.registro import nomes_permitidos

NS = 1_000_000_000
DIAS = [dt.date(2030, 1, 7), dt.date(2030, 1, 8), dt.date(2030, 1, 9), dt.date(2030, 1, 10)]


def _precos(n: int = 40) -> list[float]:
    rng = np.random.default_rng(5)
    return [130000.0 + 5.0 * float(x) for x in np.cumsum(rng.integers(-2, 3, n))]


class SemMigracao:
    """Banco minimo (so o protocolo congelado ``Banco``): nao sabe registrar migracao."""

    def __init__(self) -> None:
        self.b = BancoSQLite(":memory:")

    def inserir(self, tabela: str, linha: dict[str, Any]) -> None:
        self.b.inserir(tabela, linha)

    def consultar(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.b.consultar(sql, params)


# ====================================================================================== gravacao ==
@pytest.mark.asyncio
async def test_orquestrador_grava_um_vetor_por_ciclo_com_t_sessao_ciclo_e_bracos():
    arm = BracoRoteirizado("a", [lambda s, c: s.nada(c)])
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie(_precos()))
    linhas = banco.consultar(f"SELECT * FROM {fc.TABELA} ORDER BY t_ns, id")
    assert arm.contextos, "o teste precisa de ciclos de decisao"
    assert len(linhas) == len({c.ciclo_id for c in arm.contextos})  # uma linha por ciclo
    por_ciclo = {r["ciclo_id"]: r for r in linhas}
    permitidas = set(nomes_permitidos())
    for ctx in arm.contextos:
        r = por_ciclo[ctx.ciclo_id]
        assert r["t_ns"] == ctx.t0_ns and r["sessao"] == "t" and r["bracos"] == "a" and r["gatilhos"]
        vals = json.loads(r["valores_json"])
        assert vals and set(vals) <= permitidas and r["n"] == len(vals)
        assert all(isinstance(v, float) for v in vals.values())
        assert " " not in r["valores_json"]  # compacto
    assert banco.consultar("SELECT modulo FROM _migracoes WHERE modulo=?", (fc.MODULO,))  # migracao registrada
    assert fc.TABELA not in TABELAS  # fora do esquema congelado


@pytest.mark.asyncio
async def test_gravar_features_nao_muda_dossie_decisoes_nem_auditoria():
    precos = _precos()
    a1 = BracoRoteirizado("a", [lambda s, c: s.nada(c)])
    orq1, banco1, _ = montar_orq([a1])
    await alimentar(orq1, serie(precos))
    a2 = BracoRoteirizado("a", [lambda s, c: s.nada(c)])
    orq2, _, _ = montar_orq([a2])
    duble = SemMigracao()  # mesmo fluxo, mas o banco nao aceita a tabela nova => nada de features gravado
    orq2.registro = Registro(duble, None, "t")
    await alimentar(orq2, serie(precos))
    assert [c.hash_dossie for c in a1.contextos] == [c.hash_dossie for c in a2.contextos] != []
    sql = "SELECT id, t_ns, braco, resultado, hash_dossie FROM decisoes ORDER BY id"
    assert banco1.consultar(sql) == duble.consultar(sql)
    assert fc.TABELA not in duble.b.tabelas()
    assert banco1.consultar(f"SELECT COUNT(*) AS n FROM {fc.TABELA}")[0]["n"] > 0


def test_registro_sem_suporte_a_migracao_nao_grava_e_nao_quebra():
    reg = Registro(SemMigracao(), None, "s")
    assert reg.features_ciclo(t_ns=1, ciclo_id="c", bracos=["a"], gatilhos=["periodico"], valores={"ret_5s": 1.0}) \
        is False


def test_compactar_filtra_lista_permitida_e_nao_finitos():
    v = fc.compactar({"ret_5s": 1.234567891, "rv_1m": float("nan"), "inventada": 3.0, "spread_pts": 5}, ["ret_5s",
                     "rv_1m", "spread_pts"])
    assert v == {"ret_5s": 1.234568, "spread_pts": 5.0}


# ========================================================================================== PSI ==
def _banco_features(caminho, *, deslocar: float = 2.0, n_por_dia: int = 400, com_proxies: bool = True):
    """Banco com vetores por ciclo (feature ``ofi_30s_norm`` desloca nas ultimas sessoes; ``ret_5s`` estavel) e
    decisoes com uma saida de proposta (proxy) estavel."""
    rng = np.random.default_rng(1)
    banco = BancoSQLite(caminho)
    reg = Registro(banco, None, "s")
    for i, dia in enumerate(DIAS):
        mu = deslocar if i >= len(DIAS) // 2 else 0.0
        for j in range(n_por_dia):
            t = dia_ns(dia) + 10 * 3600 * NS + j * 15 * NS
            cic = reg.novo_id("cic")
            assert reg.features_ciclo(t_ns=t, ciclo_id=cic, bracos=["quant"], gatilhos=["periodico"],
                                      valores={"ofi_30s_norm": float(rng.normal(mu, 1.0)),
                                               "ret_5s": float(rng.normal(0.0, 1.0))})
            if com_proxies:
                reg.decisao(id=reg.novo_id("dec"), t_ns=t, braco="quant", modo="quant", gatilho="periodico",
                            acao="NAO_OPERAR", proposta={"e_brl_compra": float(rng.normal(0.0, 1.0))},
                            resultado="sem_acao")
    banco.fechar()
    return caminho


def test_psi_usa_o_vetor_gravado_por_ciclo_como_fonte_padrao(tmp_path):
    b = _banco_features(tmp_path / "f.db")
    m = mb.montar_monitoramento(b, tick_pontos=1.0, liquidos_por_braco={}, pnl_diario_por_braco={})
    psi = m["psi"]
    assert psi["fonte"] == "features" and psi["status"] == "ok"
    assert psi["niveis"]["ciclo.ofi_30s_norm"] == "alerta" and psi["niveis"]["ciclo.ret_5s"] == "estavel"
    assert not any(k.startswith("quant.") for k in psi["psi"])  # proxies ficam de fora quando ha features
    assert psi["features_em_alerta"] == ["ciclo.ofi_30s_norm"]
    assert "features_ciclo" in mb.renderizar_markdown(m)
    # alternativa explicita: proxies
    px = mb.montar_monitoramento(b, tick_pontos=1.0, liquidos_por_braco={}, pnl_diario_por_braco={},
                                 fonte_psi="proxies")["psi"]
    assert px["fonte"] == "proxies" and "quant.e_brl_compra" in px["psi"]
    assert not any(k.startswith("ciclo.") for k in px["psi"])
    # --ticks continua somando (tick.*) a qualquer fonte
    extra = {"ret_5s": [(t, v) for t, v in mb.series_de_features_ciclo(b)["ciclo.ret_5s"]]}
    tk = mb.montar_monitoramento(b, tick_pontos=1.0, liquidos_por_braco={}, pnl_diario_por_braco={},
                                 series_extra=extra)["psi"]
    assert tk["fonte"] == "features+ticks" and "tick.ret_5s" in tk["psi"]
    with pytest.raises(ValueError):
        mb.series_psi(b, [], "outra")


def test_vetores_repetidos_no_mesmo_instante_contam_uma_vez(tmp_path):
    banco = BancoSQLite(tmp_path / "g.db")
    reg = Registro(banco, None, "s")
    for grupo in ("g1", "g2"):  # dois grupos de bracos no mesmo t compartilham o snapshot
        reg.features_ciclo(t_ns=10, ciclo_id=f"cic-{grupo}", bracos=[grupo], gatilhos=["periodico"],
                           valores={"ret_5s": 1.0})
    banco.fechar()
    assert fc.ler_vetores(tmp_path / "g.db") == [(10, {"ret_5s": 1.0})]


def test_banco_antigo_sem_a_tabela_cai_nos_proxies(tmp_path):
    p = tmp_path / "antigo.db"
    con = sqlite3.connect(p)
    for d in DDL:  # so o esquema congelado (banco gravado antes da tabela nova)
        con.execute(d)
    rng = np.random.default_rng(2)
    k = 0
    for dia in DIAS:
        for j in range(80):
            t = dia_ns(dia) + 10 * 3600 * NS + j * 15 * NS
            con.execute("INSERT INTO decisoes(id, t_ns, braco, modo, resultado, intencao_json, proposta_json) "
                        "VALUES (?,?,?,?,?,?,?)", (f"d-{k}", t, "quant", "quant", "sem_acao", "{}",
                                                   json.dumps({"e_brl_compra": float(rng.normal())})))
            k += 1
    con.commit()
    con.close()
    assert fc.ler_vetores(p) == [] and mb.series_de_features_ciclo(p) == {}
    m = mb.montar_monitoramento(p, tick_pontos=1.0)
    assert m["psi"]["fonte"] == "proxies" and m["psi"]["status"] == "ok" and "quant.e_brl_compra" in m["psi"]["psi"]
    assert "proxies" in mb.renderizar_markdown(m)
    so_feat = mb.montar_monitoramento(p, tick_pontos=1.0, fonte_psi="features")["psi"]
    assert so_feat["fonte"] == "features" and so_feat["status"] == "amostra insuficiente"
    assert fc.ler_vetores(tmp_path / "nao_existe.db") == []
    assert fc.TABELA not in {r[0] for r in sqlite3.connect(p).execute("SELECT name FROM sqlite_master")}  # so leu
