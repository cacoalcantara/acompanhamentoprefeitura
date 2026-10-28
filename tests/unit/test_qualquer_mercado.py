"""Qualquer mercado do MT4/MT5 (forex, metais, CFD de indice, cripto) ao lado da B3: perfis genericos, sessoes 24x5/24x7,
conversao de moeda NO INSTANTE (sem cotacao => entradas bloqueadas), custos de corretora (comissao por lote, swap so ao
atravessar o rollover), lote fracionario no risco, simulador por cotacao, importacao do perfil a partir do terminal
(dubles MT5 e MT4), features desligadas por capacidade. Tudo SINTETICO, sem rede e sem chave."""
from __future__ import annotations

import datetime as dt
import math
import tomllib
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from robowin.core.ids import novo_id
from robowin.core.instrumentos import carregar_perfil
from robowin.core.moedas import (
    ConversorHistorico,
    ConversorMoeda,
    FeedCotacoes,
    conversor_do_terminal,
    motivo_sem_cotacao,
    rota_conversao,
)
from robowin.core.relogio import RelogioSimulado
from robowin.core.sessoes import sessao_do_perfil, sessao_especifica
from robowin.core.tipos import Acao, ComandoOrdem, ConfigRisco, Intencao, Lado, Posicao, Tick
from robowin.execucao.custos import Custos
from robowin.execucao.simulador import SimuladorExecucao
from robowin.features.pipeline import PipelineFeatures
from robowin.features.registro import features_de_negocios, features_desligadas_instrumento
from robowin.mt5.duble import DubleMT5, criar_duble_padrao, especificacao_generica, gerar_ticks_sinteticos
from robowin.mt5.specs import EspecEsperada, validar_especificacoes
from robowin.perfil.importacao import ImportacaoRecusada, gerar_perfil, gravar_perfil, resolver_nome
from robowin.risco.motor import MotorRiscoPadrao
from robowin.risco.politica import unidades_maximas

NY = ZoneInfo("America/New_York")
MS = 1_000_000
ATIVOS = ("WIN", "WDO", "EURUSD", "USDJPY", "XAUUSD", "BTCUSD", "US500")
CORRETORA = ("EURUSD", "USDJPY", "XAUUSD", "BTCUSD", "US500")


def P(a: str):
    return carregar_perfil(a, permitir_a_confirmar=True)


def ny(*a: int) -> int:
    return int(dt.datetime(*a, tzinfo=NY).timestamp() * 1e9)


# ------------------------------------------------------------------------------------------------- perfis
@pytest.mark.parametrize("ativo", ATIVOS)
def test_perfis_carregam_e_derivam_unidade(ativo):
    p = P(ativo)
    assert p.ponto_valor_brl > 0 and p.tick_valor_brl > 0
    if ativo in CORRETORA:
        assert p.de_corretora and not p.tem_negocios and not p.tarifas and p.a_confirmar
        assert p.volume_unidade == p.volume_min < 1
        # valor do ponto por unidade = tick_valor_lucro / tick x lote minimo (moeda de lucro)
        assert p.ponto_valor_brl == pytest.approx(p.tick_valor_lucro / p.tick_pontos * p.volume_min)
        assert p.moeda == p.moeda_lucro and p.bolsa_perfil().regime_tributario == "nao_calculado"
    else:
        assert not p.de_corretora and p.tem_negocios and p.tarifas and p.volume_unidade == p.lote
        assert p.moeda == "BRL"


def test_digitos_e_pip_dos_pares():
    eur, jpy = P("EURUSD"), P("USDJPY")
    assert (eur.digitos, eur.ponto, eur.pip) == (5, 0.00001, 0.0001)
    assert (jpy.digitos, jpy.ponto, jpy.pip) == (3, 0.001, 0.01)
    assert jpy.moeda == "JPY" and eur.moeda == "USD"


def test_perfil_generico_incoerente_recusa():
    from robowin.core.instrumentos import PerfilInstrumento
    base = dict(simbolo_base="X", classe="forex", tick_pontos=0.00001, tick_valor_lucro=1.0, volume_min=0.01,
                volume_step=0.01, stop_max_ticks=10, slippage_folga_ticks=1, stop_padrao_ticks=5, alvo_padrao_ticks=5,
                preco_sintetico_ref=1.0)
    assert PerfilInstrumento(**base).ponto_valor_brl == pytest.approx(1000.0)
    with pytest.raises(ValueError):
        PerfilInstrumento(**{**base, "ponto_valor_brl": 7.0})  # incoerente com o tick value
    with pytest.raises(ValueError):
        PerfilInstrumento(**{**base, "volume_min": 0.015})  # nao multiplo do passo
    with pytest.raises(ValueError):
        PerfilInstrumento(**{**base, "classe": "futuro"})  # classe de bolsa exige tarifas


# ------------------------------------------------------------------------------------------------- sessoes
def test_forex_24x5_abre_domingo_fecha_sexta_com_pausa_e_feriado():
    s = sessao_do_perfil(P("EURUSD"))
    assert s.aberto(ny(2026, 11, 8, 16, 59))[0] is False  # domingo antes da abertura
    assert s.aberto(ny(2026, 11, 8, 17, 6)) == (True, "")  # domingo depois da pausa de rollover
    assert s.aberto(ny(2026, 11, 10, 17, 0)) == (False, "pausa")  # pausa diaria 16:55-17:05
    assert s.aberto(ny(2026, 11, 13, 17, 0)) == (False, "fim_de_semana")  # sexta 17:00
    assert s.aberto(ny(2026, 11, 14, 12, 0)) == (False, "fim_de_semana")  # sabado
    assert s.aberto(ny(2026, 12, 25, 10, 0)) == (False, "dia_fechado")
    # o dia de negociacao termina no rollover (17:00 NY)
    assert s.dia_negociacao(ny(2026, 11, 10, 18, 0)) == dt.date(2026, 11, 11)
    assert s.dia_negociacao(ny(2026, 11, 10, 16, 0)) == dt.date(2026, 11, 10)


def test_cripto_24x7_abre_no_sabado_e_b3_segue_o_legado():
    s = sessao_do_perfil(P("BTCUSD"))
    sab = int(dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    assert s.aberto(sab) == (True, "")
    assert s.estado(sab, "00:00", "23:59", "23:59").entradas
    assert sessao_especifica(P("WIN")) is None  # B3: o relogio legado de Sao Paulo, sem mudanca
    assert sessao_especifica(P("EURUSD")) is not None


def test_janela_do_risco_e_lida_no_fuso_da_sessao_e_para_antes_do_rollover():
    s = sessao_do_perfil(P("EURUSD"))
    assert s.estado(ny(2026, 11, 10, 10, 0), "09:15", "17:30", "17:45").entradas  # 10:00 NY
    assert not s.estado(ny(2026, 11, 10, 8, 0), "09:15", "17:30", "17:45").entradas  # antes das 09:15 NY
    e = s.estado(ny(2026, 11, 10, 12, 30), "09:15", "17:30", "17:45")
    assert not e.entradas and e.zerar  # zeragem da grade padrao do forex (12:00 NY)
    e = s.estado(ny(2026, 11, 10, 16, 45), "00:00", "23:00", "23:30")
    assert e.motivo == "proximo_do_fechamento" and e.zerar  # 10 min antes da pausa de rollover


def test_rollovers_e_swap_triplo():
    s = sessao_do_perfil(P("EURUSD"))
    # de terca 16:00 a quinta 16:00 NY: rollovers de terca (x1) e quarta (x3)
    r = s.rollovers_entre(ny(2026, 11, 10, 16, 0), ny(2026, 11, 12, 16, 0), "quarta")
    assert [m for _, m in r] == [1, 3]
    assert s.rollovers_entre(ny(2026, 11, 10, 9, 0), ny(2026, 11, 10, 12, 0)) == []  # day trade: nenhum


# ------------------------------------------------------------------------------------------------- moedas
def test_rotas_direta_inversa_intermediaria_e_sufixo():
    nomes = ["EURUSD.m", "USDJPY.m", "USDBRL.m", "EURUSDX"]
    assert rota_conversao("USD", "BRL", nomes).descrever() == "USD->BRL via USDBRL.m"
    assert rota_conversao("BRL", "USD", nomes).descrever() == "BRL->USD via 1/USDBRL.m"
    assert rota_conversao("JPY", "BRL", nomes).descrever() == "JPY->BRL via 1/USDJPY.m x USDBRL.m"
    assert rota_conversao("USD", "USD", nomes).pernas == ()
    assert rota_conversao("CHF", "BRL", nomes) is None


def test_conversor_recusa_cotacao_ausente_invalida_ou_velha():
    feed = FeedCotacoes()
    c = ConversorMoeda(rota_conversao("USD", "BRL", ["USDBRL"]), feed, idade_max_s=60)
    assert c.fator(1_000) is None and "sem cotacao" in c.ultimo_motivo
    feed.atualizar("USDBRL", 5.0, 4.9, 1_000)
    assert c.fator(1_000) is None and "invalida" in c.ultimo_motivo
    feed.atualizar("USDBRL", 5.39, 5.41, 1_000)
    assert c.fator(1_000) == pytest.approx(5.40)
    assert c.fator(1_000 + 61_000) is None and "velha" in c.ultimo_motivo


def test_conversor_historico_nao_olha_o_futuro():
    rota = rota_conversao("USD", "BRL", ["USDBRL"])
    h = ConversorHistorico(rota, {"USDBRL": [(1_000 * MS, 5.0, 5.0), (2_000 * MS, 6.0, 6.0)]})
    assert h(999 * MS) is None
    assert h(1_500 * MS) == pytest.approx(5.0)  # ainda a cotacao de t=1000 (a de t=2000 e futuro)
    assert h(2_000 * MS) == pytest.approx(6.0)


@pytest.mark.parametrize("moeda_conta", ["USD", "BRL"])
def test_conversor_do_terminal_no_duble(moeda_conta):
    d = criar_duble_padrao(moeda_conta=moeda_conta)
    d.initialize()
    conv = conversor_do_terminal(d, "JPY", moeda_conta)
    f = conv.fator()
    esperado = 1 / 150.0 if moeda_conta == "USD" else 5.40 / 150.0
    assert f == pytest.approx(esperado, rel=0.01)


# ------------------------------------------------------------------------------------------------- custos
def test_custos_forex_sem_tarifa_de_bolsa_com_comissao_por_lote_e_swap():
    p = P("EURUSD").model_copy(update={"comissao_lote_lado": 3.5, "swap_long": -6.5, "swap_short": 1.2,
                                       "swap_modo": "pontos"})
    c = Custos.do_perfil(p, corretagem_ida_volta_brl=0.0)
    lado = c.lado(dt.date(2026, 11, 10), 2)  # 2 unidades = 0,02 lote
    assert lado.tarifas_brl == 0.0 and lado.corretagem_brl == pytest.approx(3.5 * 0.02)
    assert c.swap_custo(Lado.COMPRA, 1, 0) == 0.0
    # 1 noite comprado: -6,5 pontos x USD 1 por ponto por lote x 0,01 lote => paga USD 0,065
    assert c.swap_custo(Lado.COMPRA, 1, 1) == pytest.approx(0.065)
    assert c.swap_custo(Lado.VENDA, 1, 3) == pytest.approx(-1.2 * 0.01 * 3)  # recebe (triplo)
    sem_swap = Custos.do_perfil(P("EURUSD"))
    assert sem_swap.swap_custo(Lado.COMPRA, 1, 1) is None  # atravessou e o swap e desconhecido


@pytest.mark.parametrize("ativo", ATIVOS)
def test_custo_lado_unidade_por_mercado(ativo):
    p = P(ativo)
    c = p.custo_lado_unidade(dt.date(2026, 11, 10))
    assert c == (0.0 if p.de_corretora else p.tarifa_em(dt.date(2026, 11, 10)).tarifa_lado_brl)


# ------------------------------------------------------------------------------------------------- simulador
def _tick(t_ns, bid, ask):
    return Tick(t_servidor_ms=t_ns // MS, t_utc_ns=t_ns, t_recebido_ns=t_ns, bid=bid, ask=ask, last=0.0, volume=0.0)


def _cmd(lado=Lado.COMPRA, tipo="mercado", sl=None, ticket=None):
    return ComandoOrdem(intencao_id="i", magic=7, lado=lado, tipo=tipo, quantidade=1, sl=sl, position_ticket=ticket)


@pytest.mark.parametrize("moeda_conta,fator", [("USD", 1.0), ("BRL", 5.40)])
def test_simulador_eurusd_pnl_na_moeda_da_conta(moeda_conta, fator):
    p = P("EURUSD")
    t0 = ny(2026, 11, 10, 10, 0)
    rel = RelogioSimulado(t0)
    ins = p.instrumento("EURUSD", moeda_conta=moeda_conta)
    s = SimuladorExecucao(ins, rel, custos=Custos.do_perfil(p, corretagem_ida_volta_brl=0.0),
                          conversor=lambda _t: fator)
    s.registrar_braco("b1", 7)
    s.on_tick(_tick(t0 - MS, 1.10000, 1.10010))
    assert s.enviar(_cmd()).retcode == 10008
    s.on_tick(_tick(t0 + MS, 1.10000, 1.10010))
    assert s.posicao("b1").quantidade == 1 and s.posicao("b1").preco_medio == 1.10010
    rel.avancar_para(t0 + 60_000 * MS)
    s.on_tick(_tick(t0 + 60_000 * MS, 1.10110, 1.10120))
    s.enviar(_cmd(Lado.VENDA, "saida", ticket=s.posicao("b1").ticket))
    s.on_tick(_tick(t0 + 60_001 * MS, 1.10110, 1.10120))
    tr = s.carteira("b1").trades[-1]
    # 10 pips x USD 1/pip... = 100 ticks x USD 0,01 por tick por 0,01 lote = USD 1,00 (x fator na moeda da conta)
    assert tr.bruto_brl == pytest.approx(1.0 * fator) and tr.swap_brl == 0.0 and tr.tarifas_brl == 0.0


def test_simulador_sem_cotacao_rejeita_entrada_mas_nunca_a_saida():
    p = P("EURUSD")
    t0 = ny(2026, 11, 10, 10, 0)
    rel = RelogioSimulado(t0)
    cot = {"f": 5.4}
    s = SimuladorExecucao(p.instrumento("EURUSD", moeda_conta="BRL"), rel, conversor=lambda _t: cot["f"])
    s.registrar_braco("b1", 7)
    s.on_tick(_tick(t0 - MS, 1.1, 1.1001))
    s.enviar(_cmd())
    s.on_tick(_tick(t0 + MS, 1.1, 1.1001))
    cot["f"] = None
    ack = s.enviar(_cmd())
    assert ack.retcode != 10008 and ack.comentario == motivo_sem_cotacao("USD", "BRL")
    assert s.enviar(_cmd(Lado.VENDA, "saida", ticket=s.posicao("b1").ticket)).retcode == 10008  # saida segue
    sem_conv = SimuladorExecucao(p.instrumento("EURUSD", moeda_conta="BRL"), rel)
    sem_conv.registrar_braco("b1", 7)
    assert sem_conv.enviar(_cmd()).retcode != 10008


def test_simulador_stop_por_cotacao_sem_negocios():
    p = P("EURUSD")
    t0 = ny(2026, 11, 10, 10, 0)
    rel = RelogioSimulado(t0)
    s = SimuladorExecucao(p.instrumento("EURUSD"), rel)
    s.registrar_braco("b1", 7)
    s.on_tick(_tick(t0 - MS, 1.1, 1.1001))
    s.enviar(_cmd(sl=1.0995))
    s.on_tick(_tick(t0 + MS, 1.1, 1.1001))
    assert s.posicao("b1").quantidade == 1
    s.on_tick(_tick(t0 + 2 * MS, 1.0994, 1.0995))  # bid cruzou o stop (nao ha negocio/last)
    s.on_tick(_tick(t0 + 3 * MS, 1.0994, 1.0995))
    assert s.posicao("b1").quantidade == 0 and s.carteira("b1").trades[-1].motivo_saida


def test_simulador_swap_ao_atravessar_o_rollover():
    p = P("EURUSD").model_copy(update={"swap_long": -6.5, "swap_short": 1.2, "swap_modo": "pontos"})
    t0 = ny(2026, 11, 10, 16, 0)
    rel = RelogioSimulado(t0)
    s = SimuladorExecucao(p.instrumento("EURUSD"), rel, custos=Custos.do_perfil(p, corretagem_ida_volta_brl=0.0),
                          sessao=sessao_do_perfil(p))
    s.registrar_braco("b1", 7)
    s.on_tick(_tick(t0 - MS, 1.1, 1.1001))
    s.enviar(_cmd())
    s.on_tick(_tick(t0 + MS, 1.1, 1.1001))
    t1 = ny(2026, 11, 11, 10, 0)  # atravessou 1 rollover (terca 17:00)
    rel.avancar_para(t1)
    s.on_tick(_tick(t1, 1.1, 1.1001))
    s.enviar(_cmd(Lado.VENDA, "saida", ticket=s.posicao("b1").ticket))
    s.on_tick(_tick(t1 + MS, 1.1, 1.1001))
    assert s.carteira("b1").trades[-1].swap_brl == pytest.approx(0.065)


# ------------------------------------------------------------------------------------------------- risco
def _intencao(t, qtd=1, stop=0.0010, acao=Acao.COMPRAR, braco="b1", preco=1.1):
    return Intencao(id=novo_id("int"), braco=braco, acao=acao, quantidade=qtd, stop_pontos=stop,
                    validade_ns=t + int(60e9), preco_referencia=preco)


def _cfg(**kw):
    base = dict(stop_max_pontos=0.0050, slippage_folga_pontos=0.0002, perda_max_operacao_brl=60.0,
                entradas_de="09:15", entradas_ate="11:30", zeragem="12:00")
    return ConfigRisco(**{**base, **kw})


def _motor(ativo="EURUSD", moeda_conta="", conversor=None, cfg=None):
    p = P(ativo)
    return MotorRiscoPadrao(cfg or _cfg(), p.instrumento(ativo, moeda_conta=moeda_conta), conversor=conversor)


def test_risco_eurusd_conta_brl_sem_cotacao_bloqueia_entrada():
    t = ny(2026, 11, 10, 10, 0)
    d = _motor(moeda_conta="BRL").avaliar(_intencao(t), Posicao(simbolo="EURUSD"), t)
    assert not d.aprovada and motivo_sem_cotacao("USD", "BRL") in d.motivos
    d = _motor(moeda_conta="BRL", conversor=lambda _t: 5.4).avaliar(_intencao(t), Posicao(simbolo="EURUSD"), t)
    assert d.aprovada, d.motivos
    d = _motor(moeda_conta="USD").avaliar(_intencao(t), Posicao(simbolo="EURUSD"), t)
    assert d.aprovada, d.motivos


def test_risco_perda_por_operacao_convertida_pela_cotacao():
    t = ny(2026, 11, 10, 10, 0)
    # stop 0,0012 + folga 0,0002 = 14 pips x USD 0,10/pip (0,01 lote) = USD 1,40; em BRL a 5,40 = 7,56 > 5
    cfg = _cfg(perda_max_operacao_brl=5.0)
    usd = _motor(moeda_conta="USD", cfg=cfg).avaliar(_intencao(t, stop=0.0012), Posicao(simbolo="EURUSD"), t)
    brl = _motor(moeda_conta="BRL", conversor=lambda _t: 5.4, cfg=cfg).avaliar(
        _intencao(t, stop=0.0012), Posicao(simbolo="EURUSD"), t)
    assert usd.aprovada and "perda_por_operacao_acima_do_maximo" in brl.motivos


def test_risco_janela_no_fuso_da_sessao_e_fim_de_semana():
    m = _motor(moeda_conta="USD")
    sab = ny(2026, 11, 14, 10, 0)
    assert "fora_do_pregao" in m.avaliar(_intencao(sab), Posicao(simbolo="EURUSD"), sab).motivos
    cedo = ny(2026, 11, 10, 8, 0)
    assert "fora_da_janela_de_entradas" in m.avaliar(_intencao(cedo), Posicao(simbolo="EURUSD"), cedo).motivos
    pos = Posicao(simbolo="EURUSD", lado=Lado.COMPRA, quantidade=1, preco_medio=1.1, ticket=1, stop=1.09)
    assert m.precisa_zerar(pos, ny(2026, 11, 10, 12, 5)) == (True, "zeragem")


def test_unidades_maximas_lote_fracionario_e_win_um_contrato():
    assert unidades_maximas(ConfigRisco(), 1.0) == 1  # WIN: 1 contrato
    assert unidades_maximas(ConfigRisco(volume_max_lotes=0.05), 0.01) == 5
    assert unidades_maximas(ConfigRisco(volume_max_lotes=1.0), 0.01) == 100  # teto da v1: 1,0 lote
    from robowin.core.config import ErroConfig
    from robowin.risco.politica import validar_coerencia
    with pytest.raises(ErroConfig, match="volume_max_lotes"):
        validar_coerencia(ConfigRisco(volume_max_lotes=2.0))


@settings(max_examples=150, deadline=None)
@given(qtd=st.integers(min_value=1, max_value=12), lotes=st.sampled_from([0.01, 0.03, 0.05, 0.1]),
       pos_qtd=st.integers(min_value=0, max_value=6), mesmo_lado=st.booleans(),
       stop=st.one_of(st.none(), st.just(0.0), st.floats(min_value=0.0001, max_value=0.004)),
       ultima_perda=st.booleans(), ultimo=st.integers(min_value=0, max_value=6))
def test_propriedade_invariantes_com_lote_fracionario(qtd, lotes, pos_qtd, mesmo_lado, stop, ultima_perda, ultimo):
    t = ny(2026, 11, 10, 10, 0)
    m = _motor(moeda_conta="USD", cfg=_cfg(volume_max_lotes=lotes, perda_max_operacao_brl=1e6,
                                           perda_max_dia_brl=1e7, perda_max_semana_brl=1e7, dd_alerta_brl=1e7,
                                           dd_max_brl=1e7))
    umax = m.unidades_max
    assert umax == round(lotes / 0.01)
    if ultimo > 0:
        e = m.contab.estado("b1", t)
        e.ultima_foi_perda, e.ultimo_tamanho = ultima_perda, ultimo
    lado_pos = Lado.COMPRA if mesmo_lado else Lado.VENDA
    pos = (Posicao(simbolo="EURUSD", lado=lado_pos, quantidade=pos_qtd, preco_medio=1.1, ticket=1, stop=1.09)
           if pos_qtd else Posicao(simbolo="EURUSD"))
    d = m.avaliar(_intencao(t, qtd=qtd, stop=stop), pos, t)
    if d.aprovada:
        assert stop is not None and stop > 0  # stop obrigatorio
        assert pos_qtd == 0  # uma posicao por simbolo, sem piramidacao
        assert qtd <= umax and qtd * 0.01 <= lotes + 1e-9  # volume dentro do limite em lotes
        assert not (ultima_perda and ultimo > 0 and qtd > ultimo)  # sem martingale


# ------------------------------------------------------------------------------------------------- specs e duble
@pytest.mark.parametrize("ativo", CORRETORA)
@pytest.mark.parametrize("moeda_conta", ["USD", "BRL"])
def test_specs_do_duble_conferem_com_o_perfil_em_conta_usd_e_brl(ativo, moeda_conta):
    d = criar_duble_padrao(moeda_conta=moeda_conta)
    d.initialize()
    p = P(ativo)
    info = d.symbol_info(ativo)
    assert validar_especificacoes(info, EspecEsperada.do_perfil(p), moeda_conta=moeda_conta) == []
    info.trade_contract_size *= 10  # contrato divergente => recusa
    assert validar_especificacoes(info, EspecEsperada.do_perfil(p), moeda_conta=moeda_conta)


def test_duble_mercados_sao_so_cotacoes_e_digitos_corretos():
    d = criar_duble_padrao(n_ticks=200)
    d.initialize()
    eur, jpy = d.symbol_info("EURUSD"), d.symbol_info("USDJPY")
    assert (eur.digits, jpy.digits) == (5, 3) and eur.volume_min == 0.01 and eur.ticks_bookdepth == 0
    tk = d.symbol_info_tick("EURUSD")
    assert tk.last == 0.0 and tk.volume == 0 and round(tk.bid, 5) == tk.bid
    assert d.account_info().currency == "BRL"
    assert criar_duble_padrao(moeda_conta="USD").moeda_conta == "USD"


def test_duble_mt4_traduz_swap_e_modo_de_lucro():
    from robowin.mt5.conector_mt4 import ConectorMT4, TransporteLocal
    from robowin.mt5.duble_mt4 import criar_duble_mt4_padrao
    d4 = criar_duble_mt4_padrao(n_ticks=200, moeda_conta="USD")
    con = ConectorMT4(transporte=TransporteLocal(d4))
    assert con.initialize()
    info = con.symbol_info("EURUSD")
    assert info.trade_calc_mode == 0 and info.swap_mode == 1 and info.swap_rollover3days == 3
    assert info.currency_margin == "EUR" and con.account_info().currency == "USD"
    imp = gerar_perfil(con, "USDJPY", plataforma="mt4")
    assert imp.dados["tick_valor_lucro"] == P("USDJPY").tick_valor_lucro and imp.dados["swap_dia_triplo"] == "quarta"


# ------------------------------------------------------------------------------------------------- importacao
@pytest.mark.parametrize("ativo,classe", [("EURUSD", "forex"), ("USDJPY", "forex"), ("XAUUSD", "metal"),
                                          ("BTCUSD", "cripto"), ("US500", "cfd_indice")])
@pytest.mark.parametrize("moeda_conta", ["USD", "BRL"])
def test_importar_do_terminal_gera_perfil_a_confirmar_igual_ao_modelo(ativo, classe, moeda_conta):
    d = criar_duble_padrao(n_ticks=200, moeda_conta=moeda_conta)
    imp = gerar_perfil(d, ativo, hoje=dt.date(2026, 10, 28))
    p = P(ativo)
    assert imp.chave == ativo and imp.dados["classe"] == classe and imp.dados["a_confirmar"] is True
    assert "DUBLE SINTETICO" in imp.dados["fonte"] and imp.dados["fonte_data"] == "2026-10-28"
    for k in ("tick_pontos", "volume_min", "volume_step", "tamanho_contrato", "tick_valor_lucro", "moeda_lucro"):
        assert imp.dados[k] == getattr(p, k), k
    assert "comissao_lote_lado" not in imp.dados  # o terminal nao informa: nada inventado
    assert any("comissao" in i for i in imp.dados["a_confirmar_itens"])


def test_importar_resolve_sufixo_e_recusa_ambiguo(tmp_path):
    d = DubleMT5(RelogioSimulado(ny(2026, 11, 10, 10, 0)), moeda_conta="USD")
    p = P("EURUSD")
    t = ny(2026, 11, 10, 10, 0) // MS - 1000
    tks = gerar_ticks_sinteticos(t, 20, 100, p.preco_sintetico_ref, 1, tick=p.tick_pontos, decimais=5,
                                 so_cotacoes=True, spread_ticks=10)
    d.adicionar_simbolo(especificacao_generica(p, "EURUSD.m", moeda_conta="USD"), tks)
    imp = gerar_perfil(d, "EURUSD")
    assert imp.chave == "EURUSD" and imp.dados["simbolo_corretora"] == "EURUSD.m"
    arq = tmp_path / "inst.toml"
    arq.write_text("[WIN]\nclasse = \"futuro_indice\"\n", encoding="utf-8")
    assert gravar_perfil(imp, arq) == "criado"
    assert tomllib.loads(arq.read_text(encoding="utf-8"))["EURUSD"]["a_confirmar"] is True
    with pytest.raises(ImportacaoRecusada):
        gravar_perfil(imp, arq)  # nao sobrescreve sem --substituir
    assert gravar_perfil(imp, arq, substituir=True) == "substituido"
    assert resolver_nome(["EURUSD.m", "EURUSDpro"], "EURUSD", ".m") == "EURUSD.m"
    with pytest.raises(ImportacaoRecusada, match="ambiguo"):
        resolver_nome(["EURUSD.m", "EURUSDpro"], "EURUSD")
    with pytest.raises(ImportacaoRecusada):
        resolver_nome(["EURUSDX"], "EURUSD")  # outra letra maiuscula = outro simbolo


def test_importar_recusa_quando_o_valor_do_tick_nao_e_deduzivel():
    d = criar_duble_padrao(n_ticks=50, moeda_conta="BRL")
    d.initialize()
    d.especificacoes["EURUSD"].trade_calc_mode = 1  # futuros com conta em outra moeda: valor convertido apenas
    with pytest.raises(ImportacaoRecusada, match="nao e deduzivel"):
        gerar_perfil(d, "EURUSD", classe="forex")


def test_cli_importar_do_terminal_no_duble(tmp_path, capsys):
    from robowin.cli import main
    arq = tmp_path / "inst.toml"
    arq.write_text("", encoding="utf-8")
    rc = main(["perfil", "importar-do-terminal", "XAUUSD", "--duble", "--instrumentos", str(arq),
               "--config", str(tmp_path / "nada.toml")])
    out = capsys.readouterr().out
    assert rc == 0 and "a_confirmar = true" in out
    assert tomllib.loads(arq.read_text(encoding="utf-8"))["XAUUSD"]["classe"] == "metal"


# ------------------------------------------------------------------------------------------------- features
@pytest.mark.parametrize("ativo", ATIVOS)
def test_features_desligadas_por_capacidade(ativo):
    p = P(ativo)
    fora = set(features_desligadas_instrumento(p))
    if p.tem_negocios:
        assert not fora & set(features_de_negocios())
    else:
        assert set(features_de_negocios()) <= fora


def test_pipeline_com_ticks_so_de_cotacao_mede_volume_de_tick():
    p = P("EURUSD")
    pipe = PipelineFeatures(desligadas=features_desligadas_instrumento(p))
    t0 = ny(2026, 11, 10, 10, 0)
    v = None
    for k in range(120):
        t = t0 + k * 250 * MS
        pipe.on_tick(_tick(t, 1.1 + k * 1e-5, 1.1001 + k * 1e-5))
        v = pipe.vetor(t)
    assert v is not None
    assert v.valores["volume_tick_30s"] == pytest.approx(4.0, rel=0.05)  # 4 cotacoes/s
    assert all(math.isnan(v.valores[n]) for n in features_de_negocios())


def test_armazem_aceita_ticks_bid_ask_sem_negocios(tmp_path):
    from robowin.dados.qualidade import verificar
    from robowin.dados.ticks import ArmazenamentoTicks
    t0 = ny(2026, 11, 10, 10, 0)
    tks = [_tick(t0 + k * 100 * MS, round(1.1 + k * 1e-5, 5), round(1.1001 + k * 1e-5, 5)) for k in range(50)]
    a = ArmazenamentoTicks(tmp_path)
    assert a.gravar("EURUSD", tks) == 50
    assert not verificar(tks, sintetico=True).criticos


# ------------------------------------------------------------------------------------------------- aceitacao
@pytest.mark.parametrize("plataforma,moeda_conta", [("mt5", "USD"), ("mt5", "BRL"), ("mt4", "USD")])
def test_aceitacao_eurusd_confirmado_no_duble_sem_reprovacoes(tmp_path, monkeypatch, plataforma, moeda_conta):
    """Com o perfil EURUSD e a sessao FOREX CONFIRMADOS (copia temporaria; o repositorio segue a_confirmar), a
    aceitacao inteira roda no duble: lote 0.01, so cotacoes (A4.1), sem livro (A4.3 nao aplicavel), conta em BRL
    convertida pelo USDBRL do instante (A2.4)."""
    import argparse
    import json
    from pathlib import Path

    import robowin.core.bolsas as B
    import robowin.core.instrumentos as I
    from robowin.aceitacao import cli_cmds as K
    from robowin.perfil.edicao_toml import atualizar_secao
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "ROBOWIN_MT5_SENHA"):
        monkeypatch.delenv(v, raising=False)
    ti, tb = tmp_path / "instrumentos.toml", tmp_path / "bolsas.toml"
    ti.write_text(atualizar_secao(Path(I.CAMINHO_PADRAO).read_text(encoding="utf-8"), "EURUSD", {"a_confirmar": False}),
                  encoding="utf-8")
    tb.write_text(atualizar_secao(Path(B.CAMINHO_PADRAO).read_text(encoding="utf-8"), "FOREX", {"a_confirmar": False}),
                  encoding="utf-8")
    monkeypatch.setattr(I, "CAMINHO_PADRAO", ti)
    monkeypatch.setattr(B, "CAMINHO_PADRAO", tb)
    p = argparse.ArgumentParser()
    K.registrar(p.add_subparsers(dest="cmd"))
    a = p.parse_args(["aceitacao", "--saida", str(tmp_path / "out"), "--duble", "--plataforma", plataforma,
                      "--ativo", "EURUSD", "--minutos-coleta", "1", "--repeticoes", "4", "--confirmo-conta-demo",
                      "--pular-ia"])
    rel = RelogioSimulado(int(K.INICIO_DUBLE.timestamp() * 1e9))
    term = K.criar_duble_sim(plataforma, rel, K._n_ticks_duble(1, 4, 3.0), moeda_conta=moeda_conta)
    rc = K.rodar(a, terminal=term, relogio=rel)
    r = json.loads(sorted((tmp_path / "out").glob("aceitacao-*.json"))[-1].read_text(encoding="utf-8"))
    est = {e["etapa"]: e for e in r["etapas"]}
    assert rc == 0 and not [e for e in r["etapas"] if e["estado"] == "REPROVADO"]
    for cod in ("A2.4", "A3.1", "A3.2", "A4.1", "A5.1", "A5.2", "A5.3", "A5.4", "A7", "A8.1", "A9"):
        assert est[cod]["estado"] == "APROVADO", (cod, est[cod]["detalhe"])
    assert "volume=0.01 (esperado 0.01)=True" in est["A5.2"]["detalhe"]
    if moeda_conta == "BRL":
        assert "USDBRL" in est["A2.4"]["detalhe"]
