"""Contabilidade por EPISODIO (docs/16, C12): parciais so somam P&L; o fechamento conta o episodio inteiro em perdas
seguidas, pausa e martingale (q maximo do episodio)."""
import datetime as dt

from hypothesis import given
from hypothesis import strategies as st
from perfis_teste import WIN

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.ids import novo_id
from robowin.core.tipos import Acao, ConfigRisco, Intencao, Posicao
from robowin.risco.motor import MotorRiscoPadrao
from robowin.risco.sessao import ContabilidadeSessao

INS = WIN.instrumento("WINZ26")


def T(h, m, s=0, dia=(2026, 11, 10)):
    return dt_utc_ns(dt.datetime(*dia, h, m, s))


def test_duas_parciais_com_prejuizo_contam_uma_perda_seguida():
    c = ContabilidadeSessao()
    c.registrar_parcial("b", -10.0, 1, T(10, 0))
    c.registrar_parcial("b", -5.0, 1, T(10, 1))
    e = c.estado("b", T(10, 1))
    assert e.perdas_seguidas == 0 and e.ultima_perda_ns is None and not e.ultima_foi_perda
    assert e.pnl_dia == -15.0 and e.pnl_semana == -15.0 and e.equity_realizado == -15.0
    e = c.registrar_trade("b", -7.0, 1, T(10, 2))
    assert e.perdas_seguidas == 1 and e.ultima_foi_perda and e.ultima_perda_ns == T(10, 2)
    assert e.pnl_dia == -22.0 and e.liquido_episodio == 0.0 and e.q_max_episodio == 0
    assert e.ultimo_tamanho == 3


def test_episodio_com_parcial_lucrativa_compensa_fechamento_negativo():
    c = ContabilidadeSessao()
    c.registrar_trade("b", -1.0, 1, T(9, 30))
    c.registrar_parcial("b", 20.0, 1, T(10, 0))
    e = c.registrar_trade("b", -5.0, 1, T(10, 5))  # episodio = +15: zera a sequencia
    assert e.perdas_seguidas == 0 and not e.ultima_foi_perda and e.ultimo_tamanho == 2


@given(st.lists(st.tuples(st.floats(-100, 100), st.integers(1, 3)), min_size=1, max_size=20))
def test_sem_parciais_identico_ao_comportamento_anterior(trades):
    c = ContabilidadeSessao()
    perdas = 0
    for k, (liq, q) in enumerate(trades):
        e = c.registrar_trade("b", liq, q, T(10, 0) + k)
        perdas = perdas + 1 if liq < 0 else 0
        assert e.ultimo_tamanho == q and e.perdas_seguidas == perdas and e.ultima_foi_perda is (liq < 0)


def test_parcial_conta_no_limite_diario_e_aciona_kill():
    cfg = ConfigRisco()
    m = MotorRiscoPadrao(cfg, INS)
    m.registrar_parcial("b", -cfg.perda_max_dia_brl, 1, T(10, 0))
    assert m.kill.ativo and m.kill.ultimo is not None and m.kill.ultimo.origem == "perda_diaria"
    assert m.contab.estado("b", T(10, 0)).perdas_seguidas == 0


def test_martingale_usa_q_maximo_do_episodio():
    cfg = ConfigRisco(contratos_max=1, pausa_apos_perda_s=0)
    m = MotorRiscoPadrao(cfg, INS)
    m.cfg = m.cfg.model_copy(update={"contratos_max": 3})  # simula politica futura com mais contratos
    m.unidades_max = 3
    m.registrar_parcial("b", -5.0, 1, T(10, 0))
    m.registrar_trade("b", -5.0, 1, T(10, 1))  # episodio de 2 unidades terminou em perda
    assert m.contab.estado("b", T(10, 1)).ultimo_tamanho == 2

    def entrada(q):
        t = T(10, 5)
        return m.avaliar(Intencao(id=novo_id("i"), braco="b", acao=Acao.COMPRAR, quantidade=q,
                                  stop_pontos=20 * INS.tick_pontos,
                                  validade_ns=t + int(60e9), preco_referencia=2000 * INS.tick_pontos),
                         Posicao(simbolo="WINZ26"), t)

    assert "martingale_proibido" not in entrada(2).motivos
    assert "martingale_proibido" in entrada(3).motivos


def test_parcial_tira_a_fatia_realizada_do_pnl_aberto_sem_contar_duas_vezes():
    """marcar(-400) com 2 unidades e parcial de 1 realizando -200: o dia vale -400, nao -600 (kill falso)."""
    c = ContabilidadeSessao()
    c.marcar_a_mercado("b", -400.0, T(10, 0))
    e = c.registrar_parcial("b", -200.0, 1, T(10, 0, 1), restante=1)
    assert e.pnl_aberto == -200.0 and e.pnl_dia_total == -400.0 and e.drawdown == 400.0
    cfg = ConfigRisco()
    m = MotorRiscoPadrao(cfg, INS)
    perda = cfg.perda_max_dia_brl
    m.marcar_a_mercado("b", -0.8 * perda, T(10, 0))
    m.registrar_parcial("b", -0.4 * perda, 1, T(10, 0, 1), restante=1)
    assert not m.kill.ativo  # a perda real (0.8 do limite) nao passou do limite


def test_parcial_com_lucro_nao_cria_drawdown_fantasma():
    c = ContabilidadeSessao()
    c.marcar_a_mercado("b", 400.0, T(10, 0))
    c.registrar_parcial("b", 200.0, 1, T(10, 0, 1), restante=1)
    e = c.marcar_a_mercado("b", 200.0, T(10, 0, 2))  # o preco nao andou: a unidade restante segue em +200
    assert e.pico == 400.0 and e.drawdown == 0.0
