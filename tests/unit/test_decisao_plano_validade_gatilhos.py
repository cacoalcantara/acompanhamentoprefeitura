"""DSL e ciclo de vida do plano, validade/falha segura e gatilhos (cadencia derivada do orcamento)."""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from test_decisao_comum import NS, T0, plano_rel

from robowin.core.tipos import (
    Acao,
    ChamadaLLM,
    Condicao,
    Intencao,
    Lado,
    PlanoCondicional,
    Posicao,
)
from robowin.decisao.dsl_plano import (
    EstadoPlano,
    condicao_satisfeita,
    gatilhos_satisfeitos,
    invalidacao_ativa,
    validar_dsl,
    validar_dsl_absoluto,
)
from robowin.decisao.gatilhos import ConfigGatilhos, EstadoGatilho, GerenciadorGatilhos
from robowin.decisao.plano_condicional import (
    AssinaturaPosicao,
    GerenciadorPlano,
    converter_plano,
    niveis_recalculados,
)
from robowin.decisao.saidas import GestorSaidas
from robowin.decisao.validade import (
    PRAZOS_CONDICIONAL,
    PRAZOS_IMEDIATO,
    BloqueioDados,
    classificar_chamada,
    prazos_de_config,
    validar_frescor_entrada,
)
from robowin.ia.orcamento import OrcamentoIA
from robowin.ia.validadores import TIPOS_CONDICAO, validar_plano_bruto

REF = 130_000.0


def est(preco, t=T0, spread=5.0):
    return EstadoPlano(t_ns=t, preco=preco, spread_pts=spread)


# ------------------------------------------------------------------------------------------------ DSL ---
@pytest.mark.parametrize("tipo,valor,preco,esperado", [
    ("preco_toca", -10.0, 129_990.0, True), ("preco_toca", -10.0, 129_991.0, False),
    ("preco_toca", -10.0, 129_950.0, True),  # atravessou o nivel
    ("preco_toca", 10.0, 130_010.0, True), ("preco_toca", 10.0, 130_005.0, False),
    ("preco_toca", 0.0, 130_000.0, True), ("preco_toca", 0.0, 130_010.0, False),
    ("preco_rompe_acima", 20.0, 130_025.0, True), ("preco_rompe_acima", 20.0, 130_020.0, False),
    ("preco_rompe_abaixo", -20.0, 129_975.0, True), ("preco_rompe_abaixo", -20.0, 129_980.0, False),
])
def test_condicoes_de_preco_relativas_a_referencia(tipo, valor, preco, esperado):
    c = Condicao(tipo=tipo, valor=valor)
    assert condicao_satisfeita(c, ref=REF, est=est(preco), t_armado_ns=T0) is esperado


def test_spread_e_janela_de_tempo_como_gatilho_e_como_invalidacao():
    assert condicao_satisfeita(Condicao(tipo="spread_max_pontos", valor=10.0), ref=REF, est=est(REF, spread=10.0),
                               t_armado_ns=T0)
    assert not condicao_satisfeita(Condicao(tipo="spread_max_pontos", valor=10.0), ref=REF, est=est(REF, spread=15.0),
                                   t_armado_ns=T0)
    jan = Condicao(tipo="janela_tempo", valor=30.0)
    dentro, fora = est(REF, T0 + 20 * NS), est(REF, T0 + 40 * NS)
    assert condicao_satisfeita(jan, ref=REF, est=dentro, t_armado_ns=T0)  # gatilho: so dentro da janela
    assert not condicao_satisfeita(jan, ref=REF, est=fora, t_armado_ns=T0)
    assert not condicao_satisfeita(jan, ref=REF, est=dentro, t_armado_ns=T0, papel="invalidacao")
    assert condicao_satisfeita(jan, ref=REF, est=fora, t_armado_ns=T0, papel="invalidacao")  # janela acabou
    com_ate = Condicao(tipo="janela_tempo", valor=100.0, ate_ns=T0 + 10 * NS)  # o menor limite vale
    assert not condicao_satisfeita(com_ate, ref=REF, est=est(REF, T0 + 20 * NS), t_armado_ns=T0)


def test_todos_os_gatilhos_e_qualquer_invalidacao():
    p = PlanoCondicional(
        gatilhos=(Condicao(tipo="preco_toca", valor=-10.0), Condicao(tipo="spread_max_pontos", valor=5.0)),
        invalidacoes=(Condicao(tipo="preco_rompe_acima", valor=50.0), Condicao(tipo="preco_rompe_abaixo", valor=-50.0)),
        expira_ns=T0 + 100 * NS)
    assert not gatilhos_satisfeitos(p, ref=REF, est=est(129_990.0, spread=10.0), t_armado_ns=T0)  # spread alto
    assert gatilhos_satisfeitos(p, ref=REF, est=est(129_990.0, spread=5.0), t_armado_ns=T0)
    assert invalidacao_ativa(p, ref=REF, est=est(129_940.0), t_armado_ns=T0).tipo == "preco_rompe_abaixo"
    assert invalidacao_ativa(p, ref=REF, est=est(130_000.0), t_armado_ns=T0) is None


def test_dsl_so_aceita_a_lista_permitida_sem_construcao_de_indicador():
    assert set(TIPOS_CONDICAO) == {"preco_toca", "preco_rompe_acima", "preco_rompe_abaixo", "spread_max_pontos",
                                   "janela_tempo"}
    ruim = {"gatilhos": [{"tipo": "cruza_media", "valor": 9}], "invalidacoes": [], "expira_ns": NS}
    assert validar_plano_bruto(ruim)
    assert condicao_satisfeita.__module__.endswith("dsl_plano")
    # a DSL nao importa nada de features/indicadores: so tipos do nucleo
    arvore = ast.parse(Path(__import__("robowin.decisao.dsl_plano", fromlist=["x"]).__file__).read_text())
    mods = {n.module for n in ast.walk(arvore) if isinstance(n, ast.ImportFrom)}
    assert not any("feature" in (m or "") or "modelos" in (m or "") for m in mods)
    assert validar_dsl(PlanoCondicional(gatilhos=(), expira_ns=NS))  # plano sem gatilhos
    assert validar_dsl(PlanoCondicional(gatilhos=(Condicao(tipo="preco_toca", valor=1.0),), expira_ns=700 * NS))


def test_conversao_de_duracoes_em_instantes_absolutos():
    rel = PlanoCondicional(gatilhos=(Condicao(tipo="janela_tempo", valor=30.0, ate_ns=20 * NS),),
                           invalidacoes=(Condicao(tipo="janela_tempo", valor=60.0, ate_ns=40 * NS),),
                           expira_ns=90 * NS)
    ab = converter_plano(rel, T0)
    assert ab.expira_ns == T0 + 90 * NS
    assert ab.gatilhos[0].ate_ns == T0 + 20 * NS and ab.invalidacoes[0].ate_ns == T0 + 40 * NS
    assert not validar_dsl_absoluto(ab, T0)
    assert validar_dsl_absoluto(rel, T0)  # duracoes cruas (relativas) nao servem como instantes


# ------------------------------------------------------------------------------- ciclo de vida do plano --
def base_intencao(acao=Acao.COMPRAR):
    return Intencao(id="i1", braco="a", acao=acao, stop_pontos=30.0, alvo_pontos=40.0, tempo_max_s=60,
                    validade_ns=T0 + 60 * NS, modo="condicional", proposta_id="p1", preco_referencia=REF, t0_ns=T0)


def armar(g: GerenciadorPlano, plano=None, t=T0, pos=None):
    plano = converter_plano(plano or plano_rel(-10.0, 120, 50.0), T0)
    return g.armar(base_intencao(), plano, ref=REF, t0_ns=T0, t_ns=t,
                   assinatura=AssinaturaPosicao.de(pos or Posicao(simbolo="W")))


def test_armar_disparar_registra_idade_e_recalcula_o_stop():
    g = GerenciadorPlano("a")
    evs = armar(g, t=T0 + 2 * NS)
    assert [e.tipo for e in evs] == ["armado"] and g.armado is not None
    ev = g.avaliar(est(129_990.0, T0 + 12 * NS), assinatura=AssinaturaPosicao())
    assert ev.tipo == "disparado" and ev.idade_ns == 10 * NS and g.armado is None
    it = g.intencao_disparo(ev, preco_real=129_995.0, t_ns=T0 + 12 * NS, novo_id="i1:disp")
    assert it.preco_referencia == 129_995.0 and it.plano is None and it.id == "i1:disp"
    assert it.stop_pontos == 30.0 and it.alvo_pontos == 40.0  # distancias preservadas
    assert niveis_recalculados(True, 129_995.0, 30.0, 40.0) == (129_965.0, 130_035.0)
    assert niveis_recalculados(False, 129_995.0, 30.0, 40.0) == (130_025.0, 129_955.0)


@pytest.mark.parametrize("kw,motivo", [({"kill_ativo": True}, "kill_switch"), ({"janela_evento": True}, "janela_de_evento")])
def test_cancelamentos_por_kill_e_janela_de_evento(kw, motivo):
    g = GerenciadorPlano("a")
    armar(g)
    ev = g.avaliar(est(129_990.0), assinatura=AssinaturaPosicao(), **kw)  # mesmo com o gatilho satisfeito
    assert ev.tipo == "cancelado" and ev.motivo == motivo and g.armado is None


def test_cancelamento_por_mudanca_de_posicao():
    g = GerenciadorPlano("a")
    armar(g)
    nova = AssinaturaPosicao.de(Posicao(simbolo="W", lado=Lado.COMPRA, quantidade=1, ticket=9))
    ev = g.avaliar(est(129_990.0), assinatura=nova)
    assert ev.tipo == "cancelado" and ev.motivo == "mudanca_de_posicao"


def test_expiracao_e_invalidacao_e_plano_ja_expirado_recusado():
    g = GerenciadorPlano("a")
    armar(g, plano_rel(-100.0, 5, None))
    assert g.avaliar(est(130_000.0, T0 + 4 * NS), assinatura=AssinaturaPosicao()) is None
    assert g.avaliar(est(130_000.0, T0 + 6 * NS), assinatura=AssinaturaPosicao()).tipo == "expirado"
    g2 = GerenciadorPlano("a")
    armar(g2, plano_rel(-100.0, 120, 50.0))
    assert g2.avaliar(est(130_060.0), assinatura=AssinaturaPosicao()).tipo == "invalidado"
    with pytest.raises(ValueError):
        armar(GerenciadorPlano("a"), plano_rel(-10.0, 5, None), t=T0 + 10 * NS)


def test_um_plano_armado_por_braco():
    g = GerenciadorPlano("a")
    armar(g)
    evs = armar(g, plano_rel(-20.0, 60, None), t=T0 + NS)
    assert [e.tipo for e in evs] == ["substituido", "armado"] and g.armado.plano.gatilhos[0].valor == -20.0


# ----------------------------------------------------------------------------------- validade / falhas ---
@pytest.mark.parametrize("erro,cat,esperado", [
    ("timeout", None, "resposta_tardia"), ("http_429", None, "erro_http"), ("http_529", None, "erro_http"),
    ("recusa", "cyber", "recusa"), ("max_tokens", None, "max_tokens"), ("json_invalido", None, "resposta_invalida"),
    ("modelo_servido_fora_da_lista:gpt-x", None, "modelo_fora_da_lista"),
    ("orcamento_esgotado:teto_mensal", None, "orcamento_esgotado"), ("cache_miss", None, "cache_miss"),
    ("ValueError: boom", None, "erro_provedor"),
])
def test_toda_falha_de_ia_vira_motivo_normalizado(erro, cat, esperado):
    ch = ChamadaLLM(papel="proponente", modelo_pedido="x:y", valida=False, erro=erro, categoria_recusa=cat)
    assert classificar_chamada(ch) == esperado
    assert classificar_chamada(ChamadaLLM(papel="p", modelo_pedido="x:y", valida=True)) is None


def test_prazos_pre_registrados_e_margem_do_revisor():
    assert (PRAZOS_IMEDIATO.claude_s, PRAZOS_IMEDIATO.gpt_s, PRAZOS_IMEDIATO.validade_s) == (40.0, 12.0, 60.0)
    assert PRAZOS_IMEDIATO.desvio_max_pts == 20.0 and PRAZOS_IMEDIATO.margem_s >= 8.0
    assert PRAZOS_IMEDIATO.prazo_revisor(0.0) == 12.0  # GPT: 12 s
    assert PRAZOS_IMEDIATO.prazo_revisor(45.0) == pytest.approx(7.0)  # sobra: 60 - 8 - 45
    assert PRAZOS_IMEDIATO.prazo_revisor(55.0) < 0  # sem tempo para revisar
    assert PRAZOS_CONDICIONAL.validade_s == 600.0
    pz = prazos_de_config({"prazos": {"imediato": {"claude_s": 40, "gpt_s": 12, "validade_s": 60, "desvio_max_pts": 20},
                                      "condicional": {"expira_min_max": 10}}})
    assert pz["imediato"].claude_s == 40.0 and pz["condicional"].validade_s == 600.0


def test_frescor_validade_e_desvio():
    ok = validar_frescor_entrada(t0_ns=T0, agora_ns=T0 + 30 * NS, validade_s=60, preco_ref=REF, preco_atual=REF + 15,
                                 desvio_max_pts=20)
    assert ok.ok
    tarde = validar_frescor_entrada(t0_ns=T0, agora_ns=T0 + 61 * NS, validade_s=60, preco_ref=REF, preco_atual=REF,
                                    desvio_max_pts=20)
    assert not tarde.ok and "vencida" in tarde.violacoes[0]
    desv = validar_frescor_entrada(t0_ns=T0, agora_ns=T0 + NS, validade_s=60, preco_ref=REF, preco_atual=REF + 25,
                                   desvio_max_pts=20)
    assert not desv.ok and "desvio" in desv.violacoes[0]


def test_bloqueio_de_dados_lacuna_e_dado_velho():
    b = BloqueioDados(retencao_lacuna_s=30.0, idade_max_dado_s=15.0)
    b.dado_recebido(T0)
    assert b.motivo(T0 + 5 * NS) == ""
    assert b.motivo(T0 + 20 * NS) == "dado_velho"  # dado mais velho que a idade maxima
    b.dado_recebido(T0 + 20 * NS)  # o buraco entre 2 dados tambem e tratado como lacuna
    assert b.motivo(T0 + 21 * NS) == "dado_velho"
    for k in range(25, 56, 5):  # dados voltam a fluir; a retencao de 30 s ainda segura as entradas
        b.dado_recebido(T0 + k * NS)
    assert b.motivo(T0 + 40 * NS) == "dado_velho" and b.motivo(T0 + 55 * NS) == ""
    b.lacuna_sequencia(T0 + 56 * NS, "seq 10->12")
    assert b.motivo(T0 + 57 * NS) == "lacuna_sequencia" and b.lacunas == 1
    for k in range(60, 91, 5):
        b.dado_recebido(T0 + k * NS)
    assert b.motivo(T0 + 91 * NS) == ""  # retencao de 30 s depois da lacuna


def test_gestor_de_saidas_uma_reivindicacao_por_posicao():
    g = GestorSaidas()
    assert g.reivindicar("a", 1, "tempo", T0) and not g.reivindicar("a", 1, "zeragem", T0 + 1)
    assert g.dono("a").motivo == "tempo" and g.n_recusadas == 1
    assert g.reivindicar("b", 7, "encerrar", T0)  # outro braco e independente
    g.liberar("a")
    assert g.reivindicar("a", 2, "kill", T0 + 2)  # nova posicao, novo dono


# ----------------------------------------------------------------------------------------- gatilhos -----
def gat(**kw):
    base = {"periodo_s": 240.0, "intervalo_min_s": 10.0, "rearme_evento_s": 60.0, "e_brl_min": 1.0,
            "z_statarb_min": 2.0, "max_chamadas_dia": None}
    base.update(kw)
    return ConfigGatilhos(**base)


def test_gatilho_periodico_e_intervalo_minimo_fixo():
    g = GerenciadorGatilhos(gat())
    d = g.avaliar(EstadoGatilho(t_ns=T0))
    assert [x.nome for x in d] == ["periodico"]
    g.marcar_chamada(T0)
    assert g.avaliar(EstadoGatilho(t_ns=T0 + 5 * NS, z_statarb=9.0)) == ()  # < 10 s: nada, nem por evento
    assert g.avaliar(EstadoGatilho(t_ns=T0 + 100 * NS)) == ()  # antes do periodo
    assert [x.nome for x in g.avaliar(EstadoGatilho(t_ns=T0 + 241 * NS))] == ["periodico"]


def test_gatilhos_por_evento_modelo_zscore_noticia_e_posicao():
    g = GerenciadorGatilhos(gat())
    g.marcar_chamada(T0)
    t = T0 + 30 * NS
    nomes = lambda **kw: [x.nome for x in g.avaliar(EstadoGatilho(t_ns=t, **kw))]  # noqa: E731
    assert nomes(modelo_saida={"e_brl_compra": 0.5, "e_brl_venda": -1.0}) == []
    assert nomes(modelo_saida={"e_brl_compra": 0.5, "e_brl_venda": 2.5}) == ["limiar_modelo"]
    assert nomes(z_statarb=-2.4) == ["zscore_statarb"]
    assert nomes(noticias_novas=[0.3]) == [] and nomes(noticias_novas=[0.3, 0.9]) == ["noticia_relevante"]
    assert nomes(eventos_posicao=["abriu"]) == ["evento_posicao"]
    assert g.cfg.evento_posicao


def test_evento_de_modelo_so_rearma_depois_do_intervalo():
    g = GerenciadorGatilhos(gat(rearme_evento_s=60.0))
    est1 = EstadoGatilho(t_ns=T0, modelo_saida={"e_brl_compra": 5.0})
    assert [x.nome for x in g.avaliar(est1)] == ["limiar_modelo"]
    g.marcar_chamada(T0)
    est2 = EstadoGatilho(t_ns=T0 + 30 * NS, modelo_saida={"e_brl_compra": 5.0})
    assert g.avaliar(est2) == ()  # ainda dentro do rearme
    est3 = EstadoGatilho(t_ns=T0 + 61 * NS, modelo_saida={"e_brl_compra": 5.0})
    assert [x.nome for x in g.avaliar(est3)] == ["limiar_modelo"]


def test_teto_diario_derivado_do_orcamento_e_cadencia_do_uso_medido():
    orc = OrcamentoIA(21.0, pregoes_no_mes=21)  # cota do dia: US$ 1,00
    orc.iniciar_pregao("2026-11-10", 21)
    g = GerenciadorGatilhos(gat(custo_medio_inicial_usd=0.01), orc)
    assert g.chamadas_restantes(T0) == 100 and g.teto_diario(T0) == 100  # 1,00 / 0,01
    for _ in range(10):  # custo REAL medido de US$ 0,05 por ciclo derruba o teto
        g.registrar_custo(0.05)
    assert g.custo_medio_usd > 0.04 and g.chamadas_restantes(T0) < 25
    # cadencia: poucas chamadas restantes => intervalo maior que o periodo base
    p = g.intervalo_periodico_s(T0, segundos_restantes=7200.0)
    assert p >= 7200.0 / g.chamadas_restantes(T0) and p >= 240.0
    # gasto do dia consome a verba: sem verba => nenhum gatilho
    r = orc.reservar(0.9)
    orc.confirmar(r, 0.9)
    assert g.chamadas_restantes(T0) <= 2
    r = orc.reservar(0.1)
    orc.confirmar(r, 0.1)
    assert g.chamadas_restantes(T0) == 0 and g.avaliar(EstadoGatilho(t_ns=T0)) == ()
    assert g.bloqueios_orcamento == 1 and math.isinf(g.intervalo_periodico_s(T0, 3600.0))


def test_teto_fixo_de_chamadas_por_dia():
    g = GerenciadorGatilhos(gat(max_chamadas_dia=3, intervalo_min_s=1.0, periodo_s=1.0))
    for i in range(3):
        assert g.avaliar(EstadoGatilho(t_ns=T0 + i * 5 * NS, segundos_restantes=1.0))
        g.marcar_chamada(T0 + i * 5 * NS)
    assert g.avaliar(EstadoGatilho(t_ns=T0 + 99 * NS, segundos_restantes=1.0)) == ()
    amanha = T0 + 24 * 3600 * NS
    assert g.avaliar(EstadoGatilho(t_ns=amanha, segundos_restantes=1.0))  # novo dia, novo teto
