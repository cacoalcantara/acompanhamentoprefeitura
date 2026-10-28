from __future__ import annotations

import pytest
from perfis_teste import WIN
from pydantic import ValidationError

from robowin.core.tipos import Acao, Condicao, PlanoCondicional, PropostaIA, RevisaoIA
from robowin.ia.dossie import montar_dossie
from robowin.ia.validadores import (
    EXPIRA_MAX_NS,
    plano_para_absoluto,
    revisao_aprova,
    validar_frescor,
    validar_plano,
    validar_plano_bruto,
)
from robowin.ia.validadores import (
    validar_proposta as _validar_proposta,
)


def validar_proposta(*a, **k):
    k.setdefault("stop_max_pontos", WIN.stop_max_pontos)
    return _validar_proposta(*a, **k)


S = 1_000_000_000
T0 = 5_000 * S


def _dossie(posicao=None):
    ticks = [(T0 - (999 - i) * S, 131_000.0 + i * 0.2) for i in range(1000)]
    return montar_dossie(t0_ns=T0, ticks=ticks, spread_pontos=5.0, tick_pontos=5.0, regime="tendencia", posicao=posicao,
                         limites={"contratos_restantes": 1})


def _prop(**kw):
    base = dict(acao=Acao.COMPRAR, hipoteses=("fluxo comprador",), evidencias_a_favor=("movimento.ret_60s_pts positivo",),
                evidencias_contra=("microestrutura.spread_ticks alto",), riscos=("reversao",), incerteza=0.5,
                stop_pontos=80.0, alvo_pontos=120.0, validade_s=60, invalidacoes=("movimento.ret_10s_pts < 0",),
                justificativa_curta="alta curta")
    base.update(kw)
    return PropostaIA(**base)


def test_proposta_valida():
    r = validar_proposta(_prop(), _dossie())
    assert r.ok, r.violacoes


def test_evidencia_inexistente_no_dossie():
    r = validar_proposta(_prop(evidencias_a_favor=("movimento.campo_que_nao_existe = 3",)), _dossie())
    assert not r.ok and any("inexistente" in v for v in r.violacoes)


def test_evidencia_sem_referencia_a_campo():
    r = validar_proposta(_prop(evidencias_a_favor=("acho que vai subir",)), _dossie())
    assert not r.ok and any("sem referencia" in v for v in r.violacoes)


def test_entrada_exige_evidencia_stop_e_alvo():
    r = validar_proposta(_prop(evidencias_a_favor=(), stop_pontos=None, alvo_pontos=None), _dossie())
    assert any("evidencias a favor" in v for v in r.violacoes)
    assert any("stop" in v for v in r.violacoes) and any("alvo" in v for v in r.violacoes)
    assert not validar_proposta(_prop(stop_pontos=999.0), _dossie(), stop_max_pontos=300.0).ok


def test_nao_operar_nao_exige_stop():
    r = validar_proposta(PropostaIA(acao=Acao.NAO_OPERAR, incerteza=0.9), _dossie())
    assert r.ok, r.violacoes


COMPRADA = {"lado": "compra", "quantidade": 1, "preco_medio": 131_050.0, "aberta_ns": T0 - 120 * S}


def test_coerencia_com_a_posicao_nunca_aumentar():
    d = _dossie(COMPRADA)
    assert not validar_proposta(_prop(acao=Acao.COMPRAR), d).ok  # aumentar
    assert not validar_proposta(_prop(acao=Acao.VENDER), d).ok   # inverter
    assert validar_proposta(PropostaIA(acao=Acao.ENCERRAR), d).ok
    assert validar_proposta(PropostaIA(acao=Acao.MANTER), d).ok
    zerado = _dossie()
    assert not validar_proposta(PropostaIA(acao=Acao.ENCERRAR), zerado).ok
    assert not validar_proposta(PropostaIA(acao=Acao.MANTER), zerado).ok


@pytest.mark.parametrize("v", [0, -5, 61])
def test_validade_fora_da_faixa(v):
    assert not validar_proposta(_prop(validade_s=v), _dossie()).ok


@pytest.mark.parametrize("texto", ["RSI em 70", "cruzamento de medias moveis", "MACD positivo", "bandas de Bollinger",
                                   "media movel exponencial", "estocastico sobrevendido", "ATR alto"])
def test_indicadores_tecnicos_proibidos(texto):
    r = validar_proposta(_prop(hipoteses=(texto,)), _dossie())
    assert not r.ok and any("indicador" in v for v in r.violacoes)


def test_plano_dsl_permitida():
    plano = PlanoCondicional(gatilhos=(Condicao(tipo="preco_rompe_acima", valor=20.0),
                                       Condicao(tipo="spread_max_pontos", valor=10.0)),
                             invalidacoes=(Condicao(tipo="janela_tempo", valor=120.0),), expira_ns=180 * S)
    assert validar_plano(plano) == []
    p = _prop(plano=plano)
    assert validar_proposta(p, _dossie(), modo="condicional").ok
    assert not validar_proposta(p, _dossie(), modo="imediato").ok  # plano so no modo condicional


def test_plano_invalido():
    assert validar_plano(PlanoCondicional(gatilhos=(), expira_ns=S))
    assert validar_plano(PlanoCondicional(gatilhos=(Condicao(tipo="preco_toca"),), expira_ns=S))  # sem valor
    assert validar_plano(PlanoCondicional(gatilhos=(Condicao(tipo="preco_toca", valor=1.0),), expira_ns=EXPIRA_MAX_NS + 1))
    assert validar_plano(PlanoCondicional(gatilhos=(Condicao(tipo="janela_tempo", valor=-1.0),), expira_ns=S))


def test_dsl_sem_construcao_de_indicador():
    # o tipo Condicao e fechado (Literal): indicador nem constroi
    with pytest.raises(ValidationError):
        Condicao(tipo="rsi_abaixo", valor=30.0)
    # e o validador bruto acusa tipos e campos fora da DSL
    assert validar_plano_bruto({"gatilhos": [{"tipo": "cruzamento_medias", "valor": 1}]})
    assert validar_plano_bruto({"gatilhos": [{"tipo": "preco_toca", "valor": 1, "formula": "ema(9)"}]})
    assert validar_plano_bruto({"gatilhos": [{"tipo": "preco_toca", "valor": 1}], "invalidacoes": []}) == []


def test_plano_vira_absoluto_em_t0():
    plano = PlanoCondicional(gatilhos=(Condicao(tipo="janela_tempo", valor=60.0, ate_ns=30 * S),), expira_ns=180 * S)
    ab = plano_para_absoluto(plano, T0)
    assert ab.expira_ns == T0 + 180 * S and ab.gatilhos[0].ate_ns == T0 + 30 * S


def test_frescor():
    ok = validar_frescor(t0_ns=T0, agora_ns=T0 + 30 * S, validade_s=60, preco_ref=100.0, preco_atual=110.0, desvio_max_pts=20)
    assert ok.ok
    assert not validar_frescor(t0_ns=T0, agora_ns=T0 + 61 * S, validade_s=60, preco_ref=100.0, preco_atual=100.0, desvio_max_pts=20).ok
    assert not validar_frescor(t0_ns=T0, agora_ns=T0 + 1 * S, validade_s=60, preco_ref=100.0, preco_atual=125.0, desvio_max_pts=20).ok


def test_revisao_aprova():
    assert revisao_aprova(RevisaoIA(veredito="APROVAR"))
    assert not revisao_aprova(RevisaoIA(veredito="REJEITAR"))
    assert not revisao_aprova(RevisaoIA(veredito="APROVAR", dados_ok=False))
    assert not revisao_aprova(RevisaoIA(veredito="APROVAR", afirmacoes_sem_suporte=("noticia inventada",)))
    assert not revisao_aprova(None)
