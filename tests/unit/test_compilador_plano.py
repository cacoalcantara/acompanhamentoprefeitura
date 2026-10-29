"""Compilador do PlanoTradeIA (docs/16 WP5): grade do tick, faixa assimetrica, "agora", gestao e resumo."""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path

import pytest
from perfis_teste import INS_WIN

from robowin.core.tipos import (
    Acao,
    Condicao,
    ConfigPlanejador,
    EnvelopeDecisao,
    PlanoTradeIA,
    RegraGestao,
)
from robowin.decisao.compilador_plano import (
    ErroCompilacao,
    compilar_gestao,
    compilar_plano,
)
from robowin.decisao.dsl_plano import EstadoPlano, gatilhos_satisfeitos, invalidacao_ativa

TICK = INS_WIN.tick_pontos
REF = 130_000.0
T0 = 1_000 * 1_000_000_000
NS = 1_000_000_000
ENV = EnvelopeDecisao(unidades_max=2, stop_max_pontos_por_q=(250.0, 120.0), stop_min_pontos=2 * TICK,
                      dist_min_ajuste_pontos=TICK, tempo_max_s=300, entradas_possiveis=True)
CFG = ConfigPlanejador(ativo=True)


def _plano(**kw) -> PlanoTradeIA:
    base = dict(acao=Acao.COMPRAR, stop_pontos=10 * TICK, alvo_pontos=12 * TICK, tempo_max_s=180,
                faixa_contra_pontos=2 * TICK, faixa_favor_pontos=TICK, expira_s=120, p_alvo=0.4)
    base.update(kw)
    return PlanoTradeIA(**base)


def _compilar(p: PlanoTradeIA, **kw):
    return compilar_plano(p, ref_t0=REF, t0_ns=T0, tick_pontos=TICK, env=ENV, cfg=CFG, braco="planejador",
                          ciclo_id="c1", **kw)


def test_stop_e_alvo_arredondados_para_baixo_na_grade():
    c = _compilar(_plano(stop_pontos=10 * TICK + 0.7 * TICK, alvo_pontos=12 * TICK + 0.99 * TICK))
    assert c.intencao_base.stop_pontos == 10 * TICK
    assert c.intencao_base.alvo_pontos == 12 * TICK
    # exatamente na grade nao muda (sem ruido de float)
    c2 = _compilar(_plano(stop_pontos=10 * TICK, alvo_pontos=12 * TICK))
    assert c2.intencao_base.stop_pontos == 10 * TICK


def test_faixa_da_compra_vira_invalidacoes_relativas():
    c = _compilar(_plano(faixa_contra_pontos=2.4 * TICK, faixa_favor_pontos=1.6 * TICK))
    inv = {x.tipo: x.valor for x in c.plano_abs.invalidacoes}
    assert inv == {"preco_rompe_acima": TICK, "preco_rompe_abaixo": -2 * TICK}  # faixa arredondada para dentro


def test_faixa_da_venda_e_o_espelho():
    c = _compilar(_plano(acao=Acao.VENDER, faixa_contra_pontos=2 * TICK, faixa_favor_pontos=TICK))
    inv = {x.tipo: x.valor for x in c.plano_abs.invalidacoes}
    assert inv == {"preco_rompe_abaixo": -TICK, "preco_rompe_acima": 2 * TICK}


def test_agora_vira_janela_tempo_e_entra_dentro_da_faixa():
    c = _compilar(_plano(gatilhos=()))
    assert c.plano_abs.gatilhos == (Condicao(tipo="janela_tempo", valor=120.0),)
    assert c.plano_abs.expira_ns == T0 + 120 * NS
    dentro = EstadoPlano(t_ns=T0 + NS, preco=REF + TICK)
    fora = EstadoPlano(t_ns=T0 + NS, preco=REF + 2 * TICK)
    kw = dict(ref=REF, t_armado_ns=T0, tick_pontos=TICK)
    assert invalidacao_ativa(c.plano_abs, est=dentro, **kw) is None
    assert gatilhos_satisfeitos(c.plano_abs, est=dentro, **kw)
    assert invalidacao_ativa(c.plano_abs, est=fora, **kw) is not None


def test_gatilhos_da_ia_sao_mantidos_na_grade():
    c = _compilar(_plano(gatilhos=(Condicao(tipo="preco_toca", valor=-2.2 * TICK),
                                   Condicao(tipo="spread_max_pontos", valor=2 * TICK))))
    assert c.plano_abs.gatilhos[0] == Condicao(tipo="preco_toca", valor=-2 * TICK)
    assert c.plano_abs.gatilhos[1].tipo == "spread_max_pontos"


def test_ate_ns_proibido():
    with pytest.raises(ErroCompilacao):
        _compilar(_plano(gatilhos=(Condicao(tipo="janela_tempo", valor=30, ate_ns=10),)))


def test_intencao_base_completa():
    regras = (RegraGestao(tipo="breakeven", mfe_pontos=6 * TICK, folga_ticks=1),)
    c = _compilar(_plano(quantidade=2, gestao=regras, reavaliar_em_s=60, reavaliar_se=("perto_do_stop",)))
    it = c.intencao_base
    assert it.acao == Acao.COMPRAR and it.modo == "condicional" and it.quantidade == 2
    assert it.ancora_t0 == REF and it.preco_referencia == REF and it.t0_ns == T0
    assert it.validade_ns == c.plano_abs.expira_ns and it.tempo_max_s == 180 and it.p_alvo == 0.4
    assert it.gestao == c.gestao and it.id == "c1:planejador"
    assert c.gestao.regras[0].folga_pontos == TICK
    assert c.gestao.reavaliar_em_s == 60 and c.gestao.reavaliar_se == ("perto_do_stop",)


def test_expiracao_limitada_pela_config():
    c = _compilar(_plano(expira_s=10_000))
    assert c.plano_abs.expira_ns == T0 + CFG.expira_max_s * NS


def test_passo_minimo_espelhado():
    assert _compilar(_plano()).gestao.passo_min_pontos == CFG.passo_min_ticks * TICK
    assert _compilar(_plano(), espelhado=True).gestao.passo_min_pontos == CFG.passo_min_ticks_espelhado * TICK


@pytest.mark.parametrize("kw", [
    {"acao": Acao.NAO_OPERAR}, {"acao": Acao.MANTER}, {"stop_pontos": None}, {"alvo_pontos": None},
    {"stop_pontos": math.nan}, {"alvo_pontos": math.inf}, {"stop_pontos": 0.4 * TICK}, {"quantidade": 0},
    {"tempo_max_s": 0}, {"faixa_favor_pontos": math.nan}, {"expira_s": 0},
])
def test_plano_incompilavel(kw):
    with pytest.raises(ErroCompilacao):
        _compilar(_plano(**kw))


def test_resumo_sem_preco_absoluto_e_com_equilibrio():
    c = _compilar(_plano(), custo_ida_volta_pontos=TICK)
    txt = json.dumps(c.resumo)
    assert str(int(REF)) not in txt and "130000" not in txt
    assert c.resumo["acerto_equilibrio"] == round((10 * TICK + TICK) / (22 * TICK), 4)
    assert c.resumo["stop_resultado_pontos"] == -10 * TICK
    assert c.resumo["envelope"]["stop_max_pontos_para_q"] == 250.0


def test_resumo_com_envelope_nenhum_nao_mostra_tetos():
    cfg = ConfigPlanejador(ativo=True, expor_envelope="nenhum")
    c = compilar_plano(_plano(), ref_t0=REF, t0_ns=T0, tick_pontos=TICK, env=ENV, cfg=cfg, braco="p", ciclo_id="c")
    assert set(c.resumo["envelope"]) == {"entradas_possiveis", "stop_min_pontos", "dist_min_ajuste_pontos"}


def test_compilar_gestao_na_grade():
    regras = (RegraGestao(tipo="trailing", mfe_pontos=3.2 * TICK, distancia_pontos=4.7 * TICK),
              RegraGestao(tipo="apertar_apos", apos_s=60, distancia_pontos=0.3 * TICK),
              RegraGestao(tipo="ao_vencer_tempo"))
    g = compilar_gestao(regras, tick_pontos=TICK, cfg=CFG, espelhado=False, reavaliar_em_s=1,
                        reavaliar_se=("perto_do_alvo", "perto_do_alvo", "inexistente"))
    assert g.regras[0].mfe_pontos == 4 * TICK and g.regras[0].distancia_pontos == 4 * TICK
    assert g.regras[1].distancia_pontos == TICK  # nunca menos de um tick
    assert g.regras[2].acao_vencimento == "encerrar"
    assert g.reavaliar_em_s == CFG.reavaliar_min_s and g.reavaliar_se == ("perto_do_alvo",)
    assert g.intervalo_ajuste_s == CFG.intervalo_ajuste_s


@pytest.mark.parametrize("r", [
    RegraGestao(tipo="trailing", mfe_pontos=10.0), RegraGestao(tipo="apertar_apos", distancia_pontos=10.0),
    RegraGestao(tipo="parcial", mfe_pontos=10.0), RegraGestao(tipo="trailing", distancia_pontos=-1.0),
    RegraGestao(tipo="trailing", distancia_pontos=math.inf), RegraGestao(tipo="sem_progresso", mfe_pontos=1.0),
])
def test_regra_incompilavel(r):
    with pytest.raises(ErroCompilacao):
        compilar_gestao((r,), tick_pontos=TICK, cfg=CFG, espelhado=False, reavaliar_em_s=None, reavaliar_se=())


def test_regras_acima_do_maximo():
    regras = tuple(RegraGestao(tipo="breakeven") for _ in range(CFG.max_regras + 1))
    with pytest.raises(ErroCompilacao):
        compilar_gestao(regras, tick_pontos=TICK, cfg=CFG, espelhado=False, reavaliar_em_s=None, reavaliar_se=())


def test_deterministico():
    p = _plano(gestao=(RegraGestao(tipo="breakeven", mfe_pontos=20.0),))
    assert _compilar(p) == _compilar(p)


def test_compilador_nao_importa_risco():
    src = Path(__file__).resolve().parents[2] / "src" / "robowin" / "decisao" / "compilador_plano.py"
    arv = ast.parse(src.read_text(encoding="utf-8"))
    mods = [n.module or "" for n in ast.walk(arv) if isinstance(n, ast.ImportFrom)]
    mods += [a.name for n in ast.walk(arv) if isinstance(n, ast.Import) for a in n.names]
    assert not any("risco" in m for m in mods)
    nomes = {n.id for n in ast.walk(arv) if isinstance(n, ast.Name)}
    nomes |= {a.name for n in ast.walk(arv) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "ConfigRisco" not in nomes
