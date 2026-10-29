"""Gestao automatica (docs/16 WP5): cada regra, eventos de reavaliacao e propriedades sobre caminhos aleatorios."""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from perfis_teste import INS_WIN

from robowin.core.tipos import (
    Acao,
    ConfigPlanejador,
    Intencao,
    Lado,
    PlanoGestaoCompilado,
    Posicao,
    RegraGestao,
)
from robowin.decisao.compilador_plano import compilar_gestao
from robowin.decisao.gestao import (
    ApertarStop,
    Encerrar,
    EstenderPrazo,
    EventoReavaliacao,
    Reduzir,
    aplicar_acao,
    atualizar_excursao,
    avaliar_gestao,
    iniciar_estado,
    simular_caminho,
    sincronizar_posicao,
)

TICK = INS_WIN.tick_pontos
NS = 1_000_000_000
T0 = 5_000 * NS
ENT = 130_000.0
CFG = ConfigPlanejador(ativo=True, intervalo_ajuste_s=0.0)


@dataclass(frozen=True)
class Tk:
    t_utc_ns: int
    bid: float
    ask: float
    last: float


def _gestao(*regras: RegraGestao, reavaliar_se=(), cfg=CFG) -> PlanoGestaoCompilado:
    return compilar_gestao(regras, tick_pontos=TICK, cfg=cfg, espelhado=False, reavaliar_em_s=None,
                           reavaliar_se=reavaliar_se)


def _estado(lado=Lado.COMPRA, q=1, stop=10 * TICK, alvo=20 * TICK, tempo=300):
    s = 1 if lado == Lado.COMPRA else -1
    pos = Posicao(simbolo="X", lado=lado, quantidade=q, preco_medio=ENT, ticket=7, stop=ENT - s * stop,
                  alvo=ENT + s * alvo, aberta_ns=T0)
    it = Intencao(id="i", braco="p", acao=Acao.COMPRAR if s > 0 else Acao.VENDER, validade_ns=T0, stop_pontos=stop)
    return iniciar_estado(pos, it, tempo)


def _av(plano, est, preco, t_s=1.0, *, custo=0.0, dist_min=TICK, teto=None, spread=TICK):
    # compra: bid = preco (saida), ask = preco + spread; venda: ask = preco
    if est.lado == Lado.COMPRA:
        bid, ask = preco, preco + spread
    else:
        bid, ask = preco - spread, preco
    t = T0 + int(t_s * NS)
    atualizar_excursao(est, bid=bid, ask=ask, last=preco, por_cotacao=False)
    return avaliar_gestao(plano, est, bid=bid, ask=ask, last=preco, t_ns=t, tick=TICK, dist_min=dist_min,
                          custo_ida_volta_pontos=custo, teto_prazo_ns=teto or est.prazo_ns + 600 * NS,
                          por_cotacao=False, perto_frac=0.3)


def test_iniciar_estado():
    est = _estado()
    assert est.sl == ENT - 10 * TICK and est.tp == ENT + 20 * TICK and est.prazo_ns == T0 + 300 * NS
    assert est.stop_inicial_pontos == 10 * TICK and est.qtd == est.qtd_inicial == 1
    with pytest.raises(ValueError):
        iniciar_estado(Posicao(simbolo="X"), Intencao(id="i", braco="p", acao=Acao.COMPRAR, validade_ns=0), 60)


def test_excursao_pelo_ultimo_negocio_e_pela_cotacao():
    est = _estado()
    atualizar_excursao(est, bid=ENT + 2 * TICK, ask=ENT + 3 * TICK, last=ENT + 4 * TICK, por_cotacao=False)
    assert est.mfe_pontos == 4 * TICK and est.extremo_favoravel == ENT + 4 * TICK
    est2 = _estado()
    atualizar_excursao(est2, bid=ENT + 2 * TICK, ask=ENT + 3 * TICK, last=ENT + 4 * TICK, por_cotacao=True)
    assert est2.mfe_pontos == 2 * TICK
    atualizar_excursao(est2, bid=ENT - 3 * TICK, ask=ENT - 2 * TICK, last=0.0, por_cotacao=True)
    assert est2.mae_pontos == 3 * TICK and est2.mfe_pontos == 2 * TICK


def test_breakeven_poe_stop_na_entrada_mais_custo_e_folga():
    g = _gestao(RegraGestao(tipo="breakeven", mfe_pontos=6 * TICK, folga_ticks=1))
    est = _estado()
    assert _av(g, est, ENT + 5 * TICK, custo=TICK) == []
    acoes = _av(g, est, ENT + 6 * TICK, custo=TICK)
    assert acoes == [ApertarStop(ENT + 2 * TICK, "breakeven")]


def test_breakeven_na_venda():
    g = _gestao(RegraGestao(tipo="breakeven", mfe_pontos=6 * TICK, folga_ticks=0))
    est = _estado(lado=Lado.VENDA)
    assert _av(g, est, ENT - 6 * TICK) == [ApertarStop(ENT, "breakeven")]


def test_trailing_segue_o_extremo_e_nunca_afrouxa():
    g = _gestao(RegraGestao(tipo="trailing", mfe_pontos=4 * TICK, distancia_pontos=3 * TICK))
    est = _estado()
    assert _av(g, est, ENT + 3 * TICK) == []
    a = _av(g, est, ENT + 4 * TICK, t_s=1)
    assert a == [ApertarStop(ENT + TICK, "trailing")]
    aplicar_acao(est, a[0])
    assert _av(g, est, ENT + 2 * TICK, t_s=2) == []  # voltou: o stop nao desce
    a = _av(g, est, ENT + 8 * TICK, t_s=3)
    assert a == [ApertarStop(ENT + 5 * TICK, "trailing")]


def test_parcial_inerte_com_q1_e_uma_vez_com_q3():
    g = _gestao(RegraGestao(tipo="parcial", mfe_pontos=4 * TICK, quantidade=5))
    assert _av(g, _estado(q=1), ENT + 5 * TICK) == []
    est = _estado(q=3)
    a = _av(g, est, ENT + 5 * TICK)
    assert a == [Reduzir(2, "parcial")]  # nunca zera pela parcial: no maximo q-1
    aplicar_acao(est, a[0])
    assert est.qtd == 1 and est.parciais == 1
    assert _av(g, est, ENT + 6 * TICK, t_s=2) == []


def test_sem_progresso_encerra_so_sem_progresso():
    g = _gestao(RegraGestao(tipo="sem_progresso", apos_s=60, mfe_pontos=4 * TICK))
    est = _estado()
    assert _av(g, est, ENT + 3 * TICK, t_s=30) == []
    assert _av(g, est, ENT + TICK, t_s=60) == [Encerrar("sem_progresso")]
    est2 = _estado()
    _av(g, est2, ENT + 5 * TICK, t_s=10)
    assert _av(g, est2, ENT, t_s=60) == []  # teve progresso: a regra e consumida e nao encerra
    assert _av(g, est2, ENT, t_s=90) == []


def test_apertar_apos_em_t():
    g = _gestao(RegraGestao(tipo="apertar_apos", apos_s=60, distancia_pontos=4 * TICK))
    est = _estado()
    assert _av(g, est, ENT, t_s=59) == []
    assert _av(g, est, ENT, t_s=60) == [ApertarStop(ENT - 4 * TICK, "apertar_apos")]


def test_ao_vencer_tempo_encerrar():
    g = _gestao(RegraGestao(tipo="ao_vencer_tempo", acao_vencimento="encerrar"))
    est = _estado(tempo=100)
    assert _av(g, est, ENT, t_s=99) == []
    assert _av(g, est, ENT, t_s=100) == [Encerrar("ao_vencer_tempo")]


def test_ao_vencer_tempo_estende_so_com_lucro_liquido_travado_e_ate_o_teto():
    g = _gestao(RegraGestao(tipo="ao_vencer_tempo", acao_vencimento="proteger_e_estender", extensao_s=120))
    est = _estado(tempo=100)
    assert _av(g, est, ENT + 5 * TICK, t_s=100, custo=TICK) == []  # stop original nao trava lucro
    est = _estado(tempo=100)
    est.sl = ENT + 2 * TICK
    teto = T0 + 160 * NS
    a = _av(g, est, ENT + 5 * TICK, t_s=100, custo=TICK, teto=teto)
    assert a == [EstenderPrazo(teto, "ao_vencer_tempo")]
    aplicar_acao(est, a[0])
    assert est.prazo_ns == teto and est.ajustes_auto == 1
    est = _estado(tempo=100)
    est.sl = ENT + TICK  # trava so o custo (empate liquido): nao estende
    assert _av(g, est, ENT + 5 * TICK, t_s=100, custo=TICK) == []


def test_distancia_minima_do_preco_executavel():
    g = _gestao(RegraGestao(tipo="apertar_apos", apos_s=0, distancia_pontos=TICK))
    est = _estado()
    a = _av(g, est, ENT + 4 * TICK, t_s=1, dist_min=2 * TICK)
    assert a == [ApertarStop(ENT + 2 * TICK, "apertar_apos")]


def test_intervalo_entre_ajustes():
    cfg = ConfigPlanejador(ativo=True, intervalo_ajuste_s=5.0)
    g = _gestao(RegraGestao(tipo="trailing", mfe_pontos=0.0, distancia_pontos=3 * TICK), cfg=cfg)
    est = _estado()
    a = _av(g, est, ENT + 4 * TICK, t_s=1)
    aplicar_acao(est, a[0])
    assert _av(g, est, ENT + 9 * TICK, t_s=3) == []  # dentro do intervalo
    assert _av(g, est, ENT + 9 * TICK, t_s=6) == [ApertarStop(ENT + 6 * TICK, "trailing")]


def test_quantizacao_pelo_passo():
    cfg = ConfigPlanejador(ativo=True, intervalo_ajuste_s=0.0, passo_min_ticks=2)
    g = _gestao(RegraGestao(tipo="trailing", mfe_pontos=0.0, distancia_pontos=3 * TICK), cfg=cfg)
    est = _estado()
    est.sl = ENT - 4 * TICK
    assert _av(g, est, ENT, t_s=1) == []  # alvo do stop ENT-3t: so 1 tick acima (passo = 2 ticks)
    a = _av(g, est, ENT + TICK, t_s=2)
    assert a == [ApertarStop(ENT - 2 * TICK, "trailing")]
    assert (a[0].nivel / (2 * TICK)) == int(a[0].nivel / (2 * TICK))


def test_eventos_uma_vez_e_so_os_pedidos():
    g = _gestao(RegraGestao(tipo="breakeven", mfe_pontos=10 * TICK),
                reavaliar_se=("perto_do_stop", "lucro_acima_stop", "metade_do_tempo", "regra_disparou"))
    est = _estado(tempo=100)
    assert _av(g, est, ENT - 8 * TICK, t_s=1) == [EventoReavaliacao("perto_do_stop")]
    assert _av(g, est, ENT - 8 * TICK, t_s=2) == []
    assert _av(g, est, ENT + 10 * TICK, t_s=3) == [ApertarStop(ENT, "breakeven"), EventoReavaliacao("lucro_acima_stop"),
                                                   EventoReavaliacao("regra_disparou")]
    assert _av(g, est, ENT + 10 * TICK, t_s=50) == [ApertarStop(ENT, "breakeven"),
                                                    EventoReavaliacao("metade_do_tempo")]
    sem_pedido = _gestao()
    assert _av(sem_pedido, _estado(), ENT - 9 * TICK) == []


def test_perto_do_alvo():
    g = _gestao(reavaliar_se=("perto_do_alvo",))
    est = _estado()
    assert _av(g, est, ENT + 13 * TICK) == []
    assert _av(g, est, ENT + 14 * TICK) == [EventoReavaliacao("perto_do_alvo")]


def test_sincronizar_posicao():
    est = _estado(q=3)
    pos = Posicao(simbolo="X", lado=Lado.COMPRA, quantidade=2, preco_medio=ENT, stop=ENT - TICK, alvo=None)
    sincronizar_posicao(est, pos, origem="ia")
    assert est.qtd == 2 and est.sl == ENT - TICK and est.tp is None and est.origem_ultimo_stop == "ia"


def test_aplicar_nunca_afrouxa_nem_aumenta():
    est = _estado(q=2)
    aplicar_acao(est, ApertarStop(ENT - 20 * TICK, "x"))
    assert est.sl == ENT - 10 * TICK
    aplicar_acao(est, Reduzir(2, "x"))
    assert est.qtd == 2
    aplicar_acao(est, EstenderPrazo(T0, "x"))
    assert est.prazo_ns == T0 + 300 * NS


def test_simular_caminho_desfechos():
    g = _gestao(RegraGestao(tipo="breakeven", mfe_pontos=4 * TICK))
    est = _estado()
    ticks = [Tk(T0 + i * NS, p, p + TICK, p) for i, p in enumerate([ENT, ENT + 4 * TICK, ENT + 2 * TICK, ENT])]
    caminho = simular_caminho(g, est, ticks, tick_pontos=TICK)
    assert caminho == [(T0 + NS, ApertarStop(ENT, "breakeven")), (T0 + 3 * NS, Encerrar("stop_movido_auto"))]
    assert est.sl == ENT - 10 * TICK  # est0 intacto
    alvo = [Tk(T0 + NS, ENT + 20 * TICK, ENT + 21 * TICK, ENT + 20 * TICK)]
    assert simular_caminho(_gestao(), est, alvo, tick_pontos=TICK) == [(T0 + NS, Encerrar("alvo"))]
    tempo = [Tk(T0 + 300 * NS, ENT, ENT + TICK, ENT)]
    assert simular_caminho(_gestao(), est, tempo, tick_pontos=TICK) == [(T0 + 300 * NS, Encerrar("tempo"))]
    stop = [Tk(T0 + NS, ENT - 10 * TICK, ENT - 9 * TICK, ENT - 10 * TICK)]
    assert simular_caminho(_gestao(), est, stop, tick_pontos=TICK) == [(T0 + NS, Encerrar("stop_inicial"))]


# --------------------------------------------------------------------------------------------------
# propriedades: caminhos aleatorios, regras aleatorias
# --------------------------------------------------------------------------------------------------
regra_st = st.one_of(
    st.builds(RegraGestao, tipo=st.just("breakeven"), mfe_pontos=st.integers(0, 15).map(lambda k: k * TICK),
              folga_ticks=st.integers(0, 3)),
    st.builds(RegraGestao, tipo=st.just("trailing"), mfe_pontos=st.integers(0, 15).map(lambda k: k * TICK),
              distancia_pontos=st.integers(1, 12).map(lambda k: k * TICK)),
    st.builds(RegraGestao, tipo=st.just("apertar_apos"), apos_s=st.integers(0, 200),
              distancia_pontos=st.integers(1, 12).map(lambda k: k * TICK)),
    st.builds(RegraGestao, tipo=st.just("parcial"), mfe_pontos=st.integers(0, 15).map(lambda k: k * TICK),
              quantidade=st.integers(1, 5)),
    st.builds(RegraGestao, tipo=st.just("sem_progresso"), apos_s=st.integers(1, 200),
              mfe_pontos=st.integers(1, 10).map(lambda k: k * TICK)),
    st.builds(RegraGestao, tipo=st.just("ao_vencer_tempo"),
              acao_vencimento=st.sampled_from(["encerrar", "proteger_e_estender"]),
              extensao_s=st.integers(0, 300)),
)


@settings(max_examples=150, deadline=None)
@given(regras=st.lists(regra_st, max_size=6), passos=st.lists(st.integers(-3, 3), min_size=1, max_size=150),
       compra=st.booleans(), q=st.integers(1, 4), intervalo=st.sampled_from([0.0, 2.0]),
       passo_ticks=st.integers(1, 2), spread_ticks=st.integers(1, 2), por_cotacao=st.booleans())
def test_propriedades_da_gestao(regras, passos, compra, q, intervalo, passo_ticks, spread_ticks, por_cotacao):
    cfg = ConfigPlanejador(ativo=True, intervalo_ajuste_s=intervalo, passo_min_ticks=passo_ticks)
    g = compilar_gestao(regras, tick_pontos=TICK, cfg=cfg, espelhado=False, reavaliar_em_s=None,
                        reavaliar_se=("perto_do_stop", "perto_do_alvo", "metade_do_tempo", "regra_disparou",
                                      "lucro_acima_stop"))
    lado = Lado.COMPRA if compra else Lado.VENDA
    s = 1 if compra else -1
    est = _estado(lado=lado, q=q, stop=10 * TICK, alvo=20 * TICK, tempo=120)
    teto = T0 + 240 * NS
    dist_min = TICK + spread_ticks * TICK
    preco = ENT
    ultimo_aj: int | None = None
    sls = [est.sl]
    qs = [est.qtd]
    eventos: list[str] = []
    saidas_por_passo: list[list] = []
    for i, d in enumerate(passos):
        preco += d * TICK
        t = T0 + (i + 1) * 2 * NS
        bid, ask = preco, preco + spread_ticks * TICK
        atualizar_excursao(est, bid=bid, ask=ask, last=preco, por_cotacao=por_cotacao)
        acoes = avaliar_gestao(g, est, bid=bid, ask=ask, last=preco, t_ns=t, tick=TICK, dist_min=dist_min,
                               custo_ida_volta_pontos=TICK, teto_prazo_ns=teto, por_cotacao=por_cotacao,
                               perto_frac=0.3)
        saidas_por_passo.append(acoes)
        saida = bid if compra else ask
        for a in acoes:
            if isinstance(a, ApertarStop):
                assert s * (saida - a.nivel) >= dist_min - 1e-9  # nunca colado no preco executavel
                assert est.sl is None or s * (a.nivel - est.sl) > 0  # so aperta
                assert abs(a.nivel / (passo_ticks * TICK) - round(a.nivel / (passo_ticks * TICK))) < 1e-6
                if ultimo_aj is not None:
                    assert t - ultimo_aj >= intervalo * NS
                ultimo_aj = t
            elif isinstance(a, Reduzir):
                assert 1 <= a.q < est.qtd
            elif isinstance(a, EstenderPrazo):
                assert a.prazo_ns <= teto and a.prazo_ns > est.prazo_ns
            elif isinstance(a, EventoReavaliacao):
                eventos.append(a.nome)
        if any(isinstance(a, Encerrar) for a in acoes):
            assert len(acoes) == 1
            break
        for a in acoes:
            aplicar_acao(est, a)
        sls.append(est.sl)
        qs.append(est.qtd)
    assert all(s * (b - a) >= 0 for a, b in zip(sls, sls[1:], strict=False))  # stop monotonico
    assert all(b <= a for a, b in zip(qs, qs[1:], strict=False)) and qs[-1] >= 1  # quantidade nunca cresce
    assert est.prazo_ns <= teto
    assert len(eventos) == len(set(eventos))  # uma vez por posicao

    # determinismo: o mesmo caminho da exatamente as mesmas acoes
    est2 = _estado(lado=lado, q=q, stop=10 * TICK, alvo=20 * TICK, tempo=120)
    preco = ENT
    for i, d in enumerate(passos[:len(saidas_por_passo)]):
        preco += d * TICK
        t = T0 + (i + 1) * 2 * NS
        bid, ask = preco, preco + spread_ticks * TICK
        atualizar_excursao(est2, bid=bid, ask=ask, last=preco, por_cotacao=por_cotacao)
        acoes = avaliar_gestao(g, est2, bid=bid, ask=ask, last=preco, t_ns=t, tick=TICK, dist_min=dist_min,
                               custo_ida_volta_pontos=TICK, teto_prazo_ns=teto, por_cotacao=por_cotacao,
                               perto_frac=0.3)
        assert acoes == saidas_por_passo[i]
        if any(isinstance(a, Encerrar) for a in acoes):
            break
        for a in acoes:
            aplicar_acao(est2, a)
