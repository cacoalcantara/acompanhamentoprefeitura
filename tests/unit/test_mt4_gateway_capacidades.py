"""Gateway com plataforma mt4 + DubleMT4, capacidades por plataforma (MM e livro bloqueados) e CLI --plataforma."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from perfis_teste import WIN
from test_mt4_comum import LOGIN, SERVIDOR4, SIMB, T0_NS, cmd, montar_mt4, ticks_planos

from robowin.core.barramento import BarramentoLocal
from robowin.core.tipos import Lado
from robowin.mm.config import ConfigMM, exigir_livro, exigir_simulador_apenas
from robowin.mt5 import constantes as C
from robowin.mt5.canais import CanalComandosLocal
from robowin.mt5.capacidades import (
    CAPACIDADES_MT4,
    CAPACIDADES_MT5,
    CapacidadeAusente,
    capacidades_da_plataforma,
    capacidades_do_terminal,
    features_desligadas,
    normalizar_plataforma,
)
from robowin.mt5.cli_cmds import executar_diagnostico, texto_relatorio
from robowin.mt5.duble import DubleMT5
from robowin.mt5.execucao_demo import GuardaDemoViolada
from robowin.mt5.gateway import ConfigGateway, Gateway


def montar_gw4(tmp_path, ticks=None, duble_kw=None, **cfg_kw):
    d, con, rel = montar_mt4(ticks if ticks is not None else ticks_planos(3000), **(duble_kw or {}))
    bus = BarramentoLocal()
    eventos: list[dict] = []
    bus.assinar("*", eventos.append)
    canal = CanalComandosLocal()
    cfg_kw.setdefault("plataforma", "mt4")
    cfg = ConfigGateway(perfil=WIN, login=LOGIN, servidor=SERVIDOR4, hoje=dt.date(2026, 10, 27), dir_log=tmp_path / "log",
                        inicio_coleta_ns=T0_NS - 1_000_000_000, espera_emergencia_s=0.0, **cfg_kw)
    gw = Gateway(con, cfg, rel, bus, canal, dormir=lambda s: rel.avancar(int(s * 1e9)))
    gw.iniciar()
    return SimpleNamespace(d=d, con=con, rel=rel, bus=bus, ev=eventos, canal=canal, gw=gw, cli=canal.cliente("nucleo"))


def ordem(g, lado=Lado.COMPRA, **kw):
    if kw.get("tipo", "mercado") != "saida":
        kw.setdefault("sl", 99900.0 if lado == Lado.COMPRA else 100100.0)
    return g.cli.enviar({"tipo": "ordem", "cmd": cmd(lado, **kw).model_dump(mode="json")}, bombear=g.gw.passo)


def passos(g, n, dt_s=0.2):
    for _ in range(n):
        g.rel.avancar(int(dt_s * 1e9))
        g.gw.passo()


# --- capacidades ----------------------------------------------------------------------------------------
def test_capacidades_declaradas_por_plataforma():
    assert CAPACIDADES_MT5.livro and CAPACIDADES_MT5.historico_ticks and CAPACIDADES_MT5.conta == "netting"
    assert CAPACIDADES_MT5.tempo_ms == 1 and CAPACIDADES_MT5.ticks_de_negocio
    assert not CAPACIDADES_MT4.livro and not CAPACIDADES_MT4.historico_ticks and CAPACIDADES_MT4.conta == "hedging"
    assert CAPACIDADES_MT4.tempo_ms == 1000 and not CAPACIDADES_MT4.ticks_de_negocio
    assert capacidades_da_plataforma("MT4") is CAPACIDADES_MT4 and capacidades_da_plataforma(None) is CAPACIDADES_MT5
    with pytest.raises(ValueError, match="invalida"):
        normalizar_plataforma("mt6")
    assert capacidades_do_terminal(montar_mt4()[1]) is CAPACIDADES_MT4
    assert capacidades_do_terminal(object()) is CAPACIDADES_MT5  # MT5/duble sem atributo => MT5


def test_mm_e_livro_bloqueados_no_mt4_com_mensagem_clara():
    with pytest.raises(CapacidadeAusente, match="market making.*livro.*MT4"):
        exigir_livro(CAPACIDADES_MT4)
    with pytest.raises(CapacidadeAusente):
        exigir_simulador_apenas(ConfigMM(), CAPACIDADES_MT4)
    exigir_livro(CAPACIDADES_MT5)  # MT5 tem livro: sem bloqueio
    exigir_simulador_apenas(ConfigMM(), CAPACIDADES_MT5)
    exigir_simulador_apenas(ConfigMM())  # chamada antiga (sem terminal) continua valendo
    m = CAPACIDADES_MT4.bloqueios(usa_livro=True, usa_mm=True, usa_backfill=True, usa_fluxo_agressor=True, precisa_ms=100)
    assert len(m) == 5 and any("market making" in x for x in m) and any("1000 ms" in x for x in m)
    assert CAPACIDADES_MT5.bloqueios(usa_livro=True, usa_mm=True, usa_backfill=True, usa_fluxo_agressor=True, precisa_ms=1) == []
    with pytest.raises(CapacidadeAusente, match="historico_ticks"):
        CAPACIDADES_MT4.exigir("historico_ticks", "backfill")


def test_features_de_livro_desligadas_so_no_mt4():
    off = features_desligadas(CAPACIDADES_MT4)
    assert {"livro_imb_1", "ofi_5s", "microprice_desvio_pts"} <= set(off)
    assert "spread_pts" not in off and "fluxo_imb_5s" not in off and "ret_5s" not in off  # so ticks: continuam
    assert features_desligadas(CAPACIDADES_MT5) == ()


# --- gateway ----------------------------------------------------------------------------------------------
def test_gateway_mt4_publica_ticks_sem_livro_e_declara_capacidades(tmp_path):
    g = montar_gw4(tmp_path)
    passos(g, 15)
    topicos = {e["topico"] for e in g.ev}
    assert {"tick", "estado", "heartbeat"} <= topicos and "livro" not in topicos
    seqs = [e["seq"] for e in g.ev]
    assert seqs == list(range(1, len(seqs) + 1))
    est = [e for e in g.ev if e["topico"] == "estado"][-1]
    assert est["plataforma"] == "mt4" and est["duble"] is True and est["capacidades"]["livro"] is False
    assert est["capacidades"]["conta"] == "hedging" and est["conta"]["margin_mode"] == "RETAIL_HEDGING"
    assert est["offset_confiavel"] and est["livro_total"] == 0
    assert est["guardiao"]["modo_conta"] == "hedging"
    ticks = [e for e in g.ev if e["topico"] == "tick"]
    assert len(ticks) >= 20 and all(t["t_servidor_ms"] % 1000 == 0 for t in ticks)  # tempo em segundos
    assert len({t["t_servidor_ms"] for t in ticks}) < len(ticks)  # varios ticks no mesmo segundo, nenhum perdido
    alerta = [e for e in g.ev if e["topico"] == "alerta" and e["tipo"] == "capacidade_ausente"]
    assert alerta and any("market making" in m for m in alerta[0]["dados"]["motivos"])
    assert "livro_imb_1" in alerta[0]["dados"]["features_desligadas"]


def test_gateway_recusa_plataforma_diferente_do_terminal(tmp_path):
    d, con, rel = montar_mt4()
    cfg = ConfigGateway(perfil=WIN, login=LOGIN, servidor=SERVIDOR4, hoje=dt.date(2026, 10, 27), plataforma="mt5")
    with pytest.raises(GuardaDemoViolada, match="difere do terminal"):
        Gateway(con, cfg, rel, BarramentoLocal()).iniciar()
    d5 = DubleMT5(rel)
    cfg4 = ConfigGateway(perfil=WIN, login=LOGIN, servidor=SERVIDOR4, hoje=dt.date(2026, 10, 27), plataforma="mt4")
    with pytest.raises(GuardaDemoViolada, match="difere do terminal"):
        Gateway(d5, cfg4, rel, BarramentoLocal()).iniciar()


def test_gateway_mt4_ordem_ticket_e_emergencia_zeram_por_ticket(tmp_path):
    g = montar_gw4(tmp_path)
    passos(g, 5)
    r = ordem(g)
    assert r["estado"] == "EXECUTADA"
    tickets = g.d.tickets_abertos()
    assert tickets == [r["ack"]["order"]]
    # segunda entrada com posicao aberta e vetada pelo guardiao (hedging abriria OUTRA posicao)
    r2 = ordem(g, Lado.VENDA)
    assert r2["ack"]["retcode"] == C.RET_LOCAL_RECUSADO and "hedging: ja ha posicao" in r2["ack"]["comentario"]
    assert g.d.tickets_abertos() == tickets
    est = g.cli.enviar({"tipo": "estado"}, bombear=g.gw.passo)
    assert est["tickets_abertos"] == tickets and est["posicao"]["ticket"] == tickets[0]
    em = g.cli.enviar({"tipo": "emergencia", "origem": "teste"}, bombear=g.gw.passo)
    assert em["confirmado"] is True and em["zerado"] and g.d.tickets_abertos() == []
    fechar = [e for e in g.d.envios if e["cmd"] == "ordem_fechar"]
    assert [f["args"]["ticket"] for f in fechar] == tickets  # SEMPRE por ticket
    assert len([e for e in g.d.envios if e["cmd"] == "ordem_abrir"]) == 1  # nenhuma ordem oposta


def test_gateway_mt4_conta_real_nao_inicia(tmp_path):
    with pytest.raises(Exception, match="DEMO|demo|real"):
        montar_gw4(tmp_path, duble_kw={"demo": False})


def test_lacuna_do_feed_sem_historico_so_bloqueia_entradas(tmp_path):
    g = montar_gw4(tmp_path)
    passos(g, 6)
    g.d.reiniciar_ea()  # perda REAL de ticks: nao ha copy_ticks_range para recuperar
    passos(g, 3)
    assert g.con.lacunas_ticks >= 1 and g.con.copy_ticks_range(SIMB, 0, 1, -1) is None
    bloq = g.gw.guardiao.estado()["bloqueios"]
    assert any(b.startswith("feed_lacuna") for b in bloq)
    est = g.cli.enviar({"tipo": "estado"}, bombear=g.gw.passo)
    assert est["lacuna_pendente"] is True  # o nucleo marca bloqueio proprio ao ver isto
    r = ordem(g)
    assert r["ack"]["retcode"] == C.RET_LOCAL_RECUSADO and "feed_lacuna" in r["ack"]["comentario"]
    assert g.d.posicoes == {} and not [e for e in g.d.envios if e["cmd"] == "ordem_abrir"]
    assert not g.gw.ticks.backfills  # nunca tenta recuperar
    g.gw.guardiao.rearmar("operador conferiu o feed")  # rearme manual libera
    assert ordem(g)["estado"] == "EXECUTADA"


def test_falha_de_transporte_tambem_conta_como_lacuna_sem_backfill(tmp_path):
    g = montar_gw4(tmp_path)
    passos(g, 5)
    g.con._tr.fora_do_ar = True
    passos(g, 3)
    g.con._tr.fora_do_ar = False
    passos(g, 3)
    assert g.gw.ticks.lacunas_sem_backfill >= 1 and g.gw.ticks.lacuna_pendente is False
    assert any(b.startswith("feed_lacuna") for b in g.gw.guardiao.estado()["bloqueios"])
    assert any(e["topico"] == "alerta" and e["tipo"] == "lacuna_sem_backfill" for e in g.ev)


def test_gateway_mt5_continua_igual_com_livro_e_netting(tmp_path):
    from test_mt5_gateway import montar_gw
    g = montar_gw(tmp_path)
    passos(g, 15)
    est = [e for e in g.ev if e["topico"] == "estado"][-1]
    assert est["plataforma"] == "mt5" and est["capacidades"]["livro"] is True and est["livro_total"] > 0
    assert est["guardiao"]["modo_conta"] == "netting"


# --- diagnostico e CLI ------------------------------------------------------------------------------------
def test_diagnostico_mt4_reporta_capacidades_sem_livro(tmp_path):
    d, con, rel = montar_mt4(ticks_planos(3000))
    rel_ = executar_diagnostico(con, WIN, login_esperado=LOGIN, servidor_esperado=SERVIDOR4, hoje=dt.date(2026, 10, 27),
                                relogio=rel, plataforma="mt4")
    assert "NAO compilado/verificado em MT4 real" in rel_["origem"] and "NAO compilado/verificado" in texto_relatorio(rel_)
    assert rel_["plataforma"] == "mt4" and "DUBLE MT4" in rel_["origem"] and rel_["conta"]["margin_mode"] == "RETAIL_HEDGING"
    assert rel_["livro"]["assinado"] is False
    assert any("capacidade ausente" in a and "market making" in a for a in rel_["avisos"])
    assert not any("margin_mode" in a for a in rel_["avisos"])  # hedging e o esperado no MT4
    assert rel_["order_check"]["ok"] is True and d.envios == []  # so order_check
    assert "DIAGNOSTICO MT4" in texto_relatorio(rel_)


def test_cli_diagnostico_e_coletar_com_plataforma_mt4_duble(tmp_path, capsys):
    from robowin.cli import main
    rc = main(["diagnostico", "--duble", "--plataforma", "mt4", "--saida", str(tmp_path / "diag")])
    out = capsys.readouterr().out
    assert "DIAGNOSTICO MT4" in out and "DUBLE MT4" in out
    assert rc == 0
    j = next((tmp_path / "diag").glob("diagnostico-*.json"))
    assert json.loads(j.read_text())["plataforma"] == "mt4"
    rc = main(["coletar", "--duble", "--plataforma", "mt4", "--duracao-s", "1", "--dir-log", str(tmp_path / "log")])
    out = capsys.readouterr().out
    assert rc == 0 and "ticks" in out and "0 livros" in out


def test_cli_plataforma_invalida_e_lida_do_toml(tmp_path, capsys):
    from robowin.cli import main
    with pytest.raises(SystemExit):
        main(["diagnostico", "--duble", "--plataforma", "mt6"])
    cfg = tmp_path / "r.toml"
    cfg.write_text('[geral]\nconta = "demo_mt5"\nsimbolo_base = "WIN"\n[mt5]\nplataforma = "mt9"\n', encoding="utf-8")
    assert main(["diagnostico", "--duble", "--config", str(cfg), "--saida", str(tmp_path / "d")]) == 2
    assert "invalida" in capsys.readouterr().out
    assert main(["emergencia", "--sem-arquivo", "--config", str(cfg)]) == 2


def test_cli_emergencia_aceita_plataforma_mt4(tmp_path, capsys):
    from robowin.cli import main
    flag = tmp_path / "EMERGENCIA"
    assert main(["emergencia", "--plataforma", "mt4", "--arquivo", str(flag), "--motivo", "teste"]) == 0
    assert flag.exists() and "fecha CADA posicao pelo seu ticket" in capsys.readouterr().out
