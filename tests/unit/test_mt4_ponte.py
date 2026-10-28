"""ConectorMT4 x DubleMT4: conta, tick, ordem, fechamento por ticket, recusa de conta real, INCERTA, ZeroMQ real."""
from __future__ import annotations

import time

import numpy as np
import pytest
from test_mt4_comum import LOGIN, SERVIDOR4, SIMB, T0_MS, cmd, montar_exec4, montar_mt4

from robowin.core.protocolos import TerminalMT5
from robowin.core.tipos import EstadoOrdem, Lado
from robowin.mt5 import constantes as C
from robowin.mt5 import protocolo_mt4 as P
from robowin.mt5.coleta import ColetorTicks
from robowin.mt5.conector_mt4 import ConectorMT4, TransporteZMQ, razao_do_deal_saida
from robowin.mt5.duble_mt4 import Falha4, ServidorDubleMT4
from robowin.mt5.execucao_demo import ExecucaoRealNaoAutorizada, GuardaDemoViolada


def test_conector_cumpre_o_protocolo_e_se_rotula_duble():
    d, con, _ = montar_mt4()
    assert isinstance(con, TerminalMT5)
    assert con.eh_duble is True and d.eh_duble is True
    assert con.plataforma == "mt4" and con.capacidades.conta == "hedging" and not con.capacidades.livro


def test_account_info_demo_hedging_e_flags():
    d, con, _ = montar_mt4()
    ai = con.account_info()
    assert ai.login == LOGIN and ai.server == SERVIDOR4
    assert ai.trade_mode == C.CONTA_DEMO and ai.margin_mode == C.MARGEM_HEDGING
    assert ai.trade_allowed and ai.trade_expert
    ti = con.terminal_info()
    assert ti.connected and ti.trade_allowed
    d.definir_conta(expert=False)
    assert not con.account_info().trade_expert and not con.terminal_info().trade_allowed


def test_conta_real_do_mt4_e_vista_como_real_e_nenhuma_ordem_sai():
    a = montar_exec4(demo=False)
    assert a.con.account_info().trade_mode == C.CONTA_REAL
    with pytest.raises(ExecucaoRealNaoAutorizada):
        a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    assert a.d.envios == []
    assert [c["cmd"] for c in a.d.comandos if c["cmd"] in P.COMANDOS_ORDEM] == []


def test_ea_recusa_ordem_em_conta_real_mesmo_sem_a_guarda_do_python():
    d, con, _ = montar_mt4(demo=False)
    res = con.order_send({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_BUY, "price": 100005.0})
    assert res.retcode == C.RET_TRADE_DESABILITADO and "1006" in res.comment
    assert d.posicoes == {} and d.envios == []


def test_simbolos_info_e_tick_com_tempo_em_segundos():
    d, con, _ = montar_mt4()
    assert [s.name for s in con.symbols_get("*WIN*")] == [SIMB]
    assert con.symbols_get("*XYZ*") == ()
    info = con.symbol_info(SIMB)
    assert info.trade_tick_size == 5.0 or info.trade_tick_size > 0
    assert info.ticks_bookdepth == 0 and info.volume_min == 1.0
    tk = con.symbol_info_tick(SIMB)
    assert tk.time_msc == tk.time * 1000 and tk.time_msc % 1000 == 0  # sem milissegundos inventados
    assert tk.flags == (C.FLAG_BID | C.FLAG_ASK) and tk.last == 0.0 and tk.seq > 0
    assert con.symbol_select(SIMB, True) is True and con.symbol_info("NADA") is None


def test_sem_livro_e_sem_historico_de_ticks():
    d, con, _ = montar_mt4()
    assert con.market_book_add(SIMB) is False and con.market_book_get(SIMB) is None
    assert con.copy_ticks_range(SIMB, 0, 1, C.COPY_TICKS_ALL) is None
    assert con.suporta_backfill is False


def test_ticks_do_mesmo_segundo_nao_sao_duplicatas_e_cursor_nao_repete():
    d, con, rel = montar_mt4()
    bus = []

    class Bus:
        def publicar(self, topico, msg):
            bus.append(msg)

    from robowin.mt5.tempo_servidor import RelogioServidor
    rs = RelogioServidor(10_800_000, 0, True)
    col = ColetorTicks(con, SIMB, rel, rs, Bus(), inicio_ns=rel.agora_ns() - 5_000_000_000)
    n1 = col.sondar()
    assert n1 == 11  # 11 ticks de 100 ms ja ocorridos (t0-1s .. t0), no maximo 2 segundos de relogio
    rel.avancar(2_000_000_000)
    n2 = col.sondar()
    assert n2 == 20 and col.duplicados == 0
    assert col.sondar() == 0  # sem novos ticks => nada repetido
    ts = [m["t_servidor_ms"] for m in bus]
    assert ts == sorted(ts) and all(t % 1000 == 0 for t in ts) and len(set(ts)) < len(ts)  # varios por segundo


def test_copy_ticks_from_array_tem_seq_gtc_e_filtra_por_data():
    d, con, rel = montar_mt4()
    arr = con.copy_ticks_from(SIMB, 0, 100, C.COPY_TICKS_ALL)
    assert isinstance(arr, np.ndarray) and len(arr) == 11
    assert list(arr["seq"]) == list(range(1, 12)) and arr["gtc"].dtype.kind == "i"
    assert (arr["time_msc"] == arr["time"] * 1000).all()
    assert len(con.copy_ticks_from(SIMB, 0, 100, C.COPY_TICKS_ALL)) == 0  # cursor avancou
    rel.avancar(1_000_000_000)
    d2, con2, _ = montar_mt4()
    agora_s = int(con2.symbol_info_tick(SIMB).time)
    assert len(con2.copy_ticks_from(SIMB, agora_s + 3600, 100, C.COPY_TICKS_ALL)) == 0  # data futura descarta tudo


def test_entrada_e_saida_por_ticket_hedging():
    a = montar_exec4()
    ack = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    assert ack.retcode == C.RET_EXECUTADA and a.d.tickets_abertos() == [ack.order]
    ps = a.con.positions_get(symbol=SIMB)
    assert len(ps) == 1 and ps[0].type == C.POS_COMPRA and ps[0].sl == 99900.0 and ps[0].magic == 7
    saida = a.ex.zerar_posicao(ack.order)
    assert saida.retcode == C.RET_EXECUTADA
    assert a.d.tickets_abertos() == []
    abrir = [e for e in a.d.envios if e["cmd"] == P.CMD_ORDEM_ABRIR]
    fechar = [e for e in a.d.envios if e["cmd"] == P.CMD_ORDEM_FECHAR]
    assert len(abrir) == 1 and len(fechar) == 1 and fechar[0]["args"]["ticket"] == ack.order


def test_ordem_pendente_cancelamento_e_modificacao_de_sltp():
    a = montar_exec4()
    ack = a.ex.enviar(cmd(Lado.COMPRA, tipo="limitada", preco=99950.0, sl=99900.0))
    assert ack.retcode == C.RET_COLOCADA
    os_ = a.con.orders_get(symbol=SIMB)
    assert len(os_) == 1 and os_[0].type == C.ORDEM_BUY_LIMIT and os_[0].volume_current == 1.0
    assert a.ex.cancelar_ordem(ack.order).retcode == C.RET_EXECUTADA
    assert a.con.orders_get(symbol=SIMB) == ()
    ent = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    assert a.ex.modificar_sltp(ent.order, 99950.0, None).retcode == C.RET_EXECUTADA
    assert a.con.positions_get(symbol=SIMB)[0].sl == 99950.0
    assert "afrouxar" in a.ex.modificar_sltp(ent.order, 99900.0, None).comentario  # stop so aperta


def test_order_check_simulado_localmente():
    d, con, _ = montar_mt4()
    base = {"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_BUY, "price": 100005.0}
    assert con.order_check(base).retcode == 0
    assert con.order_check({**base, "volume": 0.5}).retcode == C.RET_VOLUME_INVALIDO
    assert con.order_check({**base, "volume": 5000.0}).retcode == C.RET_VOLUME_INVALIDO
    assert con.order_check({**base, "sl": 100100.0}).retcode == C.RET_STOPS_INVALIDOS  # SL de compra acima do bid
    assert con.order_check({**base, "symbol": "NADA"}).retcode == C.RET_INVALIDO
    assert con.order_check({**base, "action": 99}).retcode == C.RET_INVALIDO
    assert con.order_check({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL,
                            "position": 123}).retcode == C.RET_POSICAO_FECHADA
    d.definir_conta(expert=False)
    assert con.order_check(base).retcode == C.RET_ALGO_DESLIGADO_CLIENTE
    d.definir_conta(expert=True, trade_allowed=False)
    assert con.order_check(base).retcode == C.RET_TRADE_DESABILITADO
    assert d.envios == [] and not [c for c in d.comandos if c["cmd"] in P.COMANDOS_ORDEM]  # check nunca envia


def test_order_check_margem_insuficiente():
    d, con, _ = montar_mt4()
    d._specs[SIMB]["margem_requerida"] = 1e9
    con.limpar_cache()
    ch = con.order_check({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_BUY, "price": 1.0})
    assert ch.retcode == C.RET_SEM_DINHEIRO


def test_order_send_com_position_nunca_abre_posicao_oposta():
    d, con, _ = montar_mt4()
    # position invalido (0): NADA e enviado ao EA (em conta hedging abriria uma posicao nova)
    r = con.order_send({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL, "position": 0})
    assert r.retcode == C.RET_INVALIDO
    # ticket inexistente: o EA recusa e nenhuma posicao nasce
    r = con.order_send({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL, "position": 999})
    assert r.retcode == C.RET_POSICAO_FECHADA
    assert d.posicoes == {} and not [e for e in d.envios if e["cmd"] == P.CMD_ORDEM_ABRIR]


def test_erros_do_mt4_viram_retcodes():
    for cod, esperado in [(P.M4_REQUOTE, C.RET_REQUOTE), (P.M4_MUITAS_REQUISICOES, C.RET_MUITAS_REQUISICOES),
                          (P.M4_SEM_DINHEIRO, C.RET_SEM_DINHEIRO), (P.M4_TRADE_NAO_PERMITIDO, C.RET_ALGO_DESLIGADO_CLIENTE),
                          (P.M4_TIMEOUT, C.RET_TIMEOUT), (P.M4_SEM_CONEXAO, C.RET_SEM_CONEXAO), (9999, C.RET_ERRO)]:
        assert P.retcode_de_erro(cod) == esperado
    a = montar_exec4()
    a.d.injetar_falhas(P.M4_REQUOTE)
    ack = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    assert ack.retcode == C.RET_REQUOTE and a.d.posicoes == {}


def test_sem_resposta_vira_incerta_e_nunca_reenvia():
    a = montar_exec4()
    a.d.injetar_falhas(Falha4(None, executa=True))  # executou no EA, mas a resposta se perdeu
    ack = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0, iid="x1"))
    assert ack.retcode is None and "INCERTA" in ack.comentario
    assert a.ex.estados["x1"] == EstadoOrdem.INCERTA and a.ex.bloqueada_incerta
    assert len(a.d.posicoes) == 1  # a ordem existe no servidor
    assert [c["cmd"] for c in a.d.comandos].count(P.CMD_ORDEM_ABRIR) == 1  # nunca reenviada
    # reenviar a mesma intencao e recusado; nova entrada bloqueada ate reconciliar
    assert a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0, iid="x1")).retcode == C.RET_LOCAL_RECUSADO
    a.rel.avancar(1_000_000_000)
    res = a.ex.reconciliar_incertas()
    assert res == {"x1": EstadoOrdem.EXECUTADA}  # evidencia: deal de entrada EXPERT com o mesmo magic (posicao aberta)
    assert not a.ex.bloqueada_incerta


def test_incerta_sem_efeito_resolve_por_prazo():
    a = montar_exec4(cfg_extra={"espera_incerta_s": 1.0})
    a.d.injetar_falhas(Falha4(None, executa=False))
    ack = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0, iid="x2"))
    assert ack.retcode is None and a.d.posicoes == {}
    assert a.ex.reconciliar_incertas() == {}
    a.rel.avancar(3_000_000_000)
    assert a.ex.reconciliar_incertas() == {"x2": EstadoOrdem.REJEITADA}


def test_idempotencia_do_ea_por_id_reenvio_devolve_a_mesma_resposta():
    d, con, _ = montar_mt4()
    msg = P.requisicao("fixo-1", P.CMD_ORDEM_ABRIR, {"simbolo": SIMB, "tipo": C.ORDEM_BUY, "volume": 1.0, "magic": 7})
    r1, r2 = d.processar(msg), d.processar(msg)
    assert r1 == r2 and len(d.posicoes) == 1


def test_fechamento_parcial_gera_novo_ticket_no_restante():
    d, con, _ = montar_mt4()
    r = d.processar(P.requisicao("a", P.CMD_ORDEM_ABRIR, {"simbolo": SIMB, "tipo": C.ORDEM_BUY, "volume": 3.0, "magic": 7}))
    tk = r["dados"]["ticket"]
    res = con.order_send({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL, "position": tk})
    assert res.retcode == C.RET_EXECUTADA
    ps = con.positions_get(symbol=SIMB)
    assert len(ps) == 1 and ps[0].ticket != tk and ps[0].volume == 2.0 and ps[0].comment == f"from #{tk}"


def test_historico_vira_deals_in_e_out_com_razoes():
    a = montar_exec4(ticks=[(T0_MS - 1000 + i * 100, 100000.0 - (i // 3) * 5.0, 100005.0 - (i // 3) * 5.0, 0.0, 0.0, 6)
                            for i in range(200)])
    ent = a.ex.enviar(cmd(Lado.COMPRA, sl=99960.0))
    assert ent.retcode == C.RET_EXECUTADA
    a.rel.avancar(5_000_000_000)  # o preco cai e o SL fecha no servidor
    assert a.d.tickets_abertos() == []
    deals = a.con.history_deals_get(0, 2_000_000_000)
    assert [(x.entry, x.type) for x in deals] == [(C.ENTRADA_IN, C.DEAL_COMPRA), (C.ENTRADA_OUT, C.DEAL_VENDA)]
    assert deals[0].reason == C.RAZAO_EXPERT and deals[1].reason == C.RAZAO_SL
    assert deals[0].ticket != deals[1].ticket and deals[1].profit < 0
    assert len(a.con.history_orders_get(0, 2_000_000_000)) == 1


def test_razao_de_saida_manual_robo_e_desconhecido():
    base = {"magic": 7, "comentario": "", "fechado_ea": 1}
    assert razao_do_deal_saida(base) == C.RAZAO_EXPERT
    assert razao_do_deal_saida({**base, "fechado_ea": 0}) == C.RAZAO_CLIENT  # magic do robo, mas fechada fora da ponte
    assert razao_do_deal_saida({**base, "fechado_ea": -1}) == C.RAZAO_EXPERT  # EA reiniciou: nao ha como saber
    assert razao_do_deal_saida({"magic": 0, "comentario": "", "fechado_ea": -1}) == C.RAZAO_CLIENT
    assert razao_do_deal_saida({**base, "comentario": "x [tp]"}) == C.RAZAO_TP
    assert razao_do_deal_saida({**base, "comentario": "so: 1"}) == C.RAZAO_SO


def test_operacao_manual_e_classificada_e_bloqueia_entrada_do_robo():
    a = montar_exec4()
    tk = a.d.operar_manual(C.POS_COMPRA, 1.0, SIMB)
    deals = a.con.history_deals_get(0, 2_000_000_000)
    assert deals[0].reason == C.RAZAO_CLIENT and deals[0].magic == 0  # manual => guardiao alerta e bloqueia
    ack = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    assert ack.retcode == C.RET_LOCAL_RECUSADO and "ja ha posicao" in ack.comentario
    assert a.d.tickets_abertos() == [tk]


def test_hedging_configurado_exige_conta_hedging_e_netting_recusa_hedging():
    a = montar_exec4()
    a.ex.cfg.conta = "netting"  # configuracao de netting contra conta hedging => guarda recusa
    with pytest.raises(GuardaDemoViolada):
        a.ex.verificar_guarda()


def test_initialize_falha_com_ea_ausente_e_versao_incompativel():
    d, con, _ = montar_mt4()
    d.fora_do_ar = True
    novo = ConectorMT4(transporte=con._tr)
    assert novo.initialize() is False and novo.last_error()[0] == C.ERR_SEM_CONEXAO[0]
    assert novo.account_info() is None and novo.positions_get() is None and novo.copy_ticks_from(SIMB, 0, 10, -1) is None
    d.fora_do_ar = False
    resp = d.processar({"v": 99, "id": "z", "cmd": P.CMD_OLA, "args": {}})
    assert resp["ok"] is False and resp["erro"]["codigo"] == P.ERRO_VERSAO
    assert d.processar({"v": 1, "id": "z", "cmd": "nao_existe"})["erro"]["codigo"] == P.ERRO_COMANDO_DESCONHECIDO
    assert P.codificar(P.decodificar(d.processar_bytes(b"{"))) and d.processar_bytes(b"[]")


def test_perda_real_de_ticks_e_reinicio_do_ea_contam_lacunas():
    d, con, rel = montar_mt4(capacidade_ring=5)
    assert len(con.copy_ticks_from(SIMB, 0, 100, -1)) == 5  # so cabem 5 no anel
    rel.avancar(1_000_000_000)
    con.copy_ticks_from(SIMB, 0, 100, -1)
    assert con.lacunas_ticks == 1  # o anel estourou entre duas leituras
    d.reiniciar_ea()
    rel.avancar(100_000_000)
    con.copy_ticks_from(SIMB, 0, 100, -1)
    assert con.lacunas_ticks == 2


# --- ZeroMQ real (transporte, timeout e reconexao) --------------------------------------------------------
def test_zeromq_real_conta_ordem_e_reconexao_apos_timeout():
    d, _, _ = montar_mt4()
    with ServidorDubleMT4(d, atraso_sem_resposta_s=0.3) as srv:
        con = ConectorMT4(srv.endereco, timeout_ms=150)
        assert isinstance(con._tr, TransporteZMQ)
        assert con.initialize() and con.account_info().login == LOGIN
        res = con.order_send({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_BUY, "price": 100005.0,
                              "magic": 7})
        assert res.retcode == C.RET_EXECUTADA and len(d.posicoes) == 1
        d.injetar_falhas(Falha4(None, executa=True))  # executa no EA, resposta tardia => timeout no cliente
        tk = next(iter(d.posicoes))
        assert con.order_send({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL,
                               "position": tk}) is None  # INCERTA
        assert d.posicoes == {}  # fechou mesmo sem resposta
        time.sleep(0.5)  # a resposta tardia e descartada; o cliente recria o socket e segue funcionando
        assert con.account_info().login == LOGIN and con._tr.reconexoes >= 2
        con.shutdown()


def test_zeromq_sem_servidor_retorna_none_sem_travar():
    con = ConectorMT4("tcp://127.0.0.1:59999", timeout_ms=100)
    assert con.initialize() is False and con.account_info() is None
    con.shutdown()
