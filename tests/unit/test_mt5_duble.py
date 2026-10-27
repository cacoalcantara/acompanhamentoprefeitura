"""Comportamento do DubleMT5 (dados sinteticos): netting, SL/TP no servidor, retcodes, ticks, livro."""
from __future__ import annotations

import datetime as dt

import pytest
from test_mt5_comum import LOGIN, OFFSET_S, SERVIDOR, SIMB, T0_MS, montar_duble, ticks_planos

from robowin.mt5 import constantes as C
from robowin.mt5.duble import Falha, criar_duble_padrao, gerar_ticks_sinteticos


def req(tipo, vol=1.0, **kw):
    r = {"action": C.ACAO_DEAL, "symbol": SIMB, "volume": vol, "type": tipo, "price": 0.0, "deviation": 20,
         "magic": 7, "type_filling": C.PREENCHE_IOC}
    r.update(kw)
    return r


def test_duble_e_rotulado():
    d, _ = montar_duble(ticks_planos())
    assert d.eh_duble is True
    assert "SINTETIC" in (type(d).__doc__ or "").upper() + (__import__("robowin.mt5.duble").mt5.duble.__doc__ or "").upper()


def test_netting_uma_posicao_por_simbolo_e_inversao():
    d, _ = montar_duble(ticks_planos())
    r1 = d.order_send(req(C.ORDEM_BUY))
    assert r1.retcode == C.RET_EXECUTADA
    r2 = d.order_send(req(C.ORDEM_BUY))  # mesmo lado: aumenta a mesma posicao (netting)
    assert r2.retcode == C.RET_EXECUTADA
    ps = d.positions_get(symbol=SIMB)
    assert len(ps) == 1 and ps[0].volume == 2.0
    r3 = d.order_send(req(C.ORDEM_SELL, 3.0))  # oposta maior: fecha 2 e inverte 1
    ps = d.positions_get(symbol=SIMB)
    assert len(ps) == 1 and ps[0].type == C.POS_VENDA and ps[0].volume == 1.0
    assert ps[0].ticket != r1.deal  # inversao gera nova posicao
    deal3 = d.history_deals_get(ticket=r3.deal)[0]
    assert deal3.entry == C.ENTRADA_INOUT


def test_ordem_oposta_reduz_e_zera():
    d, _ = montar_duble(ticks_planos())
    d.order_send(req(C.ORDEM_BUY, 2.0))
    d.order_send(req(C.ORDEM_SELL, 1.0))
    assert d.posicao_liquida(SIMB) == 1.0
    d.order_send(req(C.ORDEM_SELL, 1.0))
    assert d.positions_get(symbol=SIMB) == ()


def test_ticket_velho_rejeitado_ou_invertendo_conforme_configuracao():
    d, _ = montar_duble(ticks_planos())
    r = d.order_send(req(C.ORDEM_BUY))
    tk = d.positions_get()[0].ticket
    d.order_send(req(C.ORDEM_SELL, position=tk))
    # ticket velho: rejeitado como no terminal (10036)
    r2 = d.order_send(req(C.ORDEM_SELL, position=tk))
    assert r2.retcode == C.RET_POSICAO_FECHADA and d.posicao_liquida(SIMB) == 0.0
    assert r.retcode == C.RET_EXECUTADA
    # modo perigoso: ignora o ticket e INVERTE (usado para provar que o executor se protege sozinho)
    d2, _ = montar_duble(ticks_planos(), rejeita_ticket_velho=False)
    d2.order_send(req(C.ORDEM_BUY))
    tk2 = d2.positions_get()[0].ticket
    d2.order_send(req(C.ORDEM_SELL, position=tk2))
    d2.order_send(req(C.ORDEM_SELL, position=tk2))
    assert d2.posicao_liquida(SIMB) == -1.0


def test_sl_vive_no_servidor_e_dispara_com_gap():
    base = ticks_planos(10, 100000.0)  # t0-1000 .. t0-100
    gap = [(T0_MS + 500, 99800.0, 99805.0, 99800.0, 0.0, C.FLAG_BID | C.FLAG_ASK)]
    d, rel = montar_duble(base + gap)
    r = d.order_send(req(C.ORDEM_BUY, sl=99900.0, tp=100200.0))
    assert r.retcode == C.RET_EXECUTADA
    assert d.positions_get()[0].sl == 99900.0
    rel.avancar(1_000_000_000)  # o tick de gap passa a ser visivel; ninguem chamou nada do cliente
    assert d.positions_get(symbol=SIMB) == ()
    deals = d.history_deals_get(dt.datetime.fromtimestamp(0, dt.UTC), dt.datetime(2100, 1, 1, tzinfo=dt.UTC))
    saida = deals[-1]
    assert saida.reason == C.RAZAO_SL and saida.entry == C.ENTRADA_OUT
    assert saida.price == 99800.0  # pior preco da lacuna, nao o do stop
    assert saida.profit == pytest.approx((99800.0 - 100005.0) * 0.20)


def test_tp_dispara_no_preco_do_alvo():
    base = ticks_planos(10, 100000.0)
    sobe = [(T0_MS + 200, 100300.0, 100305.0, 100300.0, 0.0, C.FLAG_BID | C.FLAG_ASK)]
    d, rel = montar_duble(base + sobe)
    d.order_send(req(C.ORDEM_BUY, sl=99900.0, tp=100200.0))
    rel.avancar(500_000_000)
    deals = d.deals_todos
    assert deals[-1].reason == C.RAZAO_TP and deals[-1].price == 100200.0


def test_ordem_pendente_limitada_executa_ao_cruzar():
    base = ticks_planos(10, 100000.0)
    cai = [(T0_MS + 300, 99940.0, 99945.0, 99940.0, 0.0, C.FLAG_BID | C.FLAG_ASK)]
    d, rel = montar_duble(base + cai)
    r = d.order_send({"action": C.ACAO_PENDING, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_BUY_LIMIT,
                      "price": 99950.0, "magic": 7, "type_filling": C.PREENCHE_RETURN})
    assert r.retcode == C.RET_COLOCADA and len(d.orders_get(symbol=SIMB)) == 1
    rel.avancar(1_000_000_000)
    assert d.orders_get(symbol=SIMB) == () and d.posicao_liquida(SIMB) == 1.0


@pytest.mark.parametrize("ret", [C.RET_TIMEOUT, C.RET_SEM_CONEXAO, C.RET_MUITAS_REQUISICOES, C.RET_ALGO_DESLIGADO_CLIENTE,
                                 C.RET_PREENCHIMENTO_INVALIDO])
def test_injecao_de_retcodes(ret):
    d, _ = montar_duble(ticks_planos())
    d.injetar_falhas(ret)
    r = d.order_send(req(C.ORDEM_BUY))
    assert r.retcode == ret and d.posicao_liquida(SIMB) == 0.0


def test_injecao_none_e_executa_mesmo_assim():
    d, _ = montar_duble(ticks_planos())
    d.injetar_falhas(None, Falha(C.RET_TIMEOUT, executa=True))
    assert d.order_send(req(C.ORDEM_BUY)) is None and d.posicao_liquida(SIMB) == 0.0
    r = d.order_send(req(C.ORDEM_BUY))  # executou no servidor mas a resposta se perdeu
    assert r.retcode == C.RET_TIMEOUT and d.posicao_liquida(SIMB) == 1.0


def test_execucao_parcial_10010():
    d, _ = montar_duble(ticks_planos())
    d.injetar_falhas(Falha(C.RET_PARCIAL, volume=1.0))
    r = d.order_send(req(C.ORDEM_BUY, 3.0))
    assert r.retcode == C.RET_PARCIAL and r.volume == 1.0 and d.posicao_liquida(SIMB) == 1.0


def test_ordem_de_mercado_10009_e_pendente_10008():
    d, _ = montar_duble(ticks_planos())
    assert d.order_send(req(C.ORDEM_BUY)).retcode == C.RET_EXECUTADA
    r = d.order_send({"action": C.ACAO_PENDING, "symbol": SIMB, "volume": 1.0, "type": C.ORDEM_SELL_LIMIT,
                      "price": 100500.0, "magic": 7, "type_filling": C.PREENCHE_RETURN})
    assert r.retcode == C.RET_COLOCADA


def test_validacoes_de_volume_e_preenchimento():
    d, _ = montar_duble(ticks_planos())
    assert d.order_send(req(C.ORDEM_BUY, 0.5)).retcode == C.RET_VOLUME_INVALIDO
    d.especificacoes[SIMB].filling_mode = C.SIMB_FILL_FOK
    assert d.order_send(req(C.ORDEM_BUY, type_filling=C.PREENCHE_IOC)).retcode == C.RET_PREENCHIMENTO_INVALIDO
    assert d.order_check(req(C.ORDEM_BUY, type_filling=C.PREENCHE_FOK)).retcode == 0
    assert len(d.envios) == 2 and len(d.checks) == 1


def test_troca_de_conta_no_meio_da_sessao():
    d, _ = montar_duble(ticks_planos())
    d.order_send(req(C.ORDEM_BUY))
    assert d.account_info().login == LOGIN and len(d.positions_get()) == 1
    d.trocar_conta(555, "XPMT5-REAL", "real")
    ai = d.account_info()
    assert ai.login == 555 and ai.trade_mode == C.CONTA_REAL and d.positions_get() == ()
    d.trocar_conta(LOGIN)
    assert len(d.positions_get()) == 1


@pytest.mark.parametrize("modo,cod", [("demo", 0), ("contest", 1), ("real", 2)])
def test_account_info_trade_mode(modo, cod):
    d, _ = montar_duble(ticks_planos(), trade_mode=modo)
    ai = d.account_info()
    assert ai.trade_mode == cod and ai.server == SERVIDOR and ai.margin_mode == C.MARGEM_EXCHANGE


def test_offset_nao_nulo_do_relogio_do_servidor():
    d, _ = montar_duble(ticks_planos())
    tk = d.symbol_info_tick(SIMB)
    # time_msc esta em hora de servidor (UTC + offset)
    assert OFFSET_S != 0 and tk.time_msc == T0_MS + OFFSET_S * 1000  # ultimo tick visivel esta em T0
    arr = d.copy_ticks_from(SIMB, dt.datetime.fromtimestamp((T0_MS + OFFSET_S * 1000) / 1000 - 5, dt.UTC), 1000, C.COPY_TICKS_ALL)
    assert len(arr) > 0 and arr["time_msc"][0] >= T0_MS + OFFSET_S * 1000 - 5000


def test_ticks_flags_buy_sell_e_filtro_trade():
    tk = gerar_ticks_sinteticos(T0_MS - 1000, 500, 10, semente=5, p_negocio=0.5)
    d, _ = montar_duble(tk)
    arr = d.copy_ticks_from(SIMB, dt.datetime.fromtimestamp((T0_MS + OFFSET_S * 1000) / 1000 - 2, dt.UTC), 10000, C.COPY_TICKS_ALL)
    fl = arr["flags"]
    assert ((fl & C.FLAG_BUY) > 0).any() and ((fl & C.FLAG_SELL) > 0).any()
    so_trade = d.copy_ticks_from(SIMB, dt.datetime.fromtimestamp((T0_MS + OFFSET_S * 1000) / 1000 - 2, dt.UTC), 10000, C.COPY_TICKS_TRADE)
    assert len(so_trade) < len(arr) and ((so_trade["flags"] & C.FLAG_LAST) > 0).all()


def test_backfill_apos_queda_e_futuro_invisivel():
    d, rel = montar_duble(ticks_planos(400))
    d.desconectar()
    assert d.copy_ticks_from(SIMB, dt.datetime.fromtimestamp(0, dt.UTC), 10, C.COPY_TICKS_ALL) is None
    assert d.last_error()[0] == C.ERR_SEM_CONEXAO[0]
    rel.avancar(5_000_000_000)
    d.reconectar()
    ini = dt.datetime.fromtimestamp((T0_MS + OFFSET_S * 1000) / 1000 - 1, dt.UTC)
    fim = dt.datetime.fromtimestamp((T0_MS + OFFSET_S * 1000) / 1000 + 4, dt.UTC)
    arr = d.copy_ticks_range(SIMB, ini, fim, C.COPY_TICKS_ALL)
    assert len(arr) >= 40  # ticks do periodo da queda ficaram disponiveis
    antes = d.copy_ticks_range(SIMB, ini, fim, C.COPY_TICKS_ALL)
    assert (antes["time_msc"] <= (T0_MS + 5000 + OFFSET_S * 1000)).all()


def test_livro_l2_agregado_por_preco_e_profundidade():
    d, _ = montar_duble(ticks_planos(400))
    assert d.market_book_get(SIMB) is None  # sem market_book_add
    assert d.market_book_add(SIMB) is True
    liv = d.market_book_get(SIMB)
    precos = [(b.type, b.price) for b in liv]
    assert len(precos) == len(set(precos))  # agregado: um nivel por preco
    assert {b.type for b in liv} == {C.BOOK_VENDA, C.BOOK_COMPRA}
    vendas = [b.price for b in liv if b.type == C.BOOK_VENDA]
    assert vendas == sorted(vendas, reverse=True)
    assert d.market_book_add("INEXISTENTE") is False


def test_symbols_get_e_symbol_info():
    d = criar_duble_padrao()
    d.initialize()
    nomes = {s.name for s in d.symbols_get()}
    assert nomes == {"WINZ26", "WING27", "INDZ26", "WDOX26", "BOVA11"}
    assert {s.name for s in d.symbols_get("WIN*")} == {"WINZ26", "WING27"}
    assert {s.name for s in d.symbols_get("*,!WIN*")} == {"INDZ26", "WDOX26", "BOVA11"}
    si = d.symbol_info("WINZ26")
    assert si.trade_tick_size == 5.0 and si.trade_tick_value / si.trade_tick_size == pytest.approx(0.20)
    assert si.volume_min == 1.0 and si.trade_exemode == C.EXEC_EXCHANGE and si.ticks_bookdepth > 0
    assert si.filling_mode & C.SIMB_FILL_IOC
    assert d.symbol_info("NADA") is None


def test_operar_manual_gera_deal_client():
    d, _ = montar_duble(ticks_planos())
    d.operar_manual(C.DEAL_COMPRA, 1.0, SIMB)
    assert d.deals_todos[-1].reason == C.RAZAO_CLIENT and d.posicao_liquida(SIMB) == 1.0


def test_sem_initialize_nada_responde():
    from robowin.core.relogio import RelogioSimulado
    from robowin.mt5.duble import DubleMT5
    d = DubleMT5(RelogioSimulado(0))
    assert d.account_info() is None
    assert SERVIDOR  # constante usada
