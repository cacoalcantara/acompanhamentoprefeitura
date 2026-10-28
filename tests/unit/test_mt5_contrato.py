"""Contrato vigente, especificacoes e relogio do servidor (dados sinteticos)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as NS

import pytest
from hypothesis import given
from hypothesis import strategies as st
from perfis_teste import WIN
from test_mt5_comum import OFFSET_S, SIMB, T0_MS, T0_NS, especificacao_win, montar_duble, ticks_planos

from robowin.core.relogio import RelogioSimulado
from robowin.mt5 import constantes as C
from robowin.mt5.contrato import ContratoRecusado, escolher_simbolo, resolver_contrato
from robowin.mt5.duble import DubleMT5, criar_duble_padrao
from robowin.mt5.specs import EspecEsperada, preenchimento_do_simbolo, validar_especificacoes
from robowin.mt5.tempo_servidor import RelogioServidor, calibrar, estimar_offset

ESPEC = EspecEsperada.do_perfil(WIN)


def duble_com(nomes, venc=None, **alt):
    d = DubleMT5(RelogioSimulado(T0_NS))
    for n in nomes:
        e = especificacao_win(n, 0)
        for k, v in alt.get(n, {}).items():
            setattr(e, k, v)
        d.adicionar_simbolo(e, ticks_planos(5))
    d.initialize()
    return d


def test_resolve_contrato_vigente_no_duble_padrao():
    d = criar_duble_padrao()
    d.initialize()
    r = resolver_contrato(d, dt.date(2026, 10, 27), WIN)
    i = r.instrumento
    assert r.simbolo_corretora == "WINZ26" and i.simbolo == "WINZ26" and i.vencimento == "2026-12-16"
    assert i.tick_pontos == 5.0 and i.ponto_valor_brl == pytest.approx(0.20) and i.lote == 1
    assert i.book_depth > 0 and i.trade_exemode == C.EXEC_EXCHANGE and i.filling_mode & C.SIMB_FILL_IOC
    assert r.outros_contratos == ["WING27"]


@pytest.mark.parametrize("dia,esperado", [
    (dt.date(2026, 11, 1), "WINZ26"),   # fim do horario de verao dos EUA
    (dt.date(2026, 11, 2), "WINZ26"),   # feriado
    (dt.date(2026, 11, 3), "WINZ26"),   # nova grade
    (dt.date(2026, 12, 16), "WINZ26"),  # dia do vencimento: ainda vigente
    (dt.date(2026, 12, 17), "WING27"),  # rolagem
])
def test_contrato_nas_datas_criticas(dia, esperado):
    d = criar_duble_padrao()
    d.initialize()
    assert resolver_contrato(d, dia, WIN).simbolo_corretora == esperado


def test_mesmo_prefixo_e_sufixo_da_corretora():
    d = duble_com(["B3.WINZ26.A", "B3.WING27.A", "B3.INDZ26.A"])
    r = resolver_contrato(d, dt.date(2026, 10, 27), WIN)
    assert r.simbolo_corretora == "B3.WINZ26.A" and (r.prefixo, r.sufixo) == ("B3.", ".A")


def test_decoracao_predominante_resolve_ambiguidade():
    c = escolher_simbolo(["WINZ26", "WINZ26F", "WING27"], "WINZ26", WIN)
    assert c.nome == "WINZ26"
    with pytest.raises(ContratoRecusado):
        escolher_simbolo(["WINZ26", "XWINZ26", "WING27F", "WINJ27F"], "WINZ26", WIN)


def test_contrato_nao_oferecido_recusa():
    d = duble_com(["WING27", "WINJ27"])
    with pytest.raises(ContratoRecusado, match="nao oferecido"):
        resolver_contrato(d, dt.date(2026, 10, 27), WIN)
    d2 = duble_com(["INDZ26"])
    with pytest.raises(ContratoRecusado):
        resolver_contrato(d2, dt.date(2026, 10, 27), WIN)


@pytest.mark.parametrize("campo,valor", [
    ("trade_tick_size", 10.0), ("trade_tick_size", 1.0), ("trade_tick_value", 2.0), ("trade_tick_value", 0.5),
    ("volume_min", 5.0), ("volume_step", 2.0),
])
def test_especificacao_divergente_recusa(campo, valor):
    d = duble_com([SIMB, "WING27"], **{SIMB: {campo: valor}})
    with pytest.raises(ContratoRecusado):
        resolver_contrato(d, dt.date(2026, 10, 27), WIN)


def test_vencimento_divergente_do_calendario_recusa():
    errado = int(dt.datetime(2026, 12, 30, tzinfo=dt.UTC).timestamp())
    d = duble_com([SIMB], **{SIMB: {"expiration_time": errado}})
    with pytest.raises(ContratoRecusado, match="vencimento"):
        resolver_contrato(d, dt.date(2026, 10, 27), WIN)
    certo = int(dt.datetime(2026, 12, 16, 21, tzinfo=dt.UTC).timestamp())
    assert resolver_contrato(duble_com([SIMB], **{SIMB: {"expiration_time": certo}}), dt.date(2026, 10, 27), WIN)


def test_validar_especificacoes_lista_divergencias():
    assert validar_especificacoes(especificacao_win("WINZ26"), ESPEC) == []
    assert validar_especificacoes(None, ESPEC)
    ruim = especificacao_win("WINZ26")
    ruim.trade_tick_size, ruim.trade_tick_value = 10.0, 2.0
    assert len(validar_especificacoes(ruim, ESPEC)) == 1  # tick diverge; valor do ponto (0,20) confere


def test_moeda_divergente_ou_ausente_recusa():
    e = especificacao_win("WINZ26")
    assert validar_especificacoes(e, ESPEC, moeda_conta="BRL") == []
    assert any("moeda da conta" in m for m in validar_especificacoes(e, ESPEC, moeda_conta="USD"))
    assert any("nao informada" in m for m in validar_especificacoes(e, ESPEC, moeda_conta=""))
    e.currency_profit = "USD"
    assert any("currency_profit" in m for m in validar_especificacoes(e, ESPEC, moeda_conta="BRL"))
    e.currency_profit = ""
    assert any("nao confirmada" in m for m in validar_especificacoes(e, ESPEC, moeda_conta="BRL"))


def test_resolver_contrato_recusa_conta_em_outra_moeda():
    d = criar_duble_padrao()
    d.initialize()
    real = d.account_info
    d.account_info = lambda: NS(**{**vars(real()), "currency": "USD"})
    with pytest.raises(ContratoRecusado, match="moeda da conta"):
        resolver_contrato(d, dt.date(2026, 10, 27), WIN)


def test_preenchimento_lido_do_simbolo():
    e = especificacao_win("WINZ26")
    assert preenchimento_do_simbolo(e) == C.PREENCHE_IOC
    e.filling_mode = C.SIMB_FILL_FOK
    assert preenchimento_do_simbolo(e) == C.PREENCHE_FOK
    e.filling_mode = 0
    assert preenchimento_do_simbolo(e) == C.PREENCHE_RETURN


# --- relogio do servidor -----------------------------------------------------------------------------
@pytest.mark.parametrize("off_s", [0, 7200, 10800, 12600, -10800, 19800])
def test_calibra_offset_nao_nulo(off_s):
    d, rel = montar_duble(ticks_planos(), offset_servidor_s=off_s)
    rs = calibrar(d, SIMB, rel, amostras=3, intervalo_s=0.5, dormir=lambda s: rel.avancar(int(s * 1e9)))
    assert rs.offset_s == off_s and rs.confiavel and rs.origem == "calibrado"


def test_tick_velho_nunca_e_confiavel_sem_evidencia_de_feed_vivo():
    d, rel = montar_duble(ticks_planos(), offset_servidor_s=OFFSET_S)
    rel.avancar(120_000_000_000)  # feed parado ha ~81 s: a idade parece pequena, mas nao ha evidencia de vida
    rs = calibrar(d, SIMB, rel)
    assert rs.offset_s == OFFSET_S and rs.confiavel is False and 60 < rs.idade_implicita_s < 100
    rel.avancar(3_600_000_000_000)  # 1 h sem ticks: o calculo erra o offset E continua marcado nao confiavel
    rs2 = calibrar(d, SIMB, rel)
    assert rs2.confiavel is False and rs2.offset_s != OFFSET_S
    assert calibrar(d, SIMB, rel, offset_fixo_s=OFFSET_S).offset_s == OFFSET_S


def test_feed_vivo_durante_a_amostragem_torna_confiavel():
    d, rel = montar_duble(None, offset_servidor_s=OFFSET_S)  # serie sintetica longa (300 s)
    rel.avancar(60_000_000_000)
    rs = calibrar(d, SIMB, rel, amostras=3, intervalo_s=1.0, dormir=lambda s: rel.avancar(int(s * 1e9)))
    assert rs.offset_s == OFFSET_S and rs.confiavel is True


@given(off=st.integers(-14 * 4, 14 * 4).map(lambda k: k * 900), idade=st.floats(0, 830), skew=st.floats(-55, 0))
def test_estimar_offset_recupera_o_valor_verdadeiro(off, idade, skew):
    est = off - idade - skew  # skew negativo: relogio local atrasado => est maior
    assert estimar_offset(est)[0] == off


def test_grava_ms_bruto_e_utc_normalizado():
    d, rel = montar_duble(ticks_planos())
    rs = calibrar(d, SIMB, rel)
    tk = d.symbol_info_tick(SIMB)
    t = rs.tick_de_linha({"time_msc": tk.time_msc, "bid": tk.bid, "ask": tk.ask, "last": tk.last,
                          "volume_real": 0.0, "flags": tk.flags}, rel.agora_ns())
    assert t.t_servidor_ms == tk.time_msc == T0_MS + OFFSET_S * 1000
    assert t.t_utc_ns == T0_MS * 1_000_000
    assert rs.utc_ns_para_servidor_ms(t.t_utc_ns) == t.t_servidor_ms
    assert rs.utc_ns_para_dt_servidor(t.t_utc_ns).timestamp() == pytest.approx(tk.time_msc / 1000)


def test_sem_tick_nao_calibra():
    d = DubleMT5(RelogioSimulado(T0_NS))
    d.adicionar_simbolo(especificacao_win(SIMB), [])
    d.initialize()
    rs = calibrar(d, SIMB, d.relogio)
    assert rs == RelogioServidor(0, T0_NS, False, 0, 0.0, "nao_calibrado")
    assert NS  # import usado
