"""Tabelas da decisao completa pela IA (``dados/decisao_ia.py``; docs/16, WP0): migracao idempotente, fora do
esquema congelado, banco antigo sem as tabelas continua funcionando e duble sem migracao nao grava nada."""
from __future__ import annotations

from typing import Any

import pytest

from robowin.core.esquema_banco import TABELAS as TABELAS_CONGELADAS
from robowin.dados import decisao_ia as di
from robowin.dados.banco import BancoSQLite
from robowin.decisao.registro import Registro


class SemMigracao:
    """Banco minimo (so o protocolo congelado ``Banco``): nao sabe registrar migracao."""

    def __init__(self) -> None:
        self.b = BancoSQLite(":memory:")

    def inserir(self, tabela: str, linha: dict[str, Any]) -> None:
        self.b.inserir(tabela, linha)

    def consultar(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self.b.consultar(sql, params)


def test_tabelas_fora_do_esquema_congelado_e_colunas_do_contrato():
    assert not set(di.TABELAS) & set(TABELAS_CONGELADAS)
    assert set(di.TABELAS) == {"posicoes_episodio", "gestao_eventos", "previsoes_ia", "contrafactuais"}
    assert list(di.COLUNAS["gestao_eventos"]) == [
        "id", "t_ns", "braco", "ticket", "origem", "acao", "regra", "sl_antes", "sl_depois", "tp_antes", "tp_depois",
        "prazo_ns", "aplicado", "motivo"]
    assert {"p_alvo", "incerteza", "y_alvo", "fonte_y", "resolvido_ns"} <= set(di.COLUNAS["previsoes_ia"])
    assert {"liquido_total", "n_parciais", "mfe_pts", "mae_pts", "sombra_de", "trades_json"} <= set(
        di.COLUNAS["posicoes_episodio"])


def test_migracao_idempotente(tmp_path):
    caminho = tmp_path / "b.db"
    with BancoSQLite(caminho) as b:
        assert di.garantir_tabelas(b)
        assert set(di.TABELAS) <= set(b.tabelas())
        assert b.registrar_migracao(di.MODULO, di.DDL) == 0  # ja aplicada
        assert di.garantir_tabelas(b)  # de novo: nada muda
    with BancoSQLite(caminho) as b:  # reabrir: continua aplicada
        assert b.registrar_migracao(di.MODULO, di.DDL) == 0
        assert len(b.consultar("SELECT * FROM _migracoes WHERE modulo=?", (di.MODULO,))) == len(di.DDL)


def test_banco_antigo_sem_as_tabelas_continua_funcionando(tmp_path):
    caminho = tmp_path / "antigo.db"
    with BancoSQLite(caminho) as b:
        b.inserir("incidentes", {"t_ns": 1, "origem": "t", "severidade": "info", "detalhe": "x"})
    for t in di.TABELAS:
        assert di.ler(caminho, t) == []  # sem a tabela: vazio, nunca quebra
    assert di.ler(tmp_path / "nao_existe.db", di.EPISODIOS) == []
    with BancoSQLite(caminho) as b:  # migracao em banco antigo preserva os dados
        assert di.garantir_tabelas(b)
        assert len(b.consultar("SELECT * FROM incidentes")) == 1


def test_duble_sem_migracao_nao_grava():
    d = SemMigracao()
    assert not di.garantir_tabelas(d)
    reg = Registro(d, None, "s")
    assert reg.episodio(braco="planejador", ticket=1) is None
    assert reg.gestao_evento(braco="planejador", origem="auto") is None
    assert reg.previsao_ia(braco="planejador", p_alvo=0.5) is None
    assert reg.contrafactual(decisao_id="d", politica="sem_gestao") is None
    assert not set(di.TABELAS) & set(d.b.tabelas()) and not reg.contagem


def test_registro_grava_e_completa_episodio_e_previsao(tmp_path):
    caminho = tmp_path / "r.db"
    banco = BancoSQLite(caminho)
    reg = Registro(banco, None, "s")
    eid = reg.episodio(braco="planejador", ticket=7, decisao_id="d1", intencao_id="i1", lado="compra", q_inicial=1,
                       ancora_t0=100.0, stop_inicial_pts=60.0, alvo_inicial_pts=90.0, tempo_max_s=240,
                       t_aberta_ns=10, p_alvo=0.55)
    assert eid is not None and eid.startswith("s-epi-")
    assert reg.episodio(id=eid, t_fechada_ns=20, liquido_total=-12.5, n_parciais=0, motivo_final="stop_inicial",
                        trades_json=["t1"]) == eid
    gid = reg.gestao_evento(t_ns=15, braco="planejador", ticket=7, origem="auto", acao="apertar", regra="breakeven",
                            sl_antes=40.0, sl_depois=100.0, aplicado=True, motivo="")
    pid = reg.previsao_ia(id="d1-prv", decisao_id="d1", t0_ns=5, braco="planejador", lado="compra", p_alvo=0.55,
                          incerteza=0.4, executada=True)
    reg.previsao_ia(id=pid, y_alvo=0, fonte_y="executada", resolvido_ns=20)
    reg.contrafactual(decisao_id="d1", braco_ref="planejador", politica="sem_gestao", liquido=3.0, seed=1)
    banco.fechar()
    (ep,) = di.ler(caminho, di.EPISODIOS)
    assert ep["id"] == eid and ep["ticket"] == 7 and ep["p_alvo"] == 0.55 and ep["t_aberta_ns"] == 10  # mesclado
    assert ep["t_fechada_ns"] == 20 and ep["liquido_total"] == -12.5 and ep["motivo_final"] == "stop_inicial"
    assert ep["trades_json"] == '["t1"]'
    (g,) = di.ler(caminho, di.GESTAO, onde="origem = ?", parametros=("auto",))
    assert g["id"] == gid and g["aplicado"] == 1 and g["sl_depois"] == 100.0 and g["tp_antes"] is None
    (p,) = di.ler(caminho, di.PREVISOES)
    assert p["id"] == "d1-prv" and p["p_alvo"] == 0.55 and p["executada"] == 1 and p["y_alvo"] == 0
    assert len(di.ler(caminho, di.CONTRAFACTUAIS)) == 1


def test_linha_recusa_coluna_desconhecida_e_tabela_desconhecida():
    with pytest.raises(ValueError, match="desconhecidas"):
        di.linha(di.EPISODIOS, {"id": "x", "perda_max_dia_brl": 1})
    with pytest.raises(ValueError, match="tabela desconhecida"):
        di.linha("trades", {"id": "x"})
    with pytest.raises(ValueError, match="id obrigatorio"):
        di.linha(di.GESTAO, {"braco": "b"})
    with pytest.raises(ValueError, match="tabela desconhecida"):
        di.ler(":memory:", "trades")
