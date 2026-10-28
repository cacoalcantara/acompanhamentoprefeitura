"""Espelhamento na demo (unidade): comandos novos do gateway, ponte, monitor, trava, painel e INCERTA. Sem processos."""
from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace

import pytest
from perfis_teste import ACAO
from test_decisao_comum import INS, BracoRoteirizado
from test_mt5_comum import SIMB, T0_NS, cmd, montar_exec
from test_mt5_gateway import montar_gw, ordem, passos

from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import AckOrdem, ComandoOrdem, ConfigRisco, EstadoOrdem, Lado, Posicao
from robowin.dados.banco import BancoSQLite
from robowin.decisao.espelho import (
    ConfigEspelho,
    EspelhoDemo,
    EspelhoRecusado,
    MonitorGateway,
    validar_config_espelhamento,
)
from robowin.decisao.gatilhos import ConfigGatilhos
from robowin.decisao.orquestrador import ConfigOrquestrador, Orquestrador
from robowin.decisao.registro import Registro
from robowin.execucao.ponte_gateway import (
    EmissorGateway,
    EspelhamentoNaoExclusivo,
    TerminalViaGateway,
    TravaEspelho,
    ack_de_resposta,
)
from robowin.mm.exigencias import CapacidadeAusente, exigir_capacidade_livro, tem_capacidade_livro
from robowin.mt5 import constantes as C
from robowin.mt5.consultas import posicoes_publicas, snapshot_terminal
from robowin.mt5.tempo_servidor import RelogioServidor
from robowin.painel import dados as D

NS = 1_000_000_000


# ============================================================================================ gateway ====
def test_snapshot_do_gateway_traz_posicoes_ordens_deals_em_utc_e_contratos(tmp_path):
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    assert ordem(g)["estado"] == "EXECUTADA"
    r = g.cli.enviar({"tipo": "snapshot", "desde_ns": 0}, bombear=g.gw.passo)
    assert r["tipo"] == "snapshot" and r["ok"] and len(r["positions"]) == 1 and len(r["deals"]) == 1
    p, d = r["positions"][0], r["deals"][0]
    assert p["volume"] == 1.0 and p["sl"] > 0 and abs(p["time_msc"] - T0_NS // 1_000_000) < 5000  # UTC, sem offset do servidor
    assert d["reason"] == C.RAZAO_EXPERT and d["position_id"] == p["ticket"] and d["entry"] == C.ENTRADA_IN
    est = g.cli.enviar({"tipo": "estado"}, bombear=g.gw.passo)
    assert est["posicoes"][0]["contratos"] == 1.0 and est["ordens"] == 0 and est["lote"] == 1
    assert est["capacidades"]["livro"] is True and est["modo_mm"] is False


def test_volumes_de_acoes_sao_divididos_por_lote_ao_publicar_posicoes():
    p = SimpleNamespace(ticket=7, symbol="PETR4", type=C.POS_COMPRA, volume=300.0, sl=30.0, tp=0.0, magic=9, price_open=31.0,
                        profit=1.0, time_msc=1_700_000_000_000 + 10_800_000, reason=3, identifier=7)
    pub = posicoes_publicas([p], ACAO.lote)
    assert ACAO.lote == 100 and pub[0]["volume"] == 300.0 and pub[0]["contratos"] == 3.0

    class Term:
        def positions_get(self):
            return (p,)

        def orders_get(self):
            return (SimpleNamespace(ticket=8, symbol="PETR4", type=C.ORDEM_BUY_LIMIT, magic=9, volume_current=100.0,
                                    price_open=30.0, sl=29.0, tp=0.0, time_setup_msc=1_700_000_000_000 + 10_800_000),)

        def history_deals_get(self, a, b):
            return (SimpleNamespace(ticket=1, order=2, position_id=7, time_msc=1_700_000_000_000 + 10_800_000, type=0, entry=0,
                                    magic=9, reason=3, volume=300.0, price=31.0, profit=0.0, commission=0.0, swap=0.0,
                                    fee=0.0, symbol="PETR4"),)
    snap = snapshot_terminal(Term(), RelogioServidor(10_800_000, 0, True), ACAO.lote, 0, 2 * 10**18)
    assert snap["positions"][0]["volume"] == 3.0 and snap["orders"][0]["volume_current"] == 1.0
    assert snap["deals"][0]["volume"] == 3.0 and snap["positions"][0]["time_msc"] == 1_700_000_000_000  # UTC


def test_snapshot_sem_resposta_do_terminal_e_ok_falso():
    class Mudo:
        def positions_get(self):
            return None

        def orders_get(self):
            return ()

        def history_deals_get(self, a, b):
            return ()
    assert snapshot_terminal(Mudo(), RelogioServidor(), 1, 0, 10**18)["ok"] is False


def test_gateway_modificar_e_cancelar_ordem_pendente_contam_no_orcamento(tmp_path):
    g = montar_gw(tmp_path, modo_mm=True)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    tk = g.d.symbol_info_tick(SIMB)
    preco = float(tk.bid) - 10.0
    r = ordem(g, Lado.COMPRA, tipo="limitada", preco=preco, sl=preco - 30.0)
    assert r["estado"] == "ACEITA"
    ticket = r["ack"]["order"]
    usadas = g.gw.executor.orcamento.usadas()
    m = g.cli.enviar({"tipo": "modificar_ordem", "ticket": ticket, "preco": preco - 5.0, "sl": preco - 35.0}, bombear=g.gw.passo)
    assert m["ack"]["retcode"] == C.RET_EXECUTADA
    o = g.d.orders_get(symbol=SIMB)[0]
    assert o.price_open == preco - 5.0 and o.sl == preco - 35.0
    assert g.gw.executor.orcamento.usadas() == usadas + 1
    c = g.cli.enviar({"tipo": "cancelar_ordem", "ticket": ticket}, bombear=g.gw.passo)
    assert c["ack"]["retcode"] == C.RET_EXECUTADA and g.d.orders_get(symbol=SIMB) == ()
    inexistente = g.cli.enviar({"tipo": "cancelar_ordem", "ticket": ticket}, bombear=g.gw.passo)
    assert inexistente["ack"]["retcode"] == C.RET_LOCAL_RECUSADO
    sem = g.cli.enviar({"tipo": "modificar_ordem", "ticket": 999, "preco": preco}, bombear=g.gw.passo)
    assert sem["ack"]["retcode"] == C.RET_LOCAL_RECUSADO


def test_modo_mm_uma_limitada_por_lado_e_a_que_reduz_posicao_sem_modo_mm_recusa(tmp_path):
    tk_bid = None
    for modo_mm in (False, True):
        g = montar_gw(tmp_path / str(modo_mm), modo_mm=modo_mm)
        g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
        tk_bid = float(g.d.symbol_info_tick(SIMB).bid)
        compra = ordem(g, Lado.COMPRA, tipo="limitada", preco=tk_bid - 10, sl=tk_bid - 40)
        venda = ordem(g, Lado.VENDA, tipo="limitada", preco=tk_bid + 20, sl=tk_bid + 50)
        assert compra["estado"] == "ACEITA"
        assert (venda["estado"] == "ACEITA") is modo_mm  # sem modo MM a 2a pendente excede o maximo de 1 contrato
        if modo_mm:
            outra_compra = ordem(g, Lado.COMPRA, tipo="limitada", preco=tk_bid - 15, sl=tk_bid - 45)
            assert outra_compra["ack"]["retcode"] == C.RET_LOCAL_RECUSADO  # uma limitada POR LADO
    # modo MM: com posicao longa, so a limitada de VENDA (que reduz) e aceita; a de compra (que aumentaria) nao
    g = montar_gw(tmp_path / "pos", modo_mm=True)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    assert ordem(g)["estado"] == "EXECUTADA"
    bid = float(g.d.symbol_info_tick(SIMB).bid)
    assert ordem(g, Lado.COMPRA, tipo="limitada", preco=bid - 10, sl=bid - 40)["ack"]["retcode"] == C.RET_LOCAL_RECUSADO
    assert ordem(g, Lado.VENDA, tipo="limitada", preco=bid + 20, sl=bid + 50)["estado"] == "ACEITA"


def test_executor_modificar_ordem_unidade():
    m = montar_exec()
    ack = m.ex.enviar(cmd(Lado.COMPRA, "limitada", preco=99950.0, sl=99900.0))
    assert ack.retcode == C.RET_COLOCADA
    a2 = m.ex.modificar_ordem(ack.order, 99945.0, 99890.0)
    assert a2.retcode == C.RET_EXECUTADA and m.d.orders_get(symbol=SIMB)[0].price_open == 99945.0


# ============================================================================================ ponte =====
def test_ack_de_resposta_so_aceita_ack_valido():
    assert ack_de_resposta(None) is None
    assert ack_de_resposta({"tipo": "erro", "erro": "x"}) is None
    assert ack_de_resposta({"tipo": "ack", "ack": "lixo"}) is None
    assert ack_de_resposta({"tipo": "ack", "ack": {"retcode": None}}).retcode is None
    assert ack_de_resposta({"tipo": "ack", "ack": {"retcode": 10009}}).retcode == 10009


class CanalFalso:
    """Canal de comandos que devolve respostas roteirizadas por tipo de mensagem (sem gateway)."""

    def __init__(self, **respostas):
        self.respostas = respostas
        self.enviados: list[dict] = []

    def enviar(self, msg, timeout_s=0.0):
        self.enviados.append(msg)
        r = self.respostas.get(msg["tipo"])
        return r(msg) if callable(r) else r

    def enviar_sem_resposta(self, msg):
        self.enviados.append(msg)


def estado_gw(trade_mode="demo", simbolo="WINZ26", posicoes=(), ordens=0, travado=False, **extra):
    return {"tipo": "estado", "conta": {"login": 1, "servidor": "X-DEMO", "trade_mode": trade_mode}, "simbolo": simbolo,
            "posicoes": list(posicoes), "ordens": ordens, "parado": None, "incertas": [], "duble": True, "modo_mm": True,
            "guardiao": {"travado_emergencia": travado, "bloqueios": [], "heartbeat_idade_s": 0.1},
            "capacidades": {"livro": True}, **extra}


SNAP_VAZIO = {"tipo": "snapshot", "ok": True, "positions": [], "orders": [], "deals": []}


def montar_orq_demo(braco="a", rel=None):
    rel = rel or RelogioSimulado(T0_NS)
    banco = BancoSQLite(":memory:")
    reg = Registro(banco, None, "u")
    bracos = [BracoRoteirizado(braco, [lambda s, c: s.nada(c)])]
    cfg = ConfigOrquestrador(instrumento=INS, risco=ConfigRisco(), sessao_id="u", exigir_aquecimento=False, latencia_exec_ns=0,
                             gatilhos=ConfigGatilhos(periodo_s=1.0, intervalo_min_s=1.0, e_brl_min=1e18, z_statarb_min=1e18,
                                                     evento_posicao=False, max_chamadas_dia=None))
    return Orquestrador(cfg, relogio=rel, registro=reg, bracos=bracos), banco, rel


def test_espelho_recusa_conta_nao_demo_gateway_mudo_simbolo_errado_e_conta_nao_zerada(tmp_path):
    for modo in ("real", "contest"):
        orq, _, _ = montar_orq_demo()
        e = EspelhoDemo(orq, ConfigEspelho(braco="a"), CanalFalso(estado=estado_gw(modo), snapshot=SNAP_VAZIO),
                        trava=TravaEspelho(tmp_path / "l.lock"))
        with pytest.raises(EspelhoRecusado, match="so espelha em conta DEMO"):
            e.iniciar()
        assert not e.ativo and not (tmp_path / "l.lock").exists()
    orq, _, _ = montar_orq_demo()
    with pytest.raises(EspelhoRecusado, match="nao respondeu"):
        EspelhoDemo(orq, ConfigEspelho(braco="a", timeout_s=0.1), CanalFalso(estado=None)).iniciar()
    orq, _, _ = montar_orq_demo()
    with pytest.raises(EspelhoRecusado, match="nao e do ativo"):
        EspelhoDemo(orq, ConfigEspelho(braco="a"), CanalFalso(estado=estado_gw(simbolo="WDOX26"), snapshot=SNAP_VAZIO)).iniciar()
    orq, _, _ = montar_orq_demo()
    with pytest.raises(EspelhoRecusado, match="TRAVADO"):
        EspelhoDemo(orq, ConfigEspelho(braco="a"), CanalFalso(estado=estado_gw(travado=True), snapshot=SNAP_VAZIO)).iniciar()
    orq, _, _ = montar_orq_demo()
    com_pos = {**SNAP_VAZIO, "positions": [{"ticket": 1, "symbol": "WINZ26", "type": 0, "magic": 7, "volume": 1.0,
                                            "price_open": 1.0, "sl": 0.0, "tp": 0.0, "profit": 0.0, "time_msc": 1}]}
    with pytest.raises(EspelhoRecusado, match="posicao ou ordem aberta"):
        EspelhoDemo(orq, ConfigEspelho(braco="a"), CanalFalso(estado=estado_gw(), snapshot=com_pos)).iniciar()
    orq, _, _ = montar_orq_demo()
    with pytest.raises(EspelhoRecusado, match="braco inexistente"):
        EspelhoDemo(orq, ConfigEspelho(braco="zzz"), CanalFalso())


@pytest.mark.parametrize("resposta", [
    None,  # timeout: sem resposta nenhuma
    {"tipo": "ack", "ack": {"retcode": None, "comentario": "INCERTA"}},
    {"tipo": "ack", "ack": {"retcode": C.RET_TIMEOUT}},
    {"tipo": "ack", "ack": {"retcode": C.RET_SEM_CONEXAO}},
    {"tipo": "erro", "erro": "falha ao tratar mensagem"},
])
def test_ack_none_10012_10031_viram_incerta_no_espelho_sem_reenvio(resposta):
    orq, _, rel = montar_orq_demo()
    canal = CanalFalso(estado=estado_gw(), snapshot=SNAP_VAZIO, ordem=resposta)
    e = EspelhoDemo(orq, ConfigEspelho(braco="a", espera_incerta_s=60.0), canal)
    e.iniciar()
    e.ao_estado_gateway(estado_gw())
    sim = Posicao(simbolo="WINZ26", lado=Lado.COMPRA, quantidade=1, preco_medio=130005.0)
    from robowin.core.tipos import Acao, Intencao
    it = Intencao(id="i1", braco="a", acao=Acao.COMPRAR, stop_pontos=60.0, validade_ns=rel.agora_ns() + 10**11)
    r = e.espelhar_entrada(rel.agora_ns(), sim, it, 60.0, None, 600)
    assert r.enviado and r.estado == EstadoOrdem.INCERTA.value
    assert [m for m in canal.enviados if m["tipo"] == "ordem"][0]["cmd"]["sl"] == 129945.0  # SL obrigatorio
    assert e.oms.incertas() and e.oms.bloqueado(e.braco_demo) == "ordem_incerta_pendente"
    # uma segunda tentativa (mesmo com tudo livre) NAO reenvia: OMS bloqueia entradas com INCERTA pendente
    it2 = it.model_copy(update={"id": "i2"})
    r2 = e.espelhar_entrada(rel.agora_ns() + NS, sim, it2, 60.0, None, 600)
    assert not r2.enviado and len([m for m in canal.enviados if m["tipo"] == "ordem"]) == 1


def test_espelho_so_envia_entrada_com_conta_zerada_sl_e_veto_de_risco():
    orq, banco, rel = montar_orq_demo()
    canal = CanalFalso(estado=estado_gw(), snapshot=SNAP_VAZIO,
                       ordem={"tipo": "ack", "ack": {"retcode": 10009, "order": 5, "preco": 130005.0}})
    e = EspelhoDemo(orq, ConfigEspelho(braco="a"), canal)
    e.iniciar()
    e.ao_estado_gateway(estado_gw())
    from robowin.core.tipos import Acao, Intencao
    it = Intencao(id="i1", braco="a", acao=Acao.COMPRAR, stop_pontos=60.0, validade_ns=rel.agora_ns() + 10**11)
    sim = Posicao(simbolo="WINZ26", lado=Lado.COMPRA, quantidade=1, preco_medio=130005.0)
    # veto de risco: stop acima do maximo => NADA e enviado (mesmo motor/config do braco)
    r = e.espelhar_entrada(rel.agora_ns(), sim, it, 500.0, None, 600)
    assert not r.enviado and "vetada" in r.motivo and "stop_acima_do_maximo" in r.motivo
    # sem stop: veto
    r = e.espelhar_entrada(rel.agora_ns(), sim, it, None, None, 600)
    assert not r.enviado and "stop_obrigatorio" in r.motivo
    assert not [m for m in canal.enviados if m["tipo"] == "ordem"]
    # conta demo NAO zerada => recusa
    canal.respostas["snapshot"] = {**SNAP_VAZIO, "orders": [{"ticket": 1, "symbol": "WINZ26", "type": 2, "magic": 7,
                                                              "volume_current": 1.0, "price_open": 1.0, "sl": 0.0, "tp": 0.0,
                                                              "time_setup_msc": 1}]}
    r = e.espelhar_entrada(rel.agora_ns(), sim, it.model_copy(update={"id": "i3"}), 60.0, None, 600)
    assert not r.enviado and r.motivo == "conta_demo_nao_zerada"
    canal.respostas["snapshot"] = SNAP_VAZIO
    ok = e.espelhar_entrada(rel.agora_ns() + NS, sim, it.model_copy(update={"id": "i4"}), 60.0, 80.0, 600)
    assert ok.enviado and ok.estado == "EXECUTADA"
    cmd_env = [m for m in canal.enviados if m["tipo"] == "ordem"][0]["cmd"]
    assert cmd_env["tipo"] == "mercado" and cmd_env["sl"] == 129945.0 and cmd_env["tp"] == 130085.0
    # ordens da DEMO ficam no banco como nao simuladas, com o braco rotulado demo:
    assert banco.consultar("SELECT braco, simulada FROM ordens")[0]["braco"] == "demo:a"
    assert e.crenca.quantidade == 1
    # a conta demo ja tem posicao => bloqueio de nova entrada
    assert e._bloqueio() == "espelho_posicao_aberta"


def test_espelho_desliga_para_novas_entradas_se_o_estado_do_gateway_envelhece(monkeypatch):
    orq, _, _ = montar_orq_demo()
    e = EspelhoDemo(orq, ConfigEspelho(braco="a", estado_max_idade_s=0.0), CanalFalso(estado=estado_gw(), snapshot=SNAP_VAZIO))
    e.iniciar()
    assert e._bloqueio() == "gateway_sem_estado"
    e.ativo = True
    e.desligar("teste")
    assert e._bloqueio().startswith("espelho_desligado:teste")


# ============================================================================================ monitor / painel ==
def _banco_registro():
    banco = BancoSQLite(":memory:")
    return banco, Registro(banco, None, "m")


def _est(posicoes, ordens, travado):
    return {"conta": {"login": 1, "servidor": "S", "trade_mode": "demo"}, "posicoes": posicoes, "ordens": ordens,
            "guardiao": {"travado_emergencia": travado, "bloqueios": ["emergencia: x"] if travado else [],
                         "heartbeat_idade_s": 0.3}, "incertas": [], "parado": None}


def test_monitor_grava_estado_e_so_confirma_emergencia_com_posicao_zerada():
    banco, reg = _banco_registro()
    mon = MonitorGateway(reg, confirmacao_a_cada_s=30.0)
    t = 10 * NS
    mon.ao_estado(_est([], 0, False), t)
    assert banco.consultar("SELECT 1 FROM incidentes WHERE origem='gateway_estado'")
    assert banco.consultar("SELECT 1 FROM eventos_risco WHERE tipo='gateway_estado'")
    # travado mas AINDA com posicao: nao confirma
    mon.ao_estado(_est([{"ticket": 1}], 0, True), t + NS)
    assert not banco.consultar("SELECT 1 FROM incidentes WHERE origem='gateway' AND detalhe LIKE 'emergencia_confirmada%'")
    # travado e zerado, mas com ordem pendente: nao confirma
    mon.ao_estado(_est([], 1, True), t + 2 * NS)
    assert not banco.consultar("SELECT 1 FROM incidentes WHERE origem='gateway'")
    # travado, sem posicao e sem ordens: confirma (uma vez)
    mon.ao_estado(_est([], 0, True), t + 3 * NS)
    mon.ao_estado(_est([], 0, True), t + 4 * NS)
    assert len(banco.consultar("SELECT 1 FROM incidentes WHERE origem='gateway' AND detalhe LIKE 'emergencia_confirmada%'")) == 1
    assert mon.confirmacoes == 1
    mon.ao_estado(_est([], 0, True), t + 40 * NS)  # reemite periodicamente (painel procura confirmacao POSTERIOR a flag)
    assert mon.confirmacoes == 2
    mon.silencio(t + 50 * NS, True)
    mon.silencio(t + 51 * NS, True)
    assert len(banco.consultar("SELECT 1 FROM incidentes WHERE detalhe LIKE 'sem heartbeat do gateway%'")) == 1


def test_confirmacao_do_painel_exige_o_gateway_quando_ele_reporta():
    banco, reg = _banco_registro()
    b = SimpleNamespace(consultar=banco.consultar)
    reg.evento_risco(t_ns=5 * NS, braco="a", tipo="kill_switch", detalhe="x", critico=True)
    # sem gateway reportando (simulacao pura): vale o criterio antigo
    assert D.confirmacao_emergencia(b, 0)["confirmado"] is True
    # com gateway reportando: o kill do nucleo e o alerta bloqueio_emergencia NAO bastam
    reg.incidente(t_ns=6 * NS, origem="gateway_estado", severidade="info", detalhe="gateway online")
    reg.incidente(t_ns=7 * NS, origem="gateway", severidade="alerta", detalhe="bloqueio_emergencia")
    r = D.confirmacao_emergencia(b, 0)
    assert r["confirmado"] is False and r["gateway"] is True
    reg.incidente(t_ns=8 * NS, origem="gateway", severidade="critico", detalhe="emergencia_confirmada: posicoes=0 ordens=0")
    assert D.confirmacao_emergencia(b, 0)["confirmado"] is True
    assert D.confirmacao_emergencia(b, 9 * NS)["confirmado"] is False  # a confirmacao e anterior a flag


# ============================================================================================ trava / config ===
def test_trava_de_espelho_reentrante_orfa_e_viva(tmp_path):
    t = TravaEspelho(tmp_path / "x.lock")
    t.adquirir("nucleo:a")
    t.adquirir("nucleo:a")  # mesmo dono no mesmo processo: ok
    with pytest.raises(EspelhamentoNaoExclusivo, match="ja espelha"):
        TravaEspelho(tmp_path / "x.lock").adquirir("mm")
    t.liberar()
    assert not (tmp_path / "x.lock").exists()
    # trava de processo vivo (o pai deste processo) e respeitada; de processo morto e retomada
    (tmp_path / "y.lock").write_text(f'{{"dono": "mm", "pid": {os.getppid()}, "t": 0}}')
    with pytest.raises(EspelhamentoNaoExclusivo):
        TravaEspelho(tmp_path / "y.lock").adquirir("nucleo:a")
    (tmp_path / "y.lock").write_text('{"dono": "mm", "pid": 2147483000, "t": 0}')
    t2 = TravaEspelho(tmp_path / "y.lock")
    t2.adquirir("nucleo:a")
    assert t2.atual()["dono"] == "nucleo:a"
    t2.liberar()
    (tmp_path / "z.lock").write_text("lixo")
    TravaEspelho(tmp_path / "z.lock").adquirir("mm")  # ilegivel: retomada


def test_validacao_de_config_do_espelhamento():
    ok = {"geral": {"conta": "demo_mt5", "modo": "simulacao"}}
    validar_config_espelhamento(ok, "a", canal_ok=True)
    with pytest.raises(EspelhoRecusado, match="demo_mt5"):
        validar_config_espelhamento({"geral": {"conta": "interno"}}, "a", canal_ok=True)
    with pytest.raises(EspelhoRecusado, match="simulacao"):
        validar_config_espelhamento({"geral": {"conta": "demo_mt5", "modo": "real"}}, "a", canal_ok=True)
    with pytest.raises(EspelhoRecusado, match="canal"):
        validar_config_espelhamento(ok, "a", canal_ok=False)
    with pytest.raises(EspelhoRecusado, match="EXCLUSIVO"):
        validar_config_espelhamento({**ok, "mm": {"demo_mm": True}}, "a", canal_ok=True)


def test_config_padrao_do_repositorio_deixa_o_espelhamento_e_o_mm_desligados():
    import tomllib
    from pathlib import Path
    cfg = tomllib.loads((Path(__file__).resolve().parents[2] / "config" / "robowin.toml").read_text())
    assert cfg["geral"]["espelhar"] == "" and cfg["mm"]["demo_mm"] is False


def test_exigencia_de_capacidade_livro_por_duck_typing():
    assert tem_capacidade_livro({"capacidades": {"livro": True}})
    assert not tem_capacidade_livro({"capacidades": {"livro": False}, "livro_profundidade": {"declarada": 10}})
    assert tem_capacidade_livro(SimpleNamespace(capacidades=SimpleNamespace(livro=True)))
    assert not tem_capacidade_livro(SimpleNamespace(livro=False))
    assert tem_capacidade_livro({"livro_profundidade": {"declarada": 10, "observada": 5}})
    assert not tem_capacidade_livro({"livro_profundidade": {"declarada": 0}})
    assert not tem_capacidade_livro({})  # desconhecido => sem capacidade (falha segura)
    with pytest.raises(CapacidadeAusente):
        exigir_capacidade_livro({"capacidades": {"livro": False}})


def test_terminal_via_gateway_serve_o_mesmo_snapshot_e_none_sem_resposta():
    snap = {"tipo": "snapshot", "ok": True, "positions": [{"symbol": "A", "ticket": 1}, {"symbol": "B", "ticket": 2}],
            "orders": [], "deals": [{"symbol": "A", "ticket": 9}]}
    t = TerminalViaGateway(CanalFalso(snapshot=snap))
    assert t.positions_get() is None  # antes de atualizar: sem dado
    assert t.atualizar() and [p.ticket for p in t.positions_get(symbol="A")] == [1] and len(t.positions_get()) == 2
    assert t.history_deals_get(0, 1)[0].ticket == 9
    assert not TerminalViaGateway(CanalFalso(snapshot=None)).atualizar()
    assert not TerminalViaGateway(CanalFalso(snapshot={"tipo": "snapshot", "ok": False})).atualizar()


def test_emissor_devolve_none_sem_resposta_e_ack_quando_ha():
    em = EmissorGateway(CanalFalso(ordem=None))
    c = ComandoOrdem(intencao_id="x", magic=1, lado=Lado.COMPRA, tipo="mercado", sl=1.0)
    assert em(c) is None and em.sem_resposta == 1
    em2 = EmissorGateway(CanalFalso(ordem={"tipo": "ack", "ack": {"retcode": 10009, "order": 3}}))
    a = em2(c)
    assert isinstance(a, AckOrdem) and a.retcode == 10009 and em2.sem_resposta == 0


def test_espelho_e_kill_zera_pelo_gateway_in_process(tmp_path):
    """Gateway local (sem processo): kill do braco espelhado manda EMERGENCIA; a demo zera e trava."""
    g = montar_gw(tmp_path)
    orq, _, _ = montar_orq_demo(rel=g.rel)

    class Canal:
        def enviar(self, msg, timeout_s=2.0):
            return g.cli.enviar(msg, timeout_s=min(timeout_s, 1.0), bombear=g.gw.passo)

        def enviar_sem_resposta(self, msg):
            g.cli.enviar_sem_resposta(msg)

    e = EspelhoDemo(orq, ConfigEspelho(braco="a"), Canal())
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    e.iniciar()
    passos(g, 3)
    e.ao_estado_gateway(g.gw.estado())
    from robowin.core.tipos import Acao, Intencao
    it = Intencao(id="i1", braco="a", acao=Acao.COMPRAR, stop_pontos=60.0, validade_ns=g.rel.agora_ns() + 10**11)
    ask = float(g.d.symbol_info_tick(SIMB).ask)
    r = e.espelhar_entrada(g.rel.agora_ns(), Posicao(simbolo=SIMB, lado=Lado.COMPRA, quantidade=1, preco_medio=ask), it, 60.0,
                           None, 600)
    assert r.enviado and g.d.posicao_liquida(SIMB) == 1.0
    assert e.crenca.ticket == int(g.d.positions_get()[0].ticket)
    orq.contas["a"].kill.acionar("teste", "manual_cli", g.rel.agora_ns())  # efeito do kill => espelho.ao_kill
    assert g.d.posicao_liquida(SIMB) == 0.0 and g.gw.guardiao.travado_emergencia
    assert e.crenca.quantidade == 0 and any(ev[0] == "espelho_emergencia" for ev in e.eventos)
    # saida duplicada e recusada (UM dono): com a posicao fechada nao ha o que enviar
    assert not e.saida("de_novo", g.rel.agora_ns()).enviado
    # reconciliacao em cima do gateway local: sem divergencia
    assert e.reconciliar(g.rel.agora_ns()) is not None
    sqlite3.connect(":memory:").close()
