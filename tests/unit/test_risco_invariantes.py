"""Invariantes de risco por propriedade (hypothesis), incluindo intercalacoes arbitrarias de eventos."""
import datetime as dt

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from perfis_teste import WIN

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.ids import novo_id
from robowin.core.tipos import Acao, ConfigRisco, Intencao, Lado, Posicao
from robowin.risco.invariantes import (
    inv_entrada_permitida,
    inv_exposicao,
    inv_posicao_assinada,
    inv_sem_aumento,
    inv_sem_martingale,
    stop_so_aperta,
)
from robowin.risco.kill_switch import KillSwitch
from robowin.risco.motor import MotorRiscoPadrao

BASE = dt_utc_ns(dt.datetime(2026, 11, 10, 8, 0))
CFG = ConfigRisco(operacoes_dia_max=1000, perdas_seguidas_max=1000, pausa_apos_perda_s=0,
                  perda_max_dia_brl=1e6, perda_max_semana_brl=1e6, dd_max_brl=1e6, dd_alerta_brl=1e6)
CFG_LIVRE = CFG.model_copy(update={"perda_max_operacao_brl": 1e5})

eventos = st.one_of(
    st.tuples(st.just("entrada"), st.sampled_from([Acao.COMPRAR, Acao.VENDER]), st.integers(1, 3),
              st.one_of(st.none(), st.floats(1, 400))),
    st.tuples(st.just("preencher")), st.tuples(st.just("cancelar")), st.tuples(st.just("sair")),
    st.tuples(st.just("encerrar")), st.tuples(st.just("kill")), st.tuples(st.just("tempo"), st.integers(1, 5400)),
)


@settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(eventos, max_size=60))
def test_exposicao_maxima_em_todas_as_intercalacoes(evs):
    """|posicao + pendentes| <= 1 apos QUALQUER sequencia de eventos; nenhuma entrada fora da janela/com kill."""
    ks = KillSwitch()
    m = MotorRiscoPadrao(CFG, WIN.instrumento("WINZ26"), kill_switch=ks)
    t = BASE
    pos_qtd, pos_lado = 0, None
    pend: dict[str, Lado] = {}
    for ev in evs:
        k = ev[0]
        if k == "tempo":
            t += int(ev[1] * 1e9)
        elif k == "kill":
            ks.acionar("prop", "manual_cli", t)
        elif k == "entrada":
            _, acao, q, stop = ev
            it = Intencao(id=novo_id("i"), braco="b", acao=acao, quantidade=q, stop_pontos=stop,
                          validade_ns=t + int(1e9), preco_referencia=130000.0)
            posicao = Posicao(simbolo="WINZ26", lado=pos_lado, quantidade=pos_qtd)
            d = m.avaliar(it, posicao, t)
            assert inv_entrada_permitida(d.aprovada, m.dentro_da_janela(t), ks.ativo)
            if d.aprovada:
                lado = Lado.COMPRA if acao == Acao.COMPRAR else Lado.VENDA
                m.reservar_pendente(it, lado, t)
                pend[it.id] = lado
        elif k == "preencher" and pend:
            iid = next(iter(pend))
            lado = pend.pop(iid)
            m.liberar_pendente(iid)
            pos_qtd, pos_lado = pos_qtd + 1, lado  # o fill SEMPRE ocorre (pior caso do mercado)
        elif k == "cancelar" and pend:
            iid = next(iter(pend))
            pend.pop(iid)
            m.liberar_pendente(iid)
        elif k in ("sair", "encerrar") and pos_qtd:
            if k == "encerrar":
                it = Intencao(id=novo_id("i"), braco="b", acao=Acao.ENCERRAR, validade_ns=t + 1)
                assert m.avaliar(it, Posicao(simbolo="WINZ26", lado=pos_lado, quantidade=pos_qtd), t).aprovada
            pos_qtd, pos_lado = 0, None
        assert inv_exposicao(pos_qtd, [1] * len(pend), CFG.contratos_max)
        assert m.pendentes_braco("b") == len(pend)


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.integers(1, 3), st.floats(-100, 100)), min_size=1, max_size=30))
def test_sem_martingale_no_motor(ops):
    """Apos uma perda, nenhuma entrada aprovada pode ser maior que a anterior (usa limite alto de contratos)."""
    m = MotorRiscoPadrao(CFG_LIVRE, WIN.instrumento("WINZ26"))
    m.cfg = m.cfg.model_copy(update={"contratos_max": 3})  # simula politica futura com mais contratos
    t = BASE + int(3600e9)
    aprovados, perdas = [], []
    for q, pnl in ops:
        it = Intencao(id=novo_id("i"), braco="b", acao=Acao.COMPRAR, quantidade=q, stop_pontos=100.0,
                      validade_ns=t + int(1e9), preco_referencia=130000.0)
        d = m.avaliar(it, Posicao(simbolo="WINZ26"), t)
        if d.aprovada:
            aprovados.append(q)
            perdas.append(pnl < 0)
            m.registrar_trade("b", pnl, q, t)
        t += int(10e9)
    assert inv_sem_martingale(aprovados, perdas)


@given(st.lists(st.integers(-100, 100), min_size=1, max_size=40), st.sampled_from(list(Lado)))
def test_stop_so_aperta_monotono(propostas, lado):
    m = MotorRiscoPadrao(CFG, WIN.instrumento("WINZ26"))
    stop = 100.0
    historico = [stop]
    for delta in propostas:
        novo = stop + delta
        pos = Posicao(simbolo="WINZ26", lado=lado, quantidade=1, stop=stop)
        if m.validar_stop(pos, novo):
            stop = novo
            historico.append(stop)
    seq = historico if lado == Lado.COMPRA else [-x for x in historico]
    assert all(b >= a for a, b in zip(seq, seq[1:], strict=False))
    assert all(stop_so_aperta(lado, a, b) for a, b in zip(historico, historico[1:], strict=False))


@settings(max_examples=200, deadline=None)
@given(st.integers(0, 24 * 3600 - 1), st.booleans(), st.sampled_from([Acao.COMPRAR, Acao.VENDER]))
def test_janela_e_kill_switch(seg, kill, acao):
    ks = KillSwitch()
    if kill:
        ks.acionar("x", "manual_cli", 0)
    m = MotorRiscoPadrao(CFG, WIN.instrumento("WINZ26"), kill_switch=ks)
    t = BASE - int(8 * 3600e9) + int(seg * 1e9)  # dia 10/11 (SP) segundo `seg`
    it = Intencao(id=novo_id("i"), braco="b", acao=acao, stop_pontos=100.0, validade_ns=t + int(1e9),
                  preco_referencia=130000.0)
    d = m.avaliar(it, Posicao(simbolo="WINZ26"), t)
    assert inv_entrada_permitida(d.aprovada, m.dentro_da_janela(t), ks.ativo)
    if d.aprovada:
        assert 9 * 3600 + 15 * 60 <= seg < 17 * 3600 + 30 * 60 and not kill


@given(st.integers(0, 3), st.integers(0, 3))
def test_funcoes_puras(a, b):
    assert inv_sem_aumento(0, b)
    assert inv_sem_aumento(a, a) and (a == 0 or not inv_sem_aumento(a, a + 1))
    assert inv_posicao_assinada(0, [1]) and not inv_posicao_assinada(1, [1])
    assert inv_posicao_assinada(1, [-1])  # pendente que reduz nao conta
