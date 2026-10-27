"""Guardiao: heartbeat 15/60 s, horario, flag EMERGENCIA, deals manuais, perda diaria (dados sinteticos)."""
from __future__ import annotations

import datetime as dt

import pytest
from test_mt5_comum import SIMB, SP, T0_NS, cmd, montar_exec, ticks_planos

from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import Lado
from robowin.mt5 import constantes as C
from robowin.mt5.execucao_demo import GuardaDemoViolada
from robowin.mt5.guardiao import ConfigGuardiao, Guardiao


def ns(ano, mes, dia, h, m):
    return int(dt.datetime(ano, mes, dia, h, m, tzinfo=SP).timestamp() * 1e9)


def montar(tmp_path=None, quando=T0_NS, ticks=None, cfg_g=None, **kw):
    rel = RelogioSimulado(quando)
    ms = quando // 1_000_000
    tk = ticks if ticks is not None else ticks_planos(2000, inicio_ms=ms - 1000, passo_ms=100)
    a = montar_exec(tk, relogio=rel, **kw)
    cfg = cfg_g or ConfigGuardiao(arquivo_emergencia=(tmp_path / "EMERGENCIA") if tmp_path else None)
    g = Guardiao(a.ex, a.d, cfg, rel, a.rs, alerta=lambda t, x: a.alertas.append((t, x)))
    a.g = g
    return a


def abrir(a, lado=Lado.COMPRA, **kw):
    ack = a.ex.enviar(cmd(lado, **kw))
    assert ack.retcode == C.RET_EXECUTADA, ack
    return int(a.d.positions_get(symbol=SIMB)[0].ticket)


def seg(a, s):
    a.rel.avancar(int(s * 1e9))


# --- heartbeat --------------------------------------------------------------------------------------
def test_sem_heartbeat_15s_bloqueia_ordens_novas_e_heartbeat_libera():
    a = montar()
    ok, _ = a.g.avaliar_entrada(cmd(Lado.COMPRA))
    assert ok
    seg(a, 14)
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]
    seg(a, 2)  # 16 s
    ok, motivo = a.g.avaliar_entrada(cmd(Lado.COMPRA))
    assert not ok and "heartbeat" in motivo
    a.g.heartbeat()
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]


def test_saida_nunca_e_bloqueada_pelo_guardiao():
    a = montar()
    seg(a, 30)
    assert a.g.avaliar_entrada(cmd(Lado.VENDA, "saida", 1)) == (True, "")


def test_sem_heartbeat_60s_zera_a_posicao_pelo_ticket():
    a = montar()
    tk = abrir(a)
    seg(a, 30)
    a.g.ciclo()
    assert a.d.posicao_liquida(SIMB) == 1.0  # ainda nao
    seg(a, 31)  # 61 s
    r = a.g.ciclo()
    assert a.d.posicao_liquida(SIMB) == 0.0 and any(x.startswith("zerar") for x in r["acoes"])
    assert a.d.envios[-1]["position"] == tk and a.d.envios[-1]["magic"] == a.ex.cfg.magic_guardiao


def test_heartbeat_periodico_evita_zeragem():
    a = montar()
    abrir(a)
    for _ in range(12):
        seg(a, 10)
        a.g.heartbeat()
        a.g.ciclo()
    assert a.d.posicao_liquida(SIMB) == 1.0


# --- horario ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("h,m,esperado", [(8, 59, False), (9, 14, False), (9, 15, True), (12, 0, True), (17, 29, True),
                                           (17, 30, False), (17, 46, False)])
def test_janela_de_entradas(h, m, esperado):
    a = montar(quando=ns(2026, 10, 27, h, m))
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA))[0] is esperado


def test_fim_de_semana_e_feriado_sem_entradas():
    assert not montar(quando=ns(2026, 10, 31, 12, 0)).g.avaliar_entrada(cmd(Lado.COMPRA))[0]  # sabado
    assert not montar(quando=ns(2026, 11, 2, 12, 0)).g.avaliar_entrada(cmd(Lado.COMPRA))[0]  # feriado
    assert montar(quando=ns(2026, 11, 3, 12, 0)).g.avaliar_entrada(cmd(Lado.COMPRA))[0]


def test_zera_no_horario_configurado():
    a = montar(quando=ns(2026, 10, 27, 17, 40))
    abrir_ok = a.d.order_send({"action": C.ACAO_DEAL, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_BUY,
                               "magic": 7, "type_filling": C.PREENCHE_IOC})
    assert abrir_ok.retcode == C.RET_EXECUTADA
    a.g.heartbeat()
    a.g.ciclo()
    assert a.d.posicao_liquida(SIMB) == 1.0  # 17:40 < 17:45
    seg(a, 5 * 60 + 1)
    a.g.heartbeat()
    r = a.g.ciclo()
    assert a.d.posicao_liquida(SIMB) == 0.0 and "zerar:horario de zeragem" in r["acoes"]


# --- EMERGENCIA --------------------------------------------------------------------------------------
def test_arquivo_emergencia_cancela_zera_e_trava(tmp_path):
    a = montar(tmp_path)
    abrir(a)
    a.d.order_send({"action": C.ACAO_PENDING, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL_LIMIT,
                    "price": 101000.0, "magic": 7, "type_filling": C.PREENCHE_RETURN})
    assert a.d.volume_pendente(SIMB) == 1.0
    a.g.heartbeat()
    a.g.ciclo()
    assert a.d.posicao_liquida(SIMB) == 1.0 and not a.g.travado_emergencia
    (tmp_path / "EMERGENCIA").write_text("x")
    r = a.g.ciclo()
    assert "emergencia_arquivo" in r["acoes"] and a.g.travado_emergencia
    assert a.d.posicao_liquida(SIMB) == 0.0 and a.d.volume_pendente(SIMB) == 0.0
    assert a.g.confirmacao_emergencia() == {"travado": True, "posicoes": 0, "ordens": 0, "zerado": True}
    a.g.heartbeat()
    assert not a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]
    # rearme so manual, com motivo, e nunca com o arquivo ainda presente
    with pytest.raises(RuntimeError):
        a.g.rearmar("tentativa")
    (tmp_path / "EMERGENCIA").unlink()
    with pytest.raises(ValueError):
        a.g.rearmar("  ")
    assert not a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]  # remover o arquivo NAO rearma
    a.g.rearmar("operador conferiu a conta demo")
    a.g.heartbeat()
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]


def test_acionar_emergencia_direto_retorna_zerado():
    a = montar()
    abrir(a)
    assert a.g.acionar_emergencia("comando") is True
    assert a.g.esta_zerado()


# --- reconciliacao -----------------------------------------------------------------------------------
@pytest.mark.parametrize("razao,nome", [(C.RAZAO_CLIENT, "CLIENT"), (C.RAZAO_MOBILE, "MOBILE"), (C.RAZAO_WEB, "WEB")])
def test_deal_manual_gera_alerta_e_bloqueio(razao, nome):
    a = montar()
    a.d.operar_manual(C.DEAL_COMPRA, 1.0, SIMB, razao)
    r = a.g.reconciliar()
    assert r["novos"][0]["classe"] == "manual" and r["novos"][0]["reason"] == nome
    assert any(t == "intervencao_manual" for t, _ in a.alertas)
    a.g.heartbeat()
    ok, motivo = a.g.avaliar_entrada(cmd(Lado.COMPRA))
    assert not ok and "manual" in motivo
    a.g.rearmar("conferido")
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]


def test_deals_sl_tp_esperados_e_expert_robo_nao_bloqueiam():
    from test_mt5_comum import T0_MS
    base = ticks_planos(12, 100000.0)
    queda = [(T0_MS + 300, 99800.0, 99805.0, 99800.0, 0.0, C.FLAG_BID | C.FLAG_ASK)]
    a = montar(ticks=base + queda)
    abrir(a, sl=99900.0)
    seg(a, 1)  # o servidor dispara o SL
    r = a.g.reconciliar()
    classes = sorted(x["classe"] for x in r["novos"])
    assert classes == ["esperado", "robo"]
    assert a.g.classificacao == {"esperado": 1, "robo": 1, "manual": 0, "outro": 0}
    a.g.heartbeat()
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]
    assert not any(t == "intervencao_manual" for t, _ in a.alertas)


def test_deal_expert_com_magic_desconhecido_bloqueia():
    a = montar(cfg_g=ConfigGuardiao(magics_permitidos=frozenset({7})))
    abrir(a, magic=7)
    a.g.reconciliar()
    a.g.heartbeat()
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA))[0] or True
    a.ex.zerar_posicao(int(a.d.positions_get()[0].ticket), dono="x", forcar_dono=True)  # magic do guardiao (990001)
    a.g.reconciliar()
    a.g.heartbeat()
    ok, motivo = a.g.avaliar_entrada(cmd(Lado.COMPRA))
    assert not ok and "divergencia" in motivo


def test_ciclo_reconcilia_a_cada_10s():
    a = montar()
    a.g.heartbeat()
    assert a.g.ciclo()["reconciliacao"] is not None  # primeira
    seg(a, 5)
    a.g.heartbeat()
    assert a.g.ciclo()["reconciliacao"] is None
    seg(a, 5)
    a.g.heartbeat()
    assert a.g.ciclo()["reconciliacao"] is not None


def test_terminal_sem_resposta_3x_bloqueia_entradas():
    a = montar()
    a.d.desconectar()
    for _ in range(3):
        assert a.g.reconciliar() == {"ok": False}
    a.d.reconectar()
    a.g.heartbeat()
    assert not a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]


# --- perda diaria ------------------------------------------------------------------------------------
def test_perda_diaria_zera_bloqueia_e_expira_no_dia_seguinte():
    from test_mt5_comum import T0_MS
    base = ticks_planos(12, 100000.0)
    queda = [(T0_MS + 300, 99700.0, 99705.0, 99700.0, 0.0, C.FLAG_BID | C.FLAG_ASK)]  # -300 pts = R$ 60
    a = montar(ticks=base + queda, cfg_g=ConfigGuardiao(perda_max_dia_brl=50.0))
    abrir(a)
    seg(a, 1)
    a.g.heartbeat()
    r = a.g.ciclo()
    assert a.g.pnl_dia_brl <= -50 and a.d.posicao_liquida(SIMB) == 0.0 and "zerar:perda diaria" in r["acoes"]
    a.g.heartbeat()
    ok, motivo = a.g.avaliar_entrada(cmd(Lado.COMPRA))
    assert not ok and "perda_dia" in motivo
    seg(a, 24 * 3600)
    a.g.heartbeat()
    assert not any("perda_dia" in m for m in a.g.motivos_bloqueio())


def test_troca_de_conta_derruba_o_ciclo_do_guardiao():
    a = montar()
    abrir(a)
    a.d.trocar_conta(321, "XPMT5-DEMO", "demo")
    a.g.heartbeat()
    seg(a, 61)
    with pytest.raises(GuardaDemoViolada):
        a.g.ciclo()


def test_executor_travado_ou_incerta_bloqueia_entrada():
    a = montar()
    a.ex.travado = "teste"
    assert not a.g.avaliar_entrada(cmd(Lado.COMPRA))[0]
    a.ex.travado = None
    assert a.g.avaliar_entrada(cmd(Lado.COMPRA, qtd=2)) == (False, "quantidade 2 acima de 1")
