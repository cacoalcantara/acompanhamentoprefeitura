"""Propriedade (docs/16 WP7): PlanoTradeIA aleatorias e ADVERSARIAS (NaN, inf, negativos, gigantes, sinais trocados,
gestao absurda) passam por validador, compilador, ancoragem, motor de risco e roteador. Toda intencao APROVADA pelo
motor satisfaz os invariantes: exposicao, perda por operacao, sem martingale, stop so aperta, distancia minima,
tempo no teto e quantidade <= unidades_max. O motor e checado mesmo quando o validador ja recusou (defesa em
profundidade): o veto final nunca depende da IA nem do validador."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.instrumentos import carregar_perfil
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    AjustePosicao,
    Condicao,
    ConfigPlanejador,
    ConfigRisco,
    Lado,
    PlanoTradeIA,
    Posicao,
    RegraGestao,
    Tick,
)
from robowin.dados.banco import BancoSQLite
from robowin.decisao.bracos.base import BracoBase, Geometria, ResultadoDecisao
from robowin.decisao.compilador_plano import ErroCompilacao, compilar_plano
from robowin.decisao.orquestrador import ConfigOrquestrador, Orquestrador
from robowin.decisao.registro import Registro
from robowin.decisao.validade import ajustar_entrada_ancorada
from robowin.ia.validadores import validar_plano_trade
from robowin.risco.invariantes import (
    inv_distancia_minima,
    inv_exposicao,
    inv_perda_operacao,
    inv_sem_martingale,
    stop_so_aperta,
)

NS = 1_000_000_000
T0 = dt_utc_ns(dt.datetime(2026, 11, 10, 10, 0))
PERFIL = carregar_perfil("WIN")
INS = PERFIL.instrumento("WINZ26")
TICK = INS.tick_pontos
RISCO = ConfigRisco()
CFG_P = ConfigPlanejador(ativo=True)
PRECO = 130_000.0

ruim = st.sampled_from([math.nan, math.inf, -math.inf, -1.0, 0.0, 1e12, -1e12, 1e-9])
pontos = st.one_of(ruim, st.floats(min_value=-500, max_value=2000, allow_nan=False), st.integers(1, 80).map(
    lambda k: k * TICK))
inteiros = st.one_of(st.integers(-10, 10), st.integers(-10**9, 10**9), st.sampled_from([0, 1, 2, 30, 120, 300, 10**7]))
regra = st.builds(
    RegraGestao, tipo=st.sampled_from(["breakeven", "trailing", "parcial", "sem_progresso", "apertar_apos",
                                       "ao_vencer_tempo"]),
    mfe_pontos=st.one_of(st.none(), pontos), apos_s=st.one_of(st.none(), inteiros),
    distancia_pontos=st.one_of(st.none(), pontos), folga_ticks=st.one_of(st.none(), inteiros),
    quantidade=st.one_of(st.none(), inteiros),
    acao_vencimento=st.one_of(st.none(), st.sampled_from(["encerrar", "proteger_e_estender"])),
    extensao_s=st.one_of(st.none(), inteiros))
condicao = st.builds(Condicao, tipo=st.sampled_from(["preco_toca", "preco_rompe_acima", "preco_rompe_abaixo",
                                                      "spread_max_pontos", "janela_tempo"]),
                     valor=st.one_of(st.none(), pontos))


def _orq() -> Orquestrador:
    class Nada(BracoBase):
        async def adecidir(self, ctx: Any) -> ResultadoDecisao:  # pragma: no cover - nunca chamado aqui
            raise NotImplementedError

    orq = Orquestrador(ConfigOrquestrador(instrumento=INS, risco=RISCO, sessao_id="p", exigir_aquecimento=False,
                                          latencia_exec_ns=0),
                       relogio=RelogioSimulado(T0), registro=Registro(BancoSQLite(":memory:"), None, "p"),
                       bracos=[Nada("p", "condicional", Geometria(0.0, 0.0, 0))])
    orq.sim.on_tick(Tick(t_servidor_ms=T0 // 1_000_000, t_utc_ns=T0, t_recebido_ns=T0, bid=PRECO, ask=PRECO + TICK,
                         last=PRECO, volume=1.0, flags=8))
    return orq


def _checar_entrada(orq: Orquestrador, it: Any, t: int) -> None:
    conta = orq.contas["p"]
    motor = conta.motor
    dec = motor.avaliar(it, Posicao(simbolo=INS.simbolo), t)
    if not dec.aprovada:
        return
    q = it.quantidade
    assert 1 <= q <= motor.unidades_max
    assert inv_exposicao(q, [], motor.unidades_max)
    assert it.stop_pontos is not None and math.isfinite(it.stop_pontos)
    assert it.stop_pontos >= motor.stop_min_pontos() - 1e-9
    assert inv_perda_operacao(it.stop_pontos, RISCO.slippage_folga_pontos, q, INS.ponto_valor_brl,
                              RISCO.perda_max_operacao_brl)
    assert inv_sem_martingale([q], [False])
    assert it.tempo_max_s is None or 0 < it.tempo_max_s <= RISCO.tempo_max_posicao_s
    cmd = conta.roteador._montar_entrada(it, {})
    lado = Lado.COMPRA if it.acao == Acao.COMPRAR else Lado.VENDA
    assert cmd.sl is not None and inv_distancia_minima(lado, cmd.sl, it.preco_referencia, motor.stop_min_pontos() - TICK)
    assert cmd.quantidade == q


@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(acao=st.sampled_from([Acao.COMPRAR, Acao.VENDER]), q=inteiros, stop=pontos, alvo=pontos, tempo=inteiros,
       contra=pontos, favor=pontos, expira=inteiros, gestao=st.lists(regra, max_size=4),
       gatilhos=st.lists(condicao, max_size=3), p_alvo=st.one_of(st.none(), st.floats(0, 1)),
       movimento=st.integers(-20, 20))
def test_entrada_adversaria_aprovada_respeita_invariantes(acao, q, stop, alvo, tempo, contra, favor, expira, gestao,
                                                         gatilhos, p_alvo, movimento):
    try:
        p = PlanoTradeIA(acao=acao, quantidade=q, stop_pontos=stop, alvo_pontos=alvo, tempo_max_s=tempo,
                         faixa_contra_pontos=contra, faixa_favor_pontos=favor, expira_s=expira, gestao=tuple(gestao),
                         gatilhos=tuple(gatilhos), p_alvo=p_alvo, invalidacoes=("x",),
                         evidencias_a_favor=("movimento.ret_60s_pts = 1",), evidencias_contra=("y",))
    except ValidationError:
        return
    orq = _orq()
    conta = orq.contas["p"]
    env = conta.motor.envelope("p", T0, margem_s=CFG_P.margem_zeragem_s)
    v = validar_plano_trade(p, {"movimento": {"ret_60s_pts": 1.0}}, env=env, cfg=CFG_P, tick_pontos=TICK,
                            custo_ida_volta_pontos=2.0, slip_stop_medio_pontos=1.0)
    try:
        c = compilar_plano(p, ref_t0=PRECO, t0_ns=T0, tick_pontos=TICK, env=env, cfg=CFG_P, braco="p", ciclo_id="c")
    except ErroCompilacao:
        return
    except Exception as e:  # o compilador so pode recusar com ErroCompilacao (plano ja validado ou nao)
        if v.ok:
            raise AssertionError(f"compilador quebrou com plano validado: {e!r}") from e
        return
    preco_exec = PRECO + movimento * TICK
    t = T0 + NS
    it = c.intencao_base.model_copy(update={"validade_ns": t + 30 * NS, "plano": None})
    ajustada, _ = ajustar_entrada_ancorada(it, preco_exec=preco_exec, compra=acao == Acao.COMPRAR,
                                           stop_min=conta.motor.stop_min_pontos(), alvo_min=TICK, favor_max=10 * TICK)
    for cand in (it.model_copy(update={"preco_referencia": preco_exec}), ajustada):
        if cand is not None:
            _checar_entrada(orq, cand, t)


@settings(max_examples=150, deadline=None)
@given(compra=st.booleans(), q=st.integers(1, 3), stop_res=st.one_of(st.none(), pontos),
       alvo_res=st.one_of(st.none(), pontos), tempo_total=st.one_of(st.none(), inteiros),
       reduzir=st.one_of(st.none(), inteiros), ticket_t0=st.sampled_from([None, 7, 8]),
       movimento=st.integers(-30, 30), kill=st.booleans())
def test_ajuste_adversario_aprovado_so_aperta_e_respeita_distancia(compra, q, stop_res, alvo_res, tempo_total, reduzir,
                                                                  ticket_t0, movimento, kill):
    try:
        aj = AjustePosicao(stop_resultado_pontos=stop_res, alvo_resultado_pontos=alvo_res,
                           tempo_max_total_s=tempo_total, reduzir_quantidade=reduzir)
    except ValidationError:
        return
    orq = _orq()
    conta = orq.contas["p"]
    if kill:
        conta.kill.acionar("teste", "manual_cli", T0)
    lado = Lado.COMPRA if compra else Lado.VENDA
    s = 1 if compra else -1
    pos = Posicao(simbolo=INS.simbolo, lado=lado, quantidade=q, preco_medio=PRECO, ticket=7,
                  stop=PRECO - s * 30 * TICK, alvo=PRECO + s * 60 * TICK, aberta_ns=T0 - 30 * NS)
    ref = PRECO + movimento * TICK
    from robowin.core.tipos import Intencao
    it = Intencao(id="aj", braco="p", acao=Acao.MANTER, validade_ns=T0 + 60 * NS, preco_referencia=ref, t0_ns=T0,
                  ajuste=aj, posicao_ticket=ticket_t0, quantidade_t0=q)
    dec = conta.motor.avaliar(it, pos, T0)
    if not dec.aprovada:
        return
    assert ticket_t0 in (None, 7)  # preso a posicao vista em t0
    sl, tp = conta.roteador._niveis_ajuste(it, pos)
    if sl is not None:
        assert stop_so_aperta(lado, pos.stop, sl)
        assert inv_distancia_minima(lado, sl, ref, conta.motor.dist_min_ajuste_pontos())
    if tp is not None:
        assert s * (tp - ref) > 0
    if aj.reduzir_quantidade is not None:
        assert 1 <= aj.reduzir_quantidade < q
    if aj.tempo_max_total_s is not None:
        assert 0 < aj.tempo_max_total_s <= RISCO.tempo_max_posicao_s
    if kill:
        assert aj.alvo_resultado_pontos is None and aj.tempo_max_total_s is None  # com kill, so aperto e reducao


def test_plano_sao_chega_aprovado_ao_motor():
    """Sanidade da propriedade: um plano razoavel atravessa validador, compilador, ancoragem e motor."""
    orq = _orq()
    conta = orq.contas["p"]
    env = conta.motor.envelope("p", T0, margem_s=CFG_P.margem_zeragem_s)
    p = PlanoTradeIA(acao=Acao.COMPRAR, quantidade=1, stop_pontos=20 * TICK, alvo_pontos=40 * TICK, tempo_max_s=120,
                     faixa_contra_pontos=2 * TICK, faixa_favor_pontos=TICK, expira_s=120, p_alvo=0.4)
    c = compilar_plano(p, ref_t0=PRECO, t0_ns=T0, tick_pontos=TICK, env=env, cfg=CFG_P, braco="p", ciclo_id="c")
    it = c.intencao_base.model_copy(update={"validade_ns": T0 + 30 * NS, "plano": None})
    aj, viol = ajustar_entrada_ancorada(it, preco_exec=PRECO + TICK / 2, compra=True,
                                        stop_min=conta.motor.stop_min_pontos(), alvo_min=TICK, favor_max=TICK)
    assert aj is not None, viol
    assert conta.motor.avaliar(aj, Posicao(simbolo=INS.simbolo), T0 + NS).aprovada
    _checar_entrada(orq, aj, T0 + NS)
