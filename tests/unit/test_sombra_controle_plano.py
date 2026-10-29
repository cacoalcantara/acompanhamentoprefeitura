"""Sombras do planejador (copia da entrada ja ancorada no mesmo t, gestao sem/trailing fixo) e o controle
aleatorio do plano (plano compilado inteiro, lado sorteado com seed, valores invertidos, reprodutivel)."""
from __future__ import annotations

import pytest
from test_decisao_comum import NS, T0, ctx_basico

from robowin.core.tipos import (
    Acao,
    Condicao,
    ConfigPlanejador,
    EnvelopeDecisao,
    Intencao,
    Lado,
    PlanoTradeIA,
    Posicao,
    RegraGestao,
)
from robowin.decisao.bracos.controles import BracoAleatorioPlano, espelhar_condicao
from robowin.decisao.bracos.sombra import REGRA_TRAILING_FIXO, BracoSombra
from robowin.decisao.compilador_plano import PlanoCompilado, compilar_plano
from robowin.decisao.espelho import EspelhoRecusado, validar_braco_espelhavel

ENV = EnvelopeDecisao(unidades_max=2, stop_max_pontos_por_q=(250.0, 120.0), stop_min_pontos=10.0,
                      dist_min_ajuste_pontos=10.0, tempo_max_s=300, entradas_possiveis=True)


def compilado(acao: Acao = Acao.COMPRAR, q: int = 1, gatilhos: tuple[Condicao, ...] = ()) -> PlanoCompilado:
    p = PlanoTradeIA(acao=acao, quantidade=q, stop_pontos=30.0, alvo_pontos=40.0, tempo_max_s=120,
                     faixa_contra_pontos=10.0, faixa_favor_pontos=5.0, expira_s=90, gatilhos=gatilhos, p_alvo=0.4,
                     gestao=(RegraGestao(tipo="breakeven", mfe_pontos=20.0, folga_ticks=1),), reavaliar_em_s=60,
                     reavaliar_se=("perto_do_stop",))
    return compilar_plano(p, ref_t0=130_000.0, t0_ns=T0, tick_pontos=5.0, env=ENV, cfg=ConfigPlanejador(),
                          braco="planejador", ciclo_id="c0")


def entrada_ajustada() -> Intencao:
    """Entrada de P ja ajustada pela ancoragem no disparo (stop/alvo restantes, preco executavel)."""
    it = compilado().intencao_base
    return it.model_copy(update={"stop_pontos": 35.0, "alvo_pontos": 35.0, "preco_referencia": 130_005.0,
                                 "ancora_t0": None})


# ================================================================================================== sombra ===
def test_sombra_sem_gestao_copia_a_entrada_ancorada_sem_gestao_nem_previsao():
    s = BracoSombra("sombra_sem_gestao", "planejador", "sem_gestao")
    ref = entrada_ajustada()
    it = s.derivar(ref, "c0:sombra_sem_gestao")
    assert it.id == "c0:sombra_sem_gestao" and it.braco == "sombra_sem_gestao"
    for campo in ("acao", "quantidade", "stop_pontos", "alvo_pontos", "tempo_max_s", "preco_referencia",
                  "validade_ns", "t0_ns", "modo"):
        assert getattr(it, campo) == getattr(ref, campo), campo
    assert it.gestao is None and it.ajuste is None and it.p_alvo is None and it.ancora_t0 is None
    assert it.plano is None  # executa no mesmo t, sem armar plano proprio


def test_sombra_trailing_fixo_usa_a_regra_pre_registrada_em_multiplos_do_stop():
    s = BracoSombra("sombra_trailing_fixo", "planejador", "trailing_fixo")
    ref = entrada_ajustada()
    g = s.derivar(ref, "x").gestao
    assert g is not None and [r.tipo for r in g.regras] == ["breakeven", "trailing"]
    be, tr = g.regras
    assert be.mfe_pontos == REGRA_TRAILING_FIXO["breakeven_mfe_x_stop"] * 35.0 and be.folga_pontos == 0.0
    assert tr.mfe_pontos == 35.0 and tr.distancia_pontos == 35.0
    assert g.passo_min_pontos == ref.gestao.passo_min_pontos and g.reavaliar_se == ()  # nunca chama IA
    assert s.manifesto()["regra"] == REGRA_TRAILING_FIXO


def test_sombra_so_replica_entradas_e_nunca_decide():
    s = BracoSombra("sombra_sem_gestao", "planejador")
    with pytest.raises(ValueError):
        s.derivar(Intencao(id="x", braco="p", acao=Acao.ENCERRAR, validade_ns=0), "y")
    with pytest.raises(ValueError):
        BracoSombra("s", "planejador", "outra")  # type: ignore[arg-type]
    assert s.passivo and not s.ia and s.familia == "sombra"


async def test_sombra_adecidir_e_sempre_nao_operar():
    r = await BracoSombra("sombra_sem_gestao", "planejador").adecidir(ctx_basico())
    assert r.resultado == "nao_operar" and r.intencao is None and r.chamadas == []


def test_sombras_e_controle_nao_sao_espelhaveis():
    for b in (BracoSombra("sombra_sem_gestao", "planejador"), BracoAleatorioPlano()):
        with pytest.raises(EspelhoRecusado):
            validar_braco_espelhavel(b)


# ======================================================================================= controle do plano ===
def test_espelhar_condicao_inverte_rompimentos_e_toques():
    assert espelhar_condicao(Condicao(tipo="preco_rompe_acima", valor=5.0)) == Condicao(tipo="preco_rompe_abaixo",
                                                                                         valor=-5.0)
    assert espelhar_condicao(Condicao(tipo="preco_rompe_abaixo", valor=-10.0)) == Condicao(
        tipo="preco_rompe_acima", valor=10.0)
    assert espelhar_condicao(Condicao(tipo="preco_toca", valor=-15.0)) == Condicao(tipo="preco_toca", valor=15.0)
    j = Condicao(tipo="janela_tempo", valor=90.0)
    assert espelhar_condicao(j) == j
    sp = Condicao(tipo="spread_max_pontos", valor=10.0)
    assert espelhar_condicao(sp) == sp


async def test_controle_sem_plano_observado_nao_opera():
    r = await BracoAleatorioPlano(seed=1).adecidir(ctx_basico())
    assert r.resultado == "nao_operar" and r.motivos == ("sorteio_sem_entrada",)


async def _rodar(seed: int, n: int = 12) -> list[tuple[str, Acao, tuple]]:
    b = BracoAleatorioPlano(seed=seed)
    out = []
    for i in range(n):
        b.observar_plano(compilado(gatilhos=(Condicao(tipo="preco_toca", valor=-10.0),)))
        r = await b.adecidir(ctx_basico(ciclo=f"c{i}", t0=T0 + i * 60 * NS))
        assert r.resultado == "plano"
        out.append((r.intencao.id, r.acao, tuple((c.tipo, c.valor) for c in r.intencao.plano.gatilhos)))
    return out


async def test_controle_copia_o_plano_inteiro_e_sorteia_o_lado_reprodutivel_pela_seed():
    a, b, c = await _rodar(7), await _rodar(7), await _rodar(8)
    assert a == b and a != c
    lados = {x[1] for x in a}
    assert lados == {Acao.COMPRAR, Acao.VENDER}  # os dois lados aparecem
    for _, acao, gat in a:
        assert gat == ((("preco_toca", -10.0),) if acao == Acao.COMPRAR else (("preco_toca", 10.0),))


async def test_controle_replica_gestao_tempo_quantidade_e_faixa_com_lado_trocado():
    c = compilado(q=2)
    b = BracoAleatorioPlano(seed=0)
    achou_trocado = False
    for i in range(20):
        b.observar_plano(c)
        t = T0 + i * 60 * NS
        r = await b.adecidir(ctx_basico(ciclo=f"c{i}", t0=t))
        it = r.intencao
        assert it.quantidade == 2 and it.stop_pontos == 30.0 and it.alvo_pontos == 40.0 and it.tempo_max_s == 120
        assert it.gestao.regras == c.gestao.regras and it.gestao.reavaliar_se == () and it.gestao.reavaliar_em_s is None
        assert it.ancora_t0 == 130_000.0 and it.ajuste is None and it.p_alvo is None and it.modo == "condicional"
        assert it.plano.expira_ns == t + 90 * NS and it.validade_ns == it.plano.expira_ns
        inv = {(x.tipo, x.valor) for x in it.plano.invalidacoes}
        if it.acao == Acao.VENDER:  # lado trocado: faixa espelhada
            achou_trocado = True
            assert inv == {("preco_rompe_abaixo", -5.0), ("preco_rompe_acima", 10.0)} and r.proposta["lado_trocado"]
        else:
            assert inv == {("preco_rompe_acima", 5.0), ("preco_rompe_abaixo", -10.0)}
    assert achou_trocado


async def test_controle_uma_entrada_por_plano_observado_e_nunca_com_posicao():
    b = BracoAleatorioPlano(seed=2)
    b.observar_plano(compilado())
    pos = Posicao(simbolo="WINZ26", lado=Lado.COMPRA, quantidade=1, ticket=3)
    r = await b.adecidir(ctx_basico(posicao=pos))
    assert r.resultado == "nao_operar" and r.motivos == ("posicao_aberta",)
    assert (await b.adecidir(ctx_basico(ciclo="c2"))).resultado == "plano"
    assert (await b.adecidir(ctx_basico(ciclo="c3"))).resultado == "nao_operar"  # fila vazia


async def test_controle_com_frequencia_dinamica_sorteia_entre_os_planos_observados():
    b = BracoAleatorioPlano(seed=4, frequencia_fn=lambda t: 1e6)  # taxa altissima: entra sempre que puder
    b.observar_plano(compilado())
    await b.adecidir(ctx_basico(ciclo="c0"))  # primeiro ciclo: dt = 0 => p = 0
    r = await b.adecidir(ctx_basico(ciclo="c1", t0=T0 + 60 * NS))
    assert r.resultado == "plano"
    assert b.manifesto()["frequencia"] == "taxa"
    assert BracoAleatorioPlano().manifesto()["frequencia"] == "um_por_plano_observado"
