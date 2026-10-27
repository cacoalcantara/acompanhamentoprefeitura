"""Regressoes da revisao adversarial: laco do gateway, auditoria em disco, stop obrigatorio, saida em voo."""
from __future__ import annotations

import json

from test_mt5_comum import SIMB, cmd, montar_exec, ticks_planos
from test_mt5_gateway import montar_gw, ordem, passos
from test_mt5_guardiao import montar

from robowin.core.tipos import EstadoOrdem, Lado
from robowin.mt5 import constantes as C
from robowin.mt5.gateway import ConfigGateway, Gateway, bind_exposto, intencoes_enviadas_em
from robowin.mt5.guardiao import ConfigGuardiao


# --- 1: o laco do gateway sobrevive a mensagem ruim e a excecao no ciclo -------------------------------
def test_mensagem_nao_dict_e_malformada_nao_param_o_gateway(tmp_path):
    g = montar_gw(tmp_path)
    g.canal._q.put(("x", "lixo-nao-dict"))
    g.canal._q.put(("y", {"tipo": "ordem"}))  # sem `cmd`: KeyError
    g.canal._q.put(("z", {"tipo": "ordem", "cmd": {"intencao_id": 1}}))  # validacao pydantic
    passos(g, 3)
    assert g.gw.parado is None and g.gw.erros_mensagem == 3
    assert any(t == "erro_mensagem" for t, _ in g.gw.incidentes)
    assert ordem(g)["estado"] == "EXECUTADA"  # segue atendendo


def test_excecao_no_ciclo_do_guardiao_nao_para_o_gateway(tmp_path):
    g = montar_gw(tmp_path)

    def quebra():
        raise RuntimeError("falha inesperada")
    g.gw.guardiao.ciclo = quebra
    passos(g, 10)
    assert g.gw.parado is None and g.gw.erros_ciclo >= 1
    assert any(t == "erro_ciclo_guardiao" for t, _ in g.gw.incidentes)
    n = len([e for e in g.ev if e["topico"] == "heartbeat"])
    passos(g, 10)
    assert len([e for e in g.ev if e["topico"] == "heartbeat"]) > n


# --- 2: auditoria em disco e idempotencia apos reinicio -------------------------------------------------
def test_intencao_vai_a_disco_antes_do_envio_e_sobrevive_a_reinicio(tmp_path):
    g = montar_gw(tmp_path)
    g.cli.enviar_sem_resposta({"tipo": "heartbeat"})
    c = cmd(Lado.COMPRA, sl=99900.0, iid="persistida-1")
    r = g.cli.enviar({"tipo": "ordem", "cmd": c.model_dump(mode="json")}, bombear=g.gw.passo)
    assert r["estado"] == "EXECUTADA"
    arqs = list((tmp_path / "log" / "auditoria").glob("auditoria-*.jsonl"))
    assert arqs
    tipos = [json.loads(ln)["tipo"] for a in arqs for ln in a.read_text().splitlines()]
    assert "intencao_antes_envio" in tipos
    assert intencoes_enviadas_em(tmp_path / "log" / "auditoria") == {"persistida-1"}
    # fecha a posicao e "reinicia" o gateway sobre o mesmo terminal e a mesma auditoria
    tk = int(g.d.positions_get()[0].ticket)
    assert ordem(g, Lado.VENDA, tipo="saida", ticket=tk)["estado"] == "EXECUTADA"
    novo = Gateway(g.d, g.gw.cfg, g.rel, canal=None, dormir=lambda s: g.rel.avancar(int(s * 1e9)))
    novo.iniciar()
    n = len(g.d.envios)
    novo.guardiao.heartbeat()
    resp = novo.tratar({"tipo": "ordem", "id": "r", "cmd": c.model_dump(mode="json")})
    assert resp["ack"]["retcode"] == C.RET_LOCAL_RECUSADO and "idempotencia" in resp["ack"]["comentario"]
    assert len(g.d.envios) == n
    # zerar/recusa/rearme tambem chegam a disco
    novo.guardiao.rearmar("teste")
    tipos = [json.loads(ln)["tipo"] for a in arqs for ln in a.read_text().splitlines()]
    assert "rearme" in tipos and "recusa_local" in tipos


def test_gateway_sem_diretorio_alerta_que_auditoria_e_so_memoria(tmp_path):
    from test_mt5_comum import LOGIN, SERVIDOR, montar_duble
    d, rel = montar_duble(ticks_planos(100))
    gw = Gateway(d, ConfigGateway(login=LOGIN, servidor=SERVIDOR), rel)
    gw.iniciar()
    assert any(t == "auditoria_so_memoria" for t, _ in gw.incidentes)


# --- 5: entrada sem stop / stop errado / stop longe demais ----------------------------------------------
def test_entrada_exige_stop_valido():
    a = montar(cfg_g=ConfigGuardiao(stop_max_pontos=300.0))
    # ticks planos: bid 100000 / ask 100005
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA, sl=99900.0))[0]
    assert a.g.avaliar_entrada(cmd(Lado.VENDA, sl=100100.0))[0]
    ok, m = a.g.avaliar_entrada(cmd(Lado.COMPRA))
    assert not ok and "sem stop" in m
    assert not a.g.avaliar_entrada(cmd(Lado.COMPRA, sl=100100.0))[0]  # lado errado
    assert not a.g.avaliar_entrada(cmd(Lado.VENDA, sl=99900.0))[0]  # lado errado
    ok, m = a.g.avaliar_entrada(cmd(Lado.COMPRA, sl=99600.0))
    assert not ok and "acima do maximo" in m
    assert not a.g.avaliar_entrada(cmd(Lado.COMPRA, "limitada", preco=99950.0, sl=99600.0))[0]
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA, "limitada", preco=99950.0, sl=99800.0))[0]
    assert a.g.avaliar_entrada(cmd(Lado.VENDA, "saida", 1))[0]  # saida nunca exige stop


# --- 6: saida em voo e conferencia com deal == 0 ---------------------------------------------------------
def test_saida_recusada_se_ha_ordem_de_sentido_oposto_em_voo():
    a = montar_exec(ticks_planos(), rejeita_ticket_velho=False)
    assert a.ex.enviar(cmd(Lado.COMPRA)).retcode == C.RET_EXECUTADA
    tk = int(a.d.positions_get(symbol=SIMB)[0].ticket)
    # saida do dono A colocada (10008) e ainda aberta: ordem de venda pendente
    res = a.d.order_send({"action": C.ACAO_PENDING, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL_LIMIT,
                          "price": 100200.0, "magic": 7, "type_filling": C.PREENCHE_RETURN, "type_time": C.VALIDADE_DIA})
    assert res.retcode == C.RET_COLOCADA
    n = len(a.d.envios)
    ack = a.ex.enviar(cmd(Lado.VENDA, "saida", tk), dono="B")
    assert ack.retcode == C.RET_LOCAL_RECUSADO and "em voo" in ack.comentario
    assert len(a.d.envios) == n and a.d.posicao_liquida(SIMB) == 1.0
    # o guardiao cancela as pendentes primeiro e depois zera: continua possivel
    a.ex.cancelar_ordem(int(a.d.orders_get()[0].ticket), prioritaria=True)
    assert a.ex.zerar_posicao(tk, dono="guardiao").retcode == C.RET_EXECUTADA
    assert a.d.posicao_liquida(SIMB) == 0.0


def test_conferencia_pos_deal_roda_mesmo_com_deal_zero():
    a = montar_exec(ticks_planos(), rejeita_ticket_velho=False)
    a.ex.enviar(cmd(Lado.COMPRA))
    tk = int(a.d.positions_get(symbol=SIMB)[0].ticket)
    a.d.gancho_antes_order_send = lambda d: (setattr(d, "gancho_antes_order_send", None),
                                             d.operar_manual(C.DEAL_VENDA, 1.0, SIMB))
    orig = a.d.order_send

    def sem_deal(req):
        r = orig(req)
        r.deal = 0
        return r
    a.d.order_send = sem_deal
    a.ex.enviar(cmd(Lado.VENDA, "saida", tk))
    assert "inversao_netting" in [t for t, _ in a.alertas]


# --- 7: duplicata nao sobrescreve o estado da original -----------------------------------------------------
def test_duplicata_nao_marca_rejeitada_a_intencao_original():
    a = montar_exec(ticks_planos())
    c = cmd(Lado.COMPRA, iid="dup-1")
    assert a.ex.enviar(c).retcode == C.RET_EXECUTADA
    assert a.ex.estados["dup-1"] == EstadoOrdem.EXECUTADA
    assert a.ex.enviar(c).retcode == C.RET_LOCAL_RECUSADO
    assert a.ex.estados["dup-1"] == EstadoOrdem.EXECUTADA


# --- 13: bind exposto ---------------------------------------------------------------------------------------
def test_bind_exposto_detecta_todas_as_interfaces():
    assert bind_exposto("tcp://0.0.0.0:5556") and bind_exposto("tcp://*:5556")
    assert not bind_exposto("tcp://127.0.0.1:5556")
