"""Execucao demo: guarda de conta, INCERTA, retcodes, netting, idempotencia, orcamento (dados sinteticos)."""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_mt5_comum import SERVIDOR, SIMB, T0_MS, cmd, montar_exec, ticks_planos

from robowin.core.tipos import EstadoOrdem, Lado
from robowin.mt5 import constantes as C
from robowin.mt5.duble import Falha, gerar_ticks_sinteticos
from robowin.mt5.execucao_demo import (
    ExecucaoReal,
    ExecucaoRealNaoAutorizada,
    GuardaDemoViolada,
    estado_do_retcode,
)


def entrar(a, lado=Lado.COMPRA, **kw):
    ack = a.ex.enviar(cmd(lado, **kw))
    return ack


def ticket(a):
    ps = a.d.positions_get(symbol=SIMB)
    return int(ps[0].ticket) if ps else None


# --- guarda demo ---------------------------------------------------------------------------------
@pytest.mark.parametrize("modo", ["real", "contest"])
def test_conta_real_ou_contest_zero_envios(modo):
    a = montar_exec(ticks_planos(), trade_mode=modo)
    with pytest.raises(ExecucaoRealNaoAutorizada):
        entrar(a)
    assert a.d.envios == [] and a.d.checks == []


def test_troca_de_login_no_meio_da_sessao_zero_envios_depois():
    a = montar_exec(ticks_planos())
    assert entrar(a).retcode == C.RET_EXECUTADA and len(a.d.envios) == 1
    a.d.trocar_conta(777, SERVIDOR, "demo")
    with pytest.raises(GuardaDemoViolada):
        a.ex.zerar_posicao(ticket(a) or 1)
    with pytest.raises(GuardaDemoViolada):
        entrar(a)
    assert len(a.d.envios) == 1


def test_troca_para_conta_real_no_meio_levanta_execucao_real():
    a = montar_exec(ticks_planos())
    a.d.trocar_conta(777, SERVIDOR, "real")
    with pytest.raises(ExecucaoRealNaoAutorizada):
        entrar(a)
    assert a.d.envios == []


def test_servidor_trade_expert_trade_allowed_e_modo_config():
    a = montar_exec(ticks_planos())
    a.d.definir_conta(servidor="XPMT5-PROD")
    with pytest.raises(GuardaDemoViolada):
        entrar(a)
    a.d.definir_conta(servidor="OUTRO-DEMO")  # contem DEMO mas nao e o servidor exato
    with pytest.raises(GuardaDemoViolada):
        entrar(a)
    a.d.definir_conta(servidor=SERVIDOR, trade_expert=False)
    with pytest.raises(GuardaDemoViolada):
        entrar(a)
    a.d.definir_conta(trade_expert=True, trade_allowed=False)
    with pytest.raises(GuardaDemoViolada):
        entrar(a)
    a.d.definir_conta(trade_allowed=True)
    a.d.terminal_algo_ligado = False
    with pytest.raises(GuardaDemoViolada):
        entrar(a)
    assert a.d.envios == []
    b = montar_exec(ticks_planos(), cfg_extra={"modo_config": "interno"})
    with pytest.raises(GuardaDemoViolada):
        entrar(b)
    assert b.d.envios == []


def test_terminal_sem_resposta_nao_envia():
    a = montar_exec(ticks_planos())
    a.d.desconectar()
    with pytest.raises(GuardaDemoViolada):
        entrar(a)
    assert a.d.envios == []


def test_execucao_real_sempre_levanta():
    with pytest.raises(ExecucaoRealNaoAutorizada):
        ExecucaoReal().enviar(cmd(Lado.COMPRA))


# --- INCERTA ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("ret", [None, C.RET_TIMEOUT, C.RET_SEM_CONEXAO])
@pytest.mark.parametrize("executou", [False, True])
def test_none_10012_10031_viram_incerta_sem_reenvio(ret, executou):
    a = montar_exec(ticks_planos())
    a.d.injetar_falhas(Falha(ret, executa=executou))
    c = cmd(Lado.COMPRA)
    ack = a.ex.enviar(c)
    assert ack.retcode == ret and ack.comentario.startswith("INCERTA")
    assert a.ex.estados[c.intencao_id] == EstadoOrdem.INCERTA and a.ex.bloqueada_incerta
    assert len(a.d.envios) == 1
    # a mesma intencao e qualquer entrada nova NUNCA saem enquanto a incerta nao for reconciliada
    again = a.ex.enviar(c)
    assert again.retcode == C.RET_LOCAL_RECUSADO
    assert a.ex.enviar(cmd(Lado.VENDA)).retcode == C.RET_LOCAL_RECUSADO
    assert len(a.d.envios) == 1
    assert ("ordem_incerta", {"intencao_id": c.intencao_id, "retcode": ret}) in a.alertas
    # reconciliacao
    if executou:
        res = a.ex.reconciliar_incertas()
        assert res == {c.intencao_id: EstadoOrdem.EXECUTADA} and a.d.posicao_liquida(SIMB) == 1.0
    else:
        assert a.ex.reconciliar_incertas() == {}  # ainda dentro do prazo
        a.rel.avancar(6_000_000_000)
        assert a.ex.reconciliar_incertas() == {c.intencao_id: EstadoOrdem.REJEITADA}
    assert not a.ex.bloqueada_incerta and len(a.d.envios) == 1


def test_saida_incerta_bloqueia_nova_saida_do_mesmo_ticket():
    a = montar_exec(ticks_planos())
    entrar(a)
    tk = ticket(a)
    a.d.injetar_falhas(Falha(C.RET_TIMEOUT, executa=False))
    ack = a.ex.enviar(cmd(Lado.VENDA, "saida", tk))
    assert ack.retcode == C.RET_TIMEOUT
    n = len(a.d.envios)
    r = a.ex.zerar_posicao(tk, dono="guardiao")  # nem o guardiao pode reenviar enquanto ha incerta no ticket
    assert r.retcode == C.RET_LOCAL_RECUSADO and len(a.d.envios) == n


# --- retcodes --------------------------------------------------------------------------------------
def test_10024_recuo_exponencial_sem_laco():
    a = montar_exec(ticks_planos())
    a.d.injetar_falhas(C.RET_MUITAS_REQUISICOES, C.RET_MUITAS_REQUISICOES)
    assert entrar(a).retcode == C.RET_MUITAS_REQUISICOES
    assert len(a.d.envios) == 1
    assert entrar(a).retcode == C.RET_LOCAL_RECUSADO and len(a.d.envios) == 1  # recuo ativo
    a.rel.avancar(1_100_000_000)
    assert entrar(a).retcode == C.RET_MUITAS_REQUISICOES and len(a.d.envios) == 2
    a.rel.avancar(1_100_000_000)  # recuo agora e de 2 s
    assert entrar(a).retcode == C.RET_LOCAL_RECUSADO and len(a.d.envios) == 2
    a.rel.avancar(1_000_000_000)
    assert entrar(a).retcode == C.RET_EXECUTADA
    assert any(t == "recuo_10024" for t, _ in a.alertas)


def test_10027_gera_alerta_e_nao_repete():
    a = montar_exec(ticks_planos())
    a.d.injetar_falhas(C.RET_ALGO_DESLIGADO_CLIENTE)
    ack = entrar(a)
    assert ack.retcode == C.RET_ALGO_DESLIGADO_CLIENTE and len(a.d.envios) == 1
    assert any(t == "algotrading_desligado" for t, _ in a.alertas)


def test_10008_colocada_nao_e_executada_e_10010_parcial():
    assert estado_do_retcode(C.RET_COLOCADA) == EstadoOrdem.ACEITA
    assert estado_do_retcode(C.RET_EXECUTADA) == EstadoOrdem.EXECUTADA
    assert estado_do_retcode(C.RET_PARCIAL) == EstadoOrdem.PARCIAL
    assert estado_do_retcode(C.RET_REJEITADO) == EstadoOrdem.REJEITADA
    a = montar_exec(ticks_planos())
    ack = a.ex.enviar(cmd(Lado.COMPRA, "limitada", preco=99950.0))
    assert ack.retcode == C.RET_COLOCADA and a.ex.estados[next(iter(a.ex.estados))] == EstadoOrdem.ACEITA
    assert a.d.posicao_liquida(SIMB) == 0.0  # colocada != executada
    b = montar_exec(ticks_planos(), cfg_extra={"contratos_max": 2})
    b.d.injetar_falhas(Falha(C.RET_PARCIAL, volume=1.0))
    ack = b.ex.enviar(cmd(Lado.COMPRA, qtd=2))
    assert ack.retcode == C.RET_PARCIAL and b.d.posicao_liquida(SIMB) == 1.0


def test_order_check_falho_nao_envia():
    a = montar_exec(ticks_planos())
    a.d.injetar_falhas_check(None)
    assert entrar(a).retcode == C.RET_LOCAL_RECUSADO
    a.d.injetar_falhas_check(C.RET_SEM_DINHEIRO)
    ack = entrar(a)
    assert ack.retcode == C.RET_SEM_DINHEIRO and "order_check" in ack.comentario
    assert a.d.envios == [] and len(a.d.checks) == 2


def test_preenchimento_lido_do_simbolo():
    a = montar_exec(ticks_planos())
    entrar(a)
    assert a.d.envios[-1]["type_filling"] == C.PREENCHE_IOC
    b = montar_exec(ticks_planos())
    b.d.especificacoes[SIMB].filling_mode = C.SIMB_FILL_FOK
    entrar(b)
    assert b.d.envios[-1]["type_filling"] == C.PREENCHE_FOK


# --- netting ----------------------------------------------------------------------------------------
def test_saida_sempre_com_position_e_duplicada_nao_inverte():
    a = montar_exec(ticks_planos(), rejeita_ticket_velho=False)  # terminal "ingenuo": inverteria
    entrar(a)
    tk = ticket(a)
    r1 = a.ex.enviar(cmd(Lado.VENDA, "saida", tk), dono="nucleo")
    assert r1.retcode == C.RET_EXECUTADA and a.d.envios[-1]["position"] == tk
    r2 = a.ex.zerar_posicao(tk, dono="guardiao")  # SL do servidor + guardiao + ENCERRAR: saida repetida
    r3 = a.ex.enviar(cmd(Lado.VENDA, "saida", tk))
    assert r2.retcode == r3.retcode == C.RET_LOCAL_RECUSADO
    assert a.d.posicao_liquida(SIMB) == 0.0 and len(a.d.envios) == 2


def test_saida_com_lado_igual_ao_da_posicao_e_recusada():
    a = montar_exec(ticks_planos())
    entrar(a)
    assert a.ex.enviar(cmd(Lado.COMPRA, "saida", ticket(a))).retcode == C.RET_LOCAL_RECUSADO
    assert a.ex.enviar(cmd(Lado.VENDA, "saida", None)).retcode == C.RET_LOCAL_RECUSADO
    assert a.ex.enviar(cmd(Lado.VENDA, "saida", 123)).retcode == C.RET_LOCAL_RECUSADO
    assert a.d.posicao_liquida(SIMB) == 1.0 and len(a.d.envios) == 1


def test_entrada_oposta_com_posicao_aberta_e_recusada():
    a = montar_exec(ticks_planos())
    entrar(a)
    assert entrar(a, Lado.VENDA).retcode == C.RET_LOCAL_RECUSADO  # nao fecha/inverte por entrada
    assert entrar(a, Lado.COMPRA).retcode == C.RET_LOCAL_RECUSADO  # sem piramidacao
    assert a.d.posicao_liquida(SIMB) == 1.0


def test_pendente_conta_no_limite_de_contratos():
    a = montar_exec(ticks_planos())
    assert a.ex.enviar(cmd(Lado.COMPRA, "limitada", preco=99950.0)).retcode == C.RET_COLOCADA
    assert entrar(a, Lado.VENDA).retcode == C.RET_LOCAL_RECUSADO
    assert a.d.volume_pendente(SIMB) == 1.0
    assert a.ex.enviar(cmd(Lado.COMPRA, qtd=2)).retcode == C.RET_LOCAL_RECUSADO


def test_conferencia_de_sinal_detecta_inversao_e_corrige():
    a = montar_exec(ticks_planos(), rejeita_ticket_velho=False)
    entrar(a)
    tk = ticket(a)
    # intervencao concorrente fecha a posicao entre a leitura e o envio: a saida abriria uma venda
    a.d.gancho_antes_order_send = lambda d: (setattr(d, "gancho_antes_order_send", None),
                                             d.operar_manual(C.DEAL_VENDA, 1.0, SIMB))
    a.ex.enviar(cmd(Lado.VENDA, "saida", tk))
    tipos = [t for t, _ in a.alertas]
    assert "inversao_netting" in tipos
    assert a.d.posicao_liquida(SIMB) == 0.0  # saida corretiva zerou
    assert a.ex.travado and entrar(a).retcode == C.RET_LOCAL_RECUSADO
    a.ex.destravar("conferido pelo operador")
    assert entrar(a).retcode == C.RET_EXECUTADA


def test_dono_unico_de_saida():
    a = montar_exec(ticks_planos())
    entrar(a)
    tk = ticket(a)
    a.d.injetar_falhas(C.RET_REJEITADO)
    assert a.ex.enviar(cmd(Lado.VENDA, "saida", tk), dono="nucleo").retcode == C.RET_REJEITADO
    assert a.ex.enviar(cmd(Lado.VENDA, "saida", tk), dono="outro").retcode == C.RET_LOCAL_RECUSADO
    assert "dono" in a.ex.enviar(cmd(Lado.VENDA, "saida", tk), dono="outro").comentario
    assert a.ex.enviar(cmd(Lado.VENDA, "saida", tk), dono="nucleo").retcode == C.RET_EXECUTADA


def test_stop_so_aperta():
    a = montar_exec(ticks_planos())
    entrar(a, sl=99900.0)
    tk = ticket(a)
    assert a.ex.modificar_sltp(tk, 99800.0, None).retcode == C.RET_LOCAL_RECUSADO  # afrouxar
    assert a.ex.modificar_sltp(tk, 99950.0, None).retcode == C.RET_EXECUTADA
    assert a.d.positions_get()[0].sl == 99950.0
    assert a.ex.modificar_sltp(999, 99960.0, None).retcode == C.RET_LOCAL_RECUSADO


def test_idempotencia_e_limites_locais():
    a = montar_exec(ticks_planos())
    c = cmd(Lado.COMPRA, iid="fixa-1")
    assert a.ex.enviar(c).retcode == C.RET_EXECUTADA
    assert a.ex.enviar(c).retcode == C.RET_LOCAL_RECUSADO and len(a.d.envios) == 1
    b = montar_exec(ticks_planos())
    assert b.ex.enviar(cmd(Lado.COMPRA, qtd=2)).retcode == C.RET_LOCAL_RECUSADO
    assert b.d.envios == []


def test_orcamento_de_mensagens_por_minuto():
    a = montar_exec(ticks_planos(), cfg_extra={"msgs_por_min": 3})
    assert entrar(a).retcode == C.RET_EXECUTADA
    assert a.ex.enviar(cmd(Lado.VENDA, "saida", ticket(a))).retcode == C.RET_EXECUTADA
    assert entrar(a).retcode == C.RET_EXECUTADA
    ack = a.ex.enviar(cmd(Lado.VENDA, "saida", ticket(a)))
    assert ack.retcode == C.RET_LOCAL_RECUSADO and "orcamento" in ack.comentario
    assert len(a.d.envios) == 3
    # saida de seguranca nao e barrada pelo teto
    assert a.ex.zerar_posicao(ticket(a), dono="guardiao").retcode == C.RET_EXECUTADA
    a.rel.avancar(61_000_000_000)
    assert a.ex.orcamento.usadas() == 0 and entrar(a).retcode == C.RET_EXECUTADA


def test_cancelar_conta_no_orcamento_e_exige_ordem():
    a = montar_exec(ticks_planos(), cfg_extra={"msgs_por_min": 2})
    r = a.ex.enviar(cmd(Lado.COMPRA, "limitada", preco=99950.0))
    oid = int(a.d.orders_get()[0].ticket)
    assert a.ex.cancelar_ordem(999).retcode == C.RET_LOCAL_RECUSADO
    assert a.ex.cancelar_ordem(oid).retcode == C.RET_EXECUTADA and a.d.orders_get() == ()
    assert r.retcode == C.RET_COLOCADA and a.ex.orcamento.usadas() == 2


def test_checar_sem_enviar_nunca_chama_order_send():
    a = montar_exec(ticks_planos())
    r = a.ex.checar_sem_enviar()
    assert r["ok"] and a.d.envios == [] and len(a.d.checks) == 1


def test_intencao_gravada_antes_do_envio():
    ordem: list[str] = []
    a = montar_exec(ticks_planos())
    a.ex._registrar = lambda ev, dados: ordem.append(ev)
    a.d.gancho_antes_order_send = lambda d: ordem.append("order_send")
    entrar(a)
    assert ordem.index("intencao_antes_envio") < ordem.index("order_send")
    # se a gravacao falhar, nada e enviado
    b = montar_exec(ticks_planos())

    def quebra(ev, dados):
        raise OSError("disco cheio")
    b.ex._registrar = quebra
    with pytest.raises(OSError):
        entrar(b)
    assert b.d.envios == []


# --- propriedade: netting sem inversao em todas as intercalacoes --------------------------------------
EVENTOS = ["avancar", "avancar_muito", "entrar_c", "entrar_v", "entrar_c_sl", "limite_c", "sair_nucleo", "sair_guardiao",
           "sair_velho_c", "sair_velho_v", "sair_lado_errado", "falha_none_exec", "falha_none", "falha_10012_exec", "falha_10031",
           "falha_10024", "falha_parcial", "reconciliar", "sltp"]


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(eventos=st.lists(st.sampled_from(EVENTOS), min_size=1, max_size=45), semente=st.integers(0, 10_000),
       ingenuo=st.booleans())
def test_propriedade_netting_nunca_excede_um_contrato(eventos, semente, ingenuo):
    ticks = gerar_ticks_sinteticos(T0_MS - 1000, 4000, 100, semente=semente, passo_max_ticks=40)
    a = montar_exec(ticks, rejeita_ticket_velho=not ingenuo)
    velhos: list[int] = []
    n_envios = 0
    for ev in eventos:
        d, ex = a.d, a.ex
        tk_atual = d.symbol_info_tick(SIMB)
        tkt = ticket(a)
        if ev == "avancar":
            a.rel.avancar(300_000_000)
        elif ev == "avancar_muito":
            a.rel.avancar(7_000_000_000)
        elif ev in ("entrar_c", "entrar_v", "entrar_c_sl"):
            lado = Lado.VENDA if ev == "entrar_v" else Lado.COMPRA
            sl = tk_atual.bid - 200 if ev == "entrar_c_sl" else None
            ex.enviar(cmd(lado, sl=sl))
        elif ev == "limite_c":
            ex.enviar(cmd(Lado.COMPRA, "limitada", preco=tk_atual.bid - 50))
        elif ev == "sair_nucleo" and tkt:
            lado = Lado.VENDA if d.positions_get()[0].type == C.POS_COMPRA else Lado.COMPRA
            ex.enviar(cmd(lado, "saida", tkt), dono="nucleo")
            velhos.append(tkt)
        elif ev == "sair_guardiao" and tkt:
            ex.zerar_posicao(tkt, dono="guardiao")
            velhos.append(tkt)
        elif ev in ("sair_velho_c", "sair_velho_v") and velhos:
            ex.enviar(cmd(Lado.COMPRA if ev == "sair_velho_c" else Lado.VENDA, "saida", velhos[-1]))
            ex.zerar_posicao(velhos[-1], dono="guardiao")
        elif ev == "sair_lado_errado" and tkt:
            lado = Lado.COMPRA if d.positions_get()[0].type == C.POS_COMPRA else Lado.VENDA
            ex.enviar(cmd(lado, "saida", tkt))
        elif ev == "falha_none_exec":
            d.injetar_falhas(Falha(None, executa=True))
        elif ev == "falha_none":
            d.injetar_falhas(Falha(None))
        elif ev == "falha_10012_exec":
            d.injetar_falhas(Falha(C.RET_TIMEOUT, executa=True))
        elif ev == "falha_10031":
            d.injetar_falhas(Falha(C.RET_SEM_CONEXAO))
        elif ev == "falha_10024":
            d.injetar_falhas(C.RET_MUITAS_REQUISICOES)
        elif ev == "falha_parcial":
            d.injetar_falhas(Falha(C.RET_PARCIAL, volume=1.0))
        elif ev == "reconciliar":
            ex.reconciliar_incertas()
        elif ev == "sltp" and tkt:
            ex.modificar_sltp(tkt, tk_atual.bid - 100, None)
        # --- invariantes (estado REAL do terminal) ---
        pos, pend = d.posicao_liquida(SIMB), d.volume_pendente(SIMB)
        assert abs(pos) <= 1.0, (ev, pos)
        assert abs(pos) + pend <= 1.0, (ev, pos, pend)
        # nunca reenviar: cada intencao_id aparece no maximo uma vez
        assert len(d.envios) == ex.envios and ex.envios >= n_envios
        n_envios = ex.envios
    # nenhum deal pode ter aumentado a posicao alem de 1 nem invertido por saida do executor
    assert not any(t in ("inversao_netting", "saida_aumentou_posicao") for t, _ in a.alertas)
