"""Motor de risco para o braco planejador (docs/16, WP1): despacho fail-closed, ENCERRAR/MANTER presos a posicao
vista em t0, ajuste em pontos de resultado, teto de tempo, stop minimo, gestao rechecada e envelope coerente com o
veto. Os numeros vem do perfil do instrumento (config/instrumentos.toml), nunca de literal de ativo no codigo."""
import datetime as dt

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from perfis_teste import WIN
from pydantic import ValidationError

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.ids import novo_id
from robowin.core.tipos import (
    Acao,
    AjustePosicao,
    ConfigRisco,
    EnvelopeDecisao,
    Intencao,
    Lado,
    PlanoGestaoCompilado,
    Posicao,
    RegraCompilada,
    RegraGestao,
)
from robowin.risco.kill_switch import KillSwitch
from robowin.risco.motor import EstadoMercado, MotorRiscoPadrao
from robowin.risco.politica import (
    DIST_MIN_STOP_TICKS,
    STOP_MIN_TICKS,
    stop_max_por_quantidade,
    stop_maximo_efetivo_pontos,
    validar_coerencia,
)

INS = WIN.instrumento("WINZ26")
TICK = INS.tick_pontos
REF = 2000 * TICK  # preco qualquer na grade do tick
NS = 1_000_000_000


def T(h, m, s=0, dia=(2026, 11, 10)):
    return dt_utc_ns(dt.datetime(*dia, h, m, s))


def motor(cfg=None, **kw):
    return MotorRiscoPadrao(cfg or ConfigRisco(), INS, **kw)


def entrada(acao=Acao.COMPRAR, stop=20 * TICK, t=None, braco="b1", **kw):
    t = t or T(10, 0)
    return Intencao(id=novo_id("int"), braco=braco, acao=acao, stop_pontos=stop, validade_ns=t + int(60e9),
                    preco_referencia=kw.pop("ref", REF), **kw)


def manter(ajuste=None, t=None, ref=REF, **kw):
    t = t or T(10, 0)
    return Intencao(id=novo_id("int"), braco="b1", acao=Acao.MANTER, validade_ns=t + int(60e9),
                    preco_referencia=ref, ajuste=ajuste, **kw)


def posicao(lado=Lado.COMPRA, q=2, pm=REF, stop=None, alvo=None, aberta=None, ticket=7):
    if stop is None:
        stop = pm - 20 * TICK if lado == Lado.COMPRA else pm + 20 * TICK
    return Posicao(simbolo="WINZ26", lado=lado, quantidade=q, preco_medio=pm, ticket=ticket, stop=stop,
                   alvo=alvo, aberta_ns=aberta if aberta is not None else T(9, 58))


VAZIA = Posicao(simbolo="WINZ26")


# ------------------------------------------------------------------ despacho -----------------------------------
def test_acao_desconhecida_e_vetada():
    it = Intencao.model_construct(**{**entrada().__dict__, "acao": "PIRAMIDAR"})
    d = motor().avaliar(it, VAZIA, T(10, 0))
    assert not d.aprovada and d.motivos == ("acao_desconhecida",)


def test_encerrar_com_ticket_velho_vetado_e_sem_ticket_nunca():
    ks = KillSwitch()
    ks.acionar("x", "manual_cli", 1)
    m = motor(kill_switch=ks)
    pos = posicao(ticket=7)
    t = T(18, 30)  # fora do pregao e com kill: zerar continua permitido
    sem = Intencao(id=novo_id("i"), braco="b1", acao=Acao.ENCERRAR, validade_ns=t)
    assert m.avaliar(sem, pos, t).aprovada
    mesmo = sem.model_copy(update={"posicao_ticket": 7})
    assert m.avaliar(mesmo, pos, t).aprovada
    velho = sem.model_copy(update={"posicao_ticket": 3})
    d = m.avaliar(velho, pos, t)
    assert not d.aprovada and d.motivos == ("posicao_diferente_da_vista",)


# ------------------------------------------------------------------ MANTER legado ------------------------------
def test_manter_legado_stop_colado_no_preco():
    m = motor()
    pos = posicao(stop=REF - 50 * TICK)
    assert m.avaliar(manter(stop_pontos=10 * TICK), pos, T(10, 0)).aprovada
    d = m.avaliar(manter(stop_pontos=0.5 * TICK), pos, T(10, 0))
    assert "stop_colado_no_preco" in d.motivos
    m.atualizar_mercado(EstadoMercado(spread_pts=3 * TICK))  # spread entra na distancia minima
    d = m.avaliar(manter(stop_pontos=2 * TICK), pos, T(10, 0))
    assert d.motivos == ("stop_colado_no_preco",)


def test_manter_legado_preso_ao_ticket():
    d = motor().avaliar(manter(stop_pontos=10 * TICK, posicao_ticket=99), posicao(), T(10, 0))
    assert d.motivos == ("posicao_diferente_da_vista",)


# ------------------------------------------------------------------ MANTER com ajuste ---------------------------
def test_ajuste_stop_em_pontos_de_resultado():
    m = motor()
    pos = posicao(stop=REF - 20 * TICK)  # stop atual perde 20 ticks
    ref = REF + 10 * TICK  # preco executavel de saida 10 ticks a favor
    assert m.avaliar(manter(AjustePosicao(stop_resultado_pontos=0.0), ref=ref), pos, T(10, 0)).aprovada  # empate
    assert m.avaliar(manter(AjustePosicao(stop_resultado_pontos=5 * TICK), ref=ref), pos, T(10, 0)).aprovada
    d = m.avaliar(manter(AjustePosicao(stop_resultado_pontos=-30 * TICK), ref=ref), pos, T(10, 0))
    assert d.motivos == ("stop_so_pode_apertar",)
    d = m.avaliar(manter(AjustePosicao(stop_resultado_pontos=10 * TICK), ref=ref), pos, T(10, 0))
    assert d.motivos == ("stop_colado_no_preco",)  # no preco de saida
    d = m.avaliar(manter(AjustePosicao(stop_resultado_pontos=12 * TICK), ref=ref), pos, T(10, 0))
    assert d.motivos == ("stop_colado_no_preco",)  # do lado errado


def test_ajuste_stop_venda_e_arredondamento_em_direcao_ao_preco():
    m = motor()
    pos = posicao(lado=Lado.VENDA, stop=REF + 20 * TICK)
    ref = REF - 10 * TICK
    assert m.avaliar(manter(AjustePosicao(stop_resultado_pontos=3 * TICK), ref=ref), pos, T(10, 0)).aprovada
    # 8.6 ticks de lucro arredonda para 9 ticks (em direcao ao preco): a 1 tick do preco de saida = distancia minima
    assert DIST_MIN_STOP_TICKS == 1
    it = manter(AjustePosicao(stop_resultado_pontos=8.6 * TICK), ref=ref)
    assert m.avaliar(it, pos, T(10, 0)).aprovada
    it = manter(AjustePosicao(stop_resultado_pontos=9.2 * TICK), ref=ref)  # -> 10 ticks: no preco
    assert m.avaliar(it, pos, T(10, 0)).motivos == ("stop_colado_no_preco",)


@pytest.mark.parametrize("alvo,motivo", [
    (-5 * TICK, "alvo_invalido"), (0.0, "alvo_invalido"), (float("nan"), "alvo_invalido"),
    (5 * TICK, "alvo_invalido"),  # abaixo do preco de saida (10 ticks a favor)
    (10 * TICK, "alvo_invalido"), (11 * TICK, "alvo_colado_ao_preco"), (12 * TICK, "alvo_colado_ao_preco"),
])
def test_ajuste_alvo_invalido(alvo, motivo):
    pos = posicao()
    m = motor()
    m.atualizar_mercado(EstadoMercado(spread_pts=2 * TICK))  # distancia minima = 1 tick + spread = 3 ticks
    assert m.avaliar(manter(AjustePosicao(alvo_resultado_pontos=13 * TICK), ref=REF + 10 * TICK), pos, T(10, 0)).aprovada
    d = m.avaliar(manter(AjustePosicao(alvo_resultado_pontos=alvo), ref=REF + 10 * TICK), pos, T(10, 0))
    assert d.motivos == (motivo,)


def test_ajuste_alvo_move_nos_dois_sentidos():
    m = motor()
    pos = posicao(alvo=REF + 30 * TICK)
    for alvo in (15 * TICK, 60 * TICK):
        assert m.avaliar(manter(AjustePosicao(alvo_resultado_pontos=alvo), ref=REF + 10 * TICK), pos, T(10, 0)).aprovada


def test_ajuste_tempo():
    cfg = ConfigRisco(tempo_max_posicao_s=600)
    m = motor(cfg)
    pos = posicao(aberta=T(10, 0))
    t = T(10, 5)  # 300 s decorridos
    assert m.avaliar(manter(AjustePosicao(tempo_max_total_s=400), t=t), pos, t).aprovada
    assert m.avaliar(manter(AjustePosicao(tempo_max_total_s=600), t=t), pos, t).aprovada
    assert m.avaliar(manter(AjustePosicao(tempo_max_total_s=601), t=t), pos, t).motivos == (
        "tempo_max_acima_do_limite",)
    assert m.avaliar(manter(AjustePosicao(tempo_max_total_s=300), t=t), pos, t).motivos == ("tempo_ja_vencido",)
    assert m.avaliar(manter(AjustePosicao(tempo_max_total_s=0), t=t), pos, t).motivos == (
        "tempo_max_acima_do_limite",)
    # passa da zeragem (17:45): aberta 17:40 e 600 s => 17:50
    tarde = posicao(aberta=T(17, 40))
    t2 = T(17, 41)
    assert m.avaliar(manter(AjustePosicao(tempo_max_total_s=600), t=t2), tarde, t2).motivos == (
        "tempo_max_acima_do_limite",)
    assert m.avaliar(manter(AjustePosicao(tempo_max_total_s=300), t=t2), tarde, t2).aprovada


@pytest.mark.parametrize("r,ok", [(1, True), (0, False), (2, False), (-1, False), (3, False)])
def test_ajuste_reducao(r, ok):
    d = motor().avaliar(manter(AjustePosicao(reduzir_quantidade=r)), posicao(q=2), T(10, 0))
    assert d.aprovada is ok and (ok or d.motivos == ("reducao_invalida",))


def test_reducao_nunca_vetada_por_janela_kill_ou_limites():
    ks = KillSwitch()
    m = motor(ConfigRisco(perdas_seguidas_max=1), kill_switch=ks)
    m.registrar_trade("b1", -ConfigRisco().perda_max_dia_brl, 1, T(9, 30))  # perda do dia atingida => kill
    assert m.kill.ativo
    m.atualizar_mercado(EstadoMercado(feed_ok=False, leilao=True, vol_extrema=True))
    for t in (T(10, 0), T(17, 50), T(20, 0)):
        it = manter(AjustePosicao(reduzir_quantidade=1), t=t)
        d = m.avaliar(it, posicao(q=2, aberta=T(9, 58)), t)
        assert d.aprovada, (t, d.motivos)


def test_kill_restringe_a_aperto_e_reducao():
    ks = KillSwitch()
    ks.acionar("x", "manual_cli", 1)
    m = motor(kill_switch=ks)
    pos = posicao(q=2, alvo=REF + 30 * TICK, aberta=T(9, 58))
    ref = REF + 10 * TICK
    ok = AjustePosicao(stop_resultado_pontos=2 * TICK, reduzir_quantidade=1)
    assert m.avaliar(manter(ok, ref=ref), pos, T(10, 0)).aprovada
    for aj in (AjustePosicao(alvo_resultado_pontos=40 * TICK), AjustePosicao(tempo_max_total_s=200),
               AjustePosicao(substituir_gestao=(RegraGestao(tipo="breakeven", mfe_pontos=5 * TICK),))):
        d = m.avaliar(manter(aj, ref=ref), pos, T(10, 0))
        assert "kill_ativo_so_aperto" in d.motivos, aj


def test_ajuste_preso_a_posicao_vista():
    m = motor()
    pos = posicao(q=2, ticket=7)
    aj = AjustePosicao(stop_resultado_pontos=0.0)
    ref = REF + 10 * TICK
    assert m.avaliar(manter(aj, ref=ref, posicao_ticket=7, quantidade_t0=2), pos, T(10, 0)).aprovada
    assert m.avaliar(manter(aj, ref=ref, posicao_ticket=8), pos, T(10, 0)).motivos == ("posicao_diferente_da_vista",)
    assert m.avaliar(manter(aj, ref=ref, quantidade_t0=1), pos, T(10, 0)).motivos == ("posicao_diferente_da_vista",)
    assert m.avaliar(manter(aj, ref=ref), VAZIA, T(10, 0)).motivos == ("posicao_diferente_da_vista",)


def test_ajuste_substituir_gestao_rechecada():
    m = motor()
    pos = posicao(q=2)
    boa = (RegraGestao(tipo="trailing", distancia_pontos=10 * TICK), RegraGestao(tipo="parcial", quantidade=1,
                                                                                  mfe_pontos=10 * TICK))
    assert m.avaliar(manter(AjustePosicao(substituir_gestao=boa)), pos, T(10, 0)).aprovada
    ruim = (RegraGestao(tipo="parcial", quantidade=2),)
    assert m.avaliar(manter(AjustePosicao(substituir_gestao=ruim)), pos, T(10, 0)).motivos == (
        "gestao_invalida:parcial",)


# ------------------------------------------------------------------ entrada ------------------------------------
def test_stop_abaixo_do_minimo_e_alvo_abaixo_de_um_tick():
    m = motor()
    assert STOP_MIN_TICKS == 2
    assert m.avaliar(entrada(stop=STOP_MIN_TICKS * TICK), VAZIA, T(10, 0)).aprovada
    d = m.avaliar(entrada(stop=1.5 * TICK), VAZIA, T(10, 0))
    assert d.motivos == ("stop_abaixo_do_minimo",)
    d = m.avaliar(entrada(alvo_pontos=0.5 * TICK), VAZIA, T(10, 0))
    assert d.motivos == ("alvo_abaixo_de_um_tick",)
    assert m.avaliar(entrada(alvo_pontos=TICK), VAZIA, T(10, 0)).aprovada


@pytest.mark.parametrize("tempo,ok", [(None, True), (1, True), (300, True), (301, False), (0, False), (-5, False)])
def test_teto_de_tempo_na_entrada(tempo, ok):
    d = motor().avaliar(entrada(tempo_max_s=tempo), VAZIA, T(10, 0))
    assert d.aprovada is ok and (ok or d.motivos == ("tempo_max_acima_do_limite",))


def test_tempo_da_entrada_nao_passa_da_zeragem():
    m = motor(ConfigRisco(tempo_max_posicao_s=1800))
    t = T(17, 29)  # zeragem 17:45 => 960 s
    assert m.avaliar(entrada(tempo_max_s=960, t=t), VAZIA, t).aprovada
    assert m.avaliar(entrada(tempo_max_s=961, t=t), VAZIA, t).motivos == ("tempo_max_acima_do_limite",)


def test_gestao_invalida_na_entrada():
    m = motor()
    g = PlanoGestaoCompilado(passo_min_pontos=TICK, intervalo_ajuste_s=2.0, regras=(
        RegraCompilada(tipo="trailing", distancia_pontos=10 * TICK),))
    assert m.avaliar(entrada(gestao=g), VAZIA, T(10, 0)).aprovada
    casos = [
        (RegraCompilada(tipo="martingale"), "gestao_invalida:martingale"),
        (RegraCompilada(tipo="parcial", quantidade=1), "gestao_invalida:parcial"),  # q=1: sem parcial
        (RegraCompilada(tipo="trailing", distancia_pontos=0.5 * TICK), "gestao_invalida:trailing"),
        (RegraCompilada(tipo="breakeven", mfe_pontos=float("nan")), "gestao_invalida:breakeven"),
    ]
    for regra, motivo in casos:
        g2 = g.model_copy(update={"regras": (regra,)})
        assert m.avaliar(entrada(gestao=g2), VAZIA, T(10, 0)).motivos == (motivo,)


# ------------------------------------------------------------------ entradas_possiveis ------------------------
_INTENCIONAIS = {"perda_diaria_excederia_limite", "perda_semanal_excederia_limite", "exposicao_acima_do_maximo"}


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.integers(0, 24 * 3600 - 1), st.booleans(), st.floats(0, 600), st.integers(0, 12), st.booleans(),
       st.booleans(), st.booleans(), st.integers(0, 4))
def test_entradas_possiveis_igual_aos_motivos_nao_intencionais(seg, kill, perda, ops, feed, leilao, pend, nperdas):
    ks = KillSwitch()
    if kill:
        ks.acionar("x", "manual_cli", 0)
    cfg = ConfigRisco(perdas_seguidas_max=3)
    m = motor(cfg, kill_switch=ks)
    t = dt_utc_ns(dt.datetime(2026, 11, 10)) + seg * NS + 3 * 3600 * NS  # 10/11 em SP
    if perda > 0:
        m.contab.registrar_trade("b1", -perda, 1, t - 1)
    for _ in range(nperdas):
        m.contab.registrar_trade("b1", -1.0, 1, t - 1)
    m.contab.estado("b1", t).operacoes_dia += ops
    if pend:
        m.reservar_pendente(entrada(t=t), Lado.COMPRA, t)
    m.atualizar_mercado(EstadoMercado(feed_ok=feed, leilao=leilao))
    base = m.entradas_possiveis("b1", t)
    it = entrada(stop=m.stop_min_pontos(), t=t)  # a intencao mais modesta possivel
    d = m.avaliar(it, VAZIA, t)
    assert set(d.motivos) - _INTENCIONAIS == set(base)
    assert d.aprovada is (not base)
    # e qualquer outra intencao de entrada carrega pelo menos esses motivos
    outra = entrada(stop=200 * TICK, t=t, acao=Acao.VENDER, alvo_pontos=10 * TICK)
    assert set(base) <= set(m.avaliar(outra, VAZIA, t).motivos)


def test_orcamento_de_perda_insuficiente():
    m = motor()
    t = T(10, 0)
    cfg = m.cfg
    pior_min = (m.stop_min_pontos() + cfg.slippage_folga_pontos) * INS.ponto_valor_brl
    m.registrar_trade("b1", -(cfg.perda_max_dia_brl - pior_min) - 0.01, 1, t - 10 * 60 * NS)
    assert "orcamento_de_perda_insuficiente" in m.entradas_possiveis("b1", t)
    assert "orcamento_de_perda_insuficiente" in m.avaliar(entrada(t=t), VAZIA, t).motivos


# ------------------------------------------------------------------ envelope -----------------------------------
@settings(max_examples=80, deadline=None)
@given(st.floats(0, 190), st.floats(0, 400))
def test_envelope_coerente_com_o_veto(perda_dia, perda_semana_antes):
    m = motor()
    t = T(12, 0)
    if perda_semana_antes > 0:
        m.registrar_trade("b1", -perda_semana_antes, 1, T(12, 0, dia=(2026, 11, 9)))
    if perda_dia > 0:
        m.registrar_trade("b1", -perda_dia, 1, T(10, 0))
    env = m.envelope("b1", t)
    assert isinstance(env, EnvelopeDecisao) and env.unidades_max == m.unidades_max
    s = env.stop_max_pontos_por_q[0]
    if s >= env.stop_min_pontos and env.entradas_possiveis:
        assert m.avaliar(entrada(stop=s, t=t), VAZIA, t).aprovada
    acima = m.avaliar(entrada(stop=s + TICK, t=t), VAZIA, t)
    assert not acima.aprovada


def test_envelope_sem_perdas_mostra_o_maximo_efetivo():
    m = motor()
    env = m.envelope("b1", T(10, 0))
    assert env.stop_max_pontos_por_q == (stop_maximo_efetivo_pontos(m.cfg, INS.ponto_valor_brl, 1),)
    assert env.stop_min_pontos == STOP_MIN_TICKS * TICK
    assert env.dist_min_ajuste_pontos == DIST_MIN_STOP_TICKS * TICK
    assert env.tempo_max_s == m.cfg.tempo_max_posicao_s and env.entradas_possiveis
    assert m.avaliar(entrada(stop=env.stop_max_pontos_por_q[0]), VAZIA, T(10, 0)).aprovada
    assert not m.avaliar(entrada(stop=env.stop_max_pontos_por_q[0] + TICK), VAZIA, T(10, 0)).aprovada


def test_envelope_tempo_ate_zeragem_e_com_posicao():
    m = motor()
    assert m.envelope("b1", T(17, 42)).tempo_max_s == 180
    assert m.envelope("b1", T(17, 42), margem_s=120).tempo_max_s == 60
    assert m.envelope("b1", T(17, 50)).tempo_max_s == 0
    pos = posicao(aberta=T(10, 0))
    env = m.envelope("b1", T(10, 2), posicao=pos)
    assert env.tempo_max_s == 300 and not env.entradas_possiveis
    assert "posicao_aberta" in env.motivos_sem_entrada
    m.atualizar_mercado(EstadoMercado(spread_pts=2 * TICK))
    assert m.envelope("b1", T(10, 0)).dist_min_ajuste_pontos == (DIST_MIN_STOP_TICKS + 2) * TICK


def test_envelope_sem_entradas_lista_codigos_sem_numeros():
    ks = KillSwitch()
    ks.acionar("x", "manual_cli", 1)
    env = motor(kill_switch=ks).envelope("b1", T(20, 0))
    assert not env.entradas_possiveis
    assert {"kill_switch_ativo", "fora_da_janela_de_entradas"} <= set(env.motivos_sem_entrada)
    assert not any(ch.isdigit() for mot in env.motivos_sem_entrada for ch in mot)


def test_envelope_nao_tem_campo_em_dinheiro():
    campos = set(EnvelopeDecisao.model_fields)
    assert not any(c.endswith(("_brl", "_conta")) or "perda" in c or "dd_" in c for c in campos)


def test_stop_max_por_quantidade():
    cfg = ConfigRisco()
    pv = INS.ponto_valor_brl
    por_q = stop_max_por_quantidade(cfg, pv, 3, cfg.perda_max_dia_brl, cfg.perda_max_semana_brl, tick=TICK)
    assert len(por_q) == 3 and por_q[0] == stop_maximo_efetivo_pontos(cfg, pv, 1)
    assert por_q[0] >= por_q[1] >= por_q[2] >= 0
    assert all(abs(v / TICK - round(v / TICK)) < 1e-9 for v in por_q)
    assert stop_max_por_quantidade(cfg, pv, 1, 0.0, 1e9) == (0.0,)
    assert stop_max_por_quantidade(cfg, 0.0, 2, 1e9, 1e9) == (0.0, 0.0)
    assert stop_max_por_quantidade(cfg, pv, 0, 1e9, 1e9) == ()
    assert stop_maximo_efetivo_pontos(cfg, pv) == stop_maximo_efetivo_pontos(cfg, pv, cfg.contratos_max)


# ------------------------------------------------------------------ politica -----------------------------------
def test_validar_coerencia_teto_de_tempo():
    validar_coerencia(ConfigRisco(tempo_max_posicao_s=1))
    validar_coerencia(ConfigRisco(tempo_max_posicao_s=8 * 3600 + 30 * 60))  # 09:15 -> 17:45
    for v in (0, -1, 8 * 3600 + 30 * 60 + 1):
        with pytest.raises(Exception, match="tempo_max_posicao_s"):
            validar_coerencia(ConfigRisco(tempo_max_posicao_s=v))


def test_motor_ignora_limites_vindos_de_fora():
    """A intencao nao tem campo que chegue a um limite (extra=forbid) e o veto so usa a politica aprovada."""
    with pytest.raises(ValidationError):
        Intencao(id="i", braco="b1", acao=Acao.COMPRAR, validade_ns=1, perda_max_operacao_brl=1e9)
    with pytest.raises(ValidationError):
        AjustePosicao(tempo_max_posicao_s=99999)
    m = motor()
    dossie = {"limites": {"stop_max_pontos": 10_000, "tempo_max_posicao_s": 99999}}  # nunca lido pelo motor
    it = entrada(stop=10_000.0, tempo_max_s=dossie["limites"]["tempo_max_posicao_s"])
    d = m.avaliar(it, VAZIA, T(10, 0))
    assert {"stop_acima_do_maximo", "tempo_max_acima_do_limite"} <= set(d.motivos)
