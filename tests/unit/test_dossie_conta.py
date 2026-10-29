"""Secoes da conta do dossie do planejador (docs/16 WP5): custos causais, envelope, posicao relativa, legado v1."""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from perfis_teste import WIN

from robowin.avaliacao.relatorio import SLIPPAGE_ESTRESSE_TICKS, custos_do_perfil
from robowin.core.tipos import (
    Acao,
    ConfigPlanejador,
    ConfigRisco,
    EnvelopeDecisao,
    Intencao,
    Lado,
    Posicao,
    RegraGestao,
)
from robowin.decisao.compilador_plano import compilar_gestao
from robowin.decisao.dossie_conta import (
    FONTE_CAUSAL,
    FONTE_RESERVA,
    custos_publicos,
    limites_legado,
    secoes_planejador,
)
from robowin.decisao.gestao import atualizar_excursao, iniciar_estado

TICK = WIN.tick_pontos
NS = 1_000_000_000
T0 = 7_000 * NS
ENT = 130_000.0
ENV = EnvelopeDecisao(unidades_max=1, stop_max_pontos_por_q=(40 * TICK,), stop_min_pontos=2 * TICK,
                      dist_min_ajuste_pontos=TICK, tempo_max_s=300, entradas_possiveis=True)


def test_custos_com_reserva_do_perfil_quando_poucas_amostras():
    c = custos_publicos(WIN, spread_pts=TICK, slippages_stop_anteriores=[TICK] * 29)
    base, estresse = custos_do_perfil(WIN)
    assert c.fonte_slippage == FONTE_RESERVA
    assert c.slippage_stop_medio_pontos == pytest.approx(SLIPPAGE_ESTRESSE_TICKS * TICK)
    assert c.ida_volta_pontos_base == pytest.approx(base / WIN.ponto_valor_brl, abs=1e-6)
    assert c.ida_volta_pontos_estresse == pytest.approx(estresse / WIN.ponto_valor_brl, abs=1e-6)
    assert c.meio_spread_pontos == TICK / 2


def test_custos_com_media_causal():
    amostras = [TICK] * 20 + [3 * TICK] * 10 + [math.nan]
    c = custos_publicos(WIN, spread_pts=2 * TICK, slippages_stop_anteriores=amostras)
    assert c.fonte_slippage == FONTE_CAUSAL
    assert c.slippage_stop_medio_pontos == pytest.approx((20 * TICK + 30 * TICK) / 30)


def test_custos_nunca_usam_params_slippage_do_simulador():
    src = Path(__file__).resolve().parents[2] / "src" / "robowin" / "decisao" / "dossie_conta.py"
    arv = ast.parse(src.read_text(encoding="utf-8"))
    nomes = {n.id for n in ast.walk(arv) if isinstance(n, ast.Name)}
    nomes |= {a.name for n in ast.walk(arv) if isinstance(n, ast.ImportFrom) for a in n.names}
    mods = {n.module or "" for n in ast.walk(arv) if isinstance(n, ast.ImportFrom)}
    assert "ParamsSlippage" not in nomes
    assert not any(m.endswith("simulador") or "risco" in m for m in mods)


def _estado_comprado():
    pos = Posicao(simbolo="X", lado=Lado.COMPRA, quantidade=2, preco_medio=ENT, ticket=3, stop=ENT - 10 * TICK,
                  alvo=ENT + 20 * TICK, aberta_ns=T0)
    est = iniciar_estado(pos, Intencao(id="i", braco="p", acao=Acao.COMPRAR, validade_ns=T0), 200)
    atualizar_excursao(est, bid=ENT + 6 * TICK, ask=ENT + 7 * TICK, last=ENT + 6 * TICK, por_cotacao=False)
    est.sl = ENT + TICK  # stop ja apertado: o dossie mostra o ATUAL
    return pos, est


def _secoes(expor="envelope", **kw):
    pos, est = _estado_comprado()
    custos = custos_publicos(WIN, spread_pts=TICK, slippages_stop_anteriores=[])
    gestao = compilar_gestao((RegraGestao(tipo="breakeven", mfe_pontos=4 * TICK),), tick_pontos=TICK,
                             cfg=ConfigPlanejador(), espelhado=False, reavaliar_em_s=None, reavaliar_se=())
    est.regras_disparadas.add(0)
    base = dict(pos=pos, est=est, env=ENV, expor=expor, perfil=WIN, custos=custos,
                sessao={"min_desde_abertura": 60, "de": "09:15", "min_para_zeragem": 300, "perda_max_dia_brl": 1.0},
                mercado={"feed_ok": True, "leilao": False, "vol_extrema": False, "spread_brl": 9.0},
                plano_vigente=None, retorno={"ultimo": "veto:stop_colado_no_preco", "valor": 123.0},
                latencia={"p50_s": 8.123456, "p95_s": 14.0, "x": "y"}, ultimo_preco=ENT + 4 * TICK,
                t_ns=T0 + 50 * NS, gestao=gestao)
    base.update(kw)
    return secoes_planejador(**base)


def test_posicao_em_pontos_de_resultado_com_niveis_atuais():
    d = _secoes()["posicao"]
    assert d["resultado_pontos"] == 4 * TICK
    assert d["stop_resultado_pontos"] == TICK and d["alvo_resultado_pontos"] == 20 * TICK
    assert d["distancia_stop_pontos"] == 3 * TICK and d["distancia_alvo_pontos"] == 16 * TICK
    assert d["mfe_pontos"] == 6 * TICK and d["quantidade_inicial"] == 2
    assert d["tempo_aberta_s"] == 50 and d["tempo_max_s"] == 200 and d["tempo_restante_s"] == 150
    assert d["regras"][0]["tipo"] == "breakeven" and d["regras"][0]["disparada"] is True


def _numeros(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _numeros(v)
    elif isinstance(x, list | tuple):
        for v in x:
            yield from _numeros(v)
    elif isinstance(x, int | float) and not isinstance(x, bool):
        yield float(x)


def test_secoes_sem_preco_absoluto_e_sem_dinheiro_de_limite():
    sec = _secoes()
    txt = json.dumps(sec)
    assert all(abs(v - ENT) > ENT / 2 for v in _numeros(sec))  # nenhum preco absoluto do ativo
    assert "perda_max_dia_brl" not in txt and "spread_brl" not in txt
    assert sec["retorno"] == {"ultimo": "veto:stop_colado_no_preco"}  # so codigos
    assert sec["latencia_ia"] == {"p50_s": 8.123, "p95_s": 14.0}
    assert sec["sessao"] == {"min_desde_abertura": 60, "de": "09:15", "min_para_zeragem": 300}
    assert sec["instrumento"]["stop_dispara_por"] == "ultimo_negocio"
    assert "formula_equilibrio" in sec["custos"]
    assert set(sec) == {"instrumento", "custos", "envelope", "sessao", "mercado", "posicao", "plano_vigente",
                        "retorno", "latencia_ia"}


def test_envelope_completo_ou_reduzido():
    assert tuple(_secoes()["envelope"]["stop_max_pontos_por_q"]) == (40 * TICK,)
    red = _secoes(expor="nenhum")["envelope"]
    assert set(red) == {"entradas_possiveis", "stop_min_pontos", "dist_min_ajuste_pontos"}


def test_sem_posicao():
    sec = _secoes(pos=Posicao(simbolo="X"), est=None)
    assert sec["posicao"] is None


def test_limites_legado_usa_stop_efetivo_q1():
    cfg = ConfigRisco()
    est = SimpleNamespace(pnl_dia_total=-50.0, operacoes_dia=3)
    d = limites_legado(ENV, est, cfg)
    assert d == {"perda_restante_dia_brl": cfg.perda_max_dia_brl - 50.0,
                 "operacoes_restantes": float(cfg.operacoes_dia_max - 3),
                 "contratos_max": float(cfg.contratos_max), "stop_max_pontos": 40 * TICK}
