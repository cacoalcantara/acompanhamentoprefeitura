import datetime as dt

import numpy as np
import pytest
from perfis_teste import WIN

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import ComandoOrdem, Lado, LivroSnapshot, NivelBook, Tick
from robowin.execucao.custos import Custos, TarifaB3, tarifa_em
from robowin.execucao.preenchimento import FilaLimitada, ParamsSlippage, slippage_pontos
from robowin.execucao.simulador import SimuladorExecucao

T0 = dt_utc_ns(dt.datetime(2026, 11, 10, 10, 0))
MS = 1_000_000
MAGIC = 7


def tick(dt_ms, bid, ask, last=None, vol=1.0):
    t = T0 + dt_ms * MS
    return Tick(t_servidor_ms=t // MS, t_utc_ns=t, t_recebido_ns=t, bid=bid, ask=ask,
                last=last if last is not None else bid, volume=vol)


def sim(latencia_ms=0, seed=1, custos=None, **kw):
    rel = RelogioSimulado(T0)
    s = SimuladorExecucao(WIN.instrumento("WINZ26"), rel, custos=custos, latencia_ns=latencia_ms * MS,
                          seed=seed, **kw)
    s.registrar_braco("b1", MAGIC)
    return s, rel


def cmd(lado=Lado.COMPRA, tipo="mercado", q=1, preco=None, sl=None, tp=None, ticket=None, iid="i"):
    return ComandoOrdem(intencao_id=iid, magic=MAGIC, lado=lado, tipo=tipo, quantidade=q, preco=preco, sl=sl, tp=tp,
                        position_ticket=ticket)


def abrir_longa(s, rel, preco_ask=130010.0, sl=None, tp=None):
    s.on_tick(tick(-1000, 130000.0, preco_ask))
    ack = s.enviar(cmd(sl=sl, tp=tp))
    assert ack.retcode == 10008
    ev = s.on_tick(tick(10, 130000.0, preco_ask))
    assert s.posicao("b1").quantidade == 1
    return ev


# ---- mercado + latencia ---------------------------------------------------------------------------------
def test_mercado_ponta_contraria_em_t_mais_latencia():
    s, rel = sim(latencia_ms=200)
    s.on_tick(tick(-500, 129990.0, 130000.0))
    s.enviar(cmd(Lado.COMPRA))
    assert s.on_tick(tick(100, 129995.0, 130005.0)) == []  # antes de t+200ms: nada
    ev = s.on_tick(tick(300, 130095.0, 130100.0))
    ex = [e.execucao for e in ev if e.execucao][0]
    assert ex.preco == 130005.0  # ask vigente em t+200ms (tick de 100ms), nao o do tick posterior
    assert ex.t_ns == T0 + 200 * MS
    ev2 = s.on_tick(tick(400, 130000.0, 130010.0))
    rel.avancar_para(T0 + 450 * MS)
    s.enviar(cmd(Lado.VENDA, tipo="saida", ticket=s.posicao("b1").ticket))
    ev3 = s.on_tick(tick(500, 130000.0, 130010.0))
    ev3 += s.on_tick(tick(900, 130010.0, 130020.0))
    assert ev2 == [] and [e.execucao.preco for e in ev3 if e.execucao] == [130000.0]


def test_venda_mercado_executa_no_bid():
    s, _ = sim()
    s.on_tick(tick(-10, 130000.0, 130005.0))
    s.enviar(cmd(Lado.VENDA))
    s.on_tick(tick(1, 130000.0, 130005.0))
    p = s.posicao("b1")
    assert p.lado == Lado.VENDA and p.preco_medio == 130000.0


# ---- limitada ----------------------------------------------------------------------------------------------
def test_limitada_so_executa_quando_negocio_atravessa():
    s, _ = sim(fila_padrao=10.0)
    s.on_tick(tick(-10, 130000.0, 130005.0))
    s.enviar(cmd(tipo="limitada", preco=129995.0))
    s.on_tick(tick(1, 130000.0, 130005.0))  # ativa
    for k, (last, vol) in enumerate([(130005.0, 50), (130000.0, 50)]):  # negocios acima do preco: nada
        assert s.on_tick(tick(10 + k, 130000.0, 130005.0, last=last, vol=vol)) == []
    assert s.posicao("b1").quantidade == 0
    ev = s.on_tick(tick(100, 129985.0, 129990.0, last=129990.0, vol=1))  # atravessou (last < preco)
    assert [e.execucao.preco for e in ev if e.execucao] == [129995.0] and s.posicao("b1").quantidade == 1


def test_limitada_no_preco_exige_volume_acima_da_fila():
    s, _ = sim(fila_padrao=10.0)
    s.on_tick(tick(-10, 130000.0, 130005.0))
    s.enviar(cmd(tipo="limitada", preco=129995.0))
    s.on_tick(tick(1, 130000.0, 130005.0))
    for k in range(2):  # 8 + 2 = 10 = fila: ainda nao supera
        assert s.on_tick(tick(10 + k, 129995.0, 130000.0, last=129995.0, vol=5.0)) == []
    assert s.posicao("b1").quantidade == 0
    ev = s.on_tick(tick(50, 129995.0, 130000.0, last=129995.0, vol=3.0))  # excedente de 3 > 0
    assert any(e.execucao for e in ev) and s.posicao("b1").quantidade == 1


def test_limitada_usa_fila_do_livro():
    s, _ = sim(fila_padrao=0.0)
    s.on_livro(LivroSnapshot(t_utc_ns=T0, niveis=(NivelBook(preco=129995.0, volume=100.0, lado="compra"),)))
    s.on_tick(tick(-10, 130000.0, 130005.0))
    s.enviar(cmd(tipo="limitada", preco=129995.0))
    s.on_tick(tick(1, 130000.0, 130005.0))
    assert s.on_tick(tick(10, 129995.0, 130000.0, last=129995.0, vol=60.0)) == []
    assert s.on_tick(tick(20, 129995.0, 130000.0, last=129995.0, vol=60.0)) != []  # 120 > 100


def test_limitada_que_cruza_o_spread_executa_como_agressora():
    s, _ = sim()
    s.on_tick(tick(-10, 130000.0, 130005.0))
    s.enviar(cmd(tipo="limitada", preco=130010.0))
    ev = s.on_tick(tick(1, 130000.0, 130005.0))
    assert [e.execucao.preco for e in ev if e.execucao] == [130005.0]  # sem melhora alem da cotacao


def test_fila_limitada_unidade():
    f = FilaLimitada(Lado.VENDA, 100.0, restante=2, fila_a_frente=5.0)
    assert f.processar_negocio(99.0, 100) == 0  # abaixo do preco de venda: nao atravessa
    assert f.processar_negocio(100.0, 4.0) == 0 and f.fila_a_frente == 1.0
    assert f.processar_negocio(100.0, 2.0) == 1  # sobra 1
    assert f.processar_negocio(101.0, 1) == 1 and f.restante == 0


def test_preco_limitado_fora_do_grid_e_saida_sem_posicao():
    s, _ = sim()
    assert s.enviar(cmd(tipo="limitada", preco=129997.0)).retcode == 10015
    assert s.enviar(cmd(Lado.VENDA, tipo="saida")).retcode == 10036
    assert s.enviar(ComandoOrdem(intencao_id="x", magic=999, lado=Lado.COMPRA, tipo="mercado")).retcode == 10013
    s.algotrading = False
    assert s.enviar(cmd()).retcode == 10027


# ---- stop ------------------------------------------------------------------------------------------------------
def test_stop_dispara_pelo_ultimo_negocio_e_executa_no_proximo_preco_com_slippage():
    s, rel = sim(seed=3)
    abrir_longa(s, rel, 130010.0, sl=129900.0)
    assert s.on_tick(tick(100, 129905.0, 129910.0, last=129905.0)) == []  # bid/ask nao disparam, so negocio
    assert s.on_tick(tick(200, 129890.0, 129900.0, last=129900.0)) == []  # disparo (last <= sl); ainda sem fill
    ev = s.on_tick(tick(300, 129880.0, 129890.0, last=129885.0))  # proximo preco: bid 129880
    tr = [e.trade for e in ev if e.trade][0]
    assert tr.saida <= 129880.0 - 5.0  # pior entre stop e bid, menos >= 1 tick de slippage
    assert tr.saida % 5 == 0 and tr.motivo_saida == "stop"
    assert s.posicao("b1").quantidade == 0 and s.carteira("b1").slippage_pontos_total >= 5.0


def test_stop_em_gap_aplica_pior_preco():
    s, rel = sim(seed=3)
    abrir_longa(s, rel, 130010.0, sl=129900.0)
    s.on_tick(tick(100, 129000.0, 129005.0, last=129000.0))  # gap de 900 pts, muito alem do stop
    ev = s.on_tick(tick(200, 128990.0, 128995.0, last=128995.0))
    tr = [e.trade for e in ev if e.trade][0]
    assert tr.saida <= 128990.0 - 5.0 and tr.saida < 129900.0 - 900.0
    assert tr.bruto_brl < -0.2 * (130010.0 - 129900.0) * 3  # perda muito maior que a do stop


def test_stop_de_venda_e_simetrico():
    s, rel = sim(seed=3)
    s.on_tick(tick(-100, 130000.0, 130005.0))
    s.enviar(cmd(Lado.VENDA, sl=130100.0))
    s.on_tick(tick(1, 130000.0, 130005.0))
    s.on_tick(tick(50, 130100.0, 130105.0, last=130105.0))
    ev = s.on_tick(tick(60, 130110.0, 130115.0, last=130110.0))
    tr = [e.trade for e in ev if e.trade][0]
    assert tr.saida >= 130115.0 + 5.0 and tr.bruto_brl < 0


def test_slippage_de_cauda_pesada_e_reprodutivel_por_seed():
    p = ParamsSlippage()
    a = [slippage_pontos(np.random.default_rng(5), p, 5.0) for _ in range(1)]
    rng = np.random.default_rng(5)
    amostra = np.array([slippage_pontos(rng, p, 5.0) for _ in range(20000)])
    assert amostra.min() >= 5.0 and amostra.max() >= 100.0  # cauda: ate dezenas de ticks
    assert np.quantile(amostra, 0.99) > 3 * np.median(amostra)
    rng2 = np.random.default_rng(5)
    assert [slippage_pontos(rng2, p, 5.0)] == a
    est = np.array([slippage_pontos(np.random.default_rng(i), ParamsSlippage.estresse(), 5.0) for i in range(500)])
    assert est.min() >= 25.0  # estresse: >= 5 ticks


def test_mesma_seed_mesmas_execucoes():
    def rodar(seed):
        s, rel = sim(seed=seed)
        abrir_longa(s, rel, 130010.0, sl=129900.0)
        s.on_tick(tick(100, 129890.0, 129895.0, last=129890.0))
        ev = s.on_tick(tick(200, 129890.0, 129895.0, last=129890.0))
        return [e.trade.saida for e in ev if e.trade][0]
    assert rodar(11) == rodar(11)


def test_alvo_como_limitada_so_ao_atravessar():
    s, rel = sim(fila_padrao=0.0)
    abrir_longa(s, rel, 130010.0, tp=130100.0)
    assert s.on_tick(tick(100, 130095.0, 130100.0, last=130095.0)) == []
    ev = s.on_tick(tick(200, 130105.0, 130110.0, last=130105.0))  # last > tp: atravessou
    tr = [e.trade for e in ev if e.trade][0]
    assert tr.saida == 130100.0 and tr.motivo_saida == "alvo" and tr.bruto_brl == pytest.approx(0.2 * 90)


def test_saida_dupla_nao_inverte_posicao():
    s, rel = sim()
    abrir_longa(s, rel, 130010.0, sl=129900.0)
    tk = s.posicao("b1").ticket
    a1 = s.enviar(cmd(Lado.VENDA, tipo="saida", ticket=tk, iid="s1"))
    a2 = s.enviar(cmd(Lado.VENDA, tipo="saida", ticket=tk, iid="s2"))  # segunda saida (ex.: guardiao)
    assert a1.retcode == a2.retcode == 10008
    ev = s.on_tick(tick(100, 130000.0, 130010.0))
    ev += s.on_tick(tick(110, 130000.0, 130010.0))
    assert s.posicao("b1").quantidade == 0 and s.posicao("b1").lado is None  # nunca ficou vendido
    assert len(s.carteira("b1").trades) == 1
    assert any(e.tipo == "cancelada" and e.motivo == "posicao_fechada" for e in ev)  # a 2a saida cai
    assert s.enviar(cmd(Lado.VENDA, tipo="saida", ticket=tk)).retcode == 10036  # ticket velho


def test_modificar_stop_so_aperta():
    s, rel = sim()
    abrir_longa(s, rel, 130010.0, sl=129900.0)
    assert s.modificar_stop("b1", 129950.0)
    assert not s.modificar_stop("b1", 129800.0)
    assert s.posicao("b1").stop == 129950.0


# ---- custos --------------------------------------------------------------------------------------------------------
def test_custos_tarifas_b3_e_corretagem():
    s, rel = sim()
    abrir_longa(s, rel, 130010.0)
    s.on_tick(tick(50, 130040.0, 130050.0))
    rel.avancar_para(T0 + 60 * MS)
    s.enviar(cmd(Lado.VENDA, tipo="saida", ticket=s.posicao("b1").ticket))
    ev = s.on_tick(tick(100, 130040.0, 130050.0))
    ev += s.on_tick(tick(110, 130040.0, 130050.0))
    tr = [e.trade for e in ev if e.trade][0]
    assert tr.tarifas_brl == pytest.approx(0.5) and tr.corretagem_brl == 0.0
    assert tr.bruto_brl == pytest.approx((130040.0 - 130010.0) * 0.2)
    assert tr.liquido_brl == pytest.approx(6.0 - 0.5)
    assert all(e.execucao.taxas_brl == pytest.approx(0.25) for e in ev if e.execucao)


def test_custos_estresse_com_corretagem_e_taxa_de_zeragem():
    tabela = (TarifaB3(vigente_desde="2025-01-01", tarifa_lado_brl=0.25, taxa_zeragem_brl=0.10),)
    s, rel = sim(custos=Custos(tabela=tabela, corretagem_ida_volta_brl=0.22))
    abrir_longa(s, rel, 130010.0)
    ack = s.enviar(cmd(Lado.VENDA, tipo="saida", ticket=s.posicao("b1").ticket))
    s.marcar_zeragem(ack.order)
    ev = s.on_tick(tick(100, 130000.0, 130010.0)) + s.on_tick(tick(110, 130000.0, 130010.0))
    tr = [e.trade for e in ev if e.trade][0]
    assert tr.corretagem_brl == pytest.approx(0.22) and tr.tarifas_brl == pytest.approx(0.25 + 0.25 + 0.10)
    assert tr.motivo_saida == "zeragem"


def test_tabela_de_tarifas_versionada_por_vigencia():
    import datetime
    TAB = WIN.tarifas
    assert tarifa_em(datetime.date(2026, 3, 1), TAB).vigente_desde == "2025-01-01"
    assert tarifa_em(datetime.date(2026, 11, 3), TAB).vigente_desde == "2026-11-03"
    with pytest.raises(ValueError):
        tarifa_em(datetime.date(2020, 1, 1), TAB)
    assert Custos.do_perfil(WIN).ida_e_volta(datetime.date(2026, 11, 10), 1) == pytest.approx(0.5)
    assert Custos.do_perfil(WIN).estresse().ida_e_volta(datetime.date(2026, 11, 10), 1) == pytest.approx(0.72)


def test_carteira_por_braco_independente():
    s, rel = sim()
    s.registrar_braco("b2", 8)
    s.on_tick(tick(-10, 130000.0, 130005.0))
    s.enviar(cmd(Lado.COMPRA))
    s.on_tick(tick(1, 130000.0, 130005.0))
    assert s.posicao("b1").quantidade == 1 and s.posicao("b2").quantidade == 0
