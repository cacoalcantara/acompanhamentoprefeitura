"""Bracos A (quant), B (proponente), C (proponente + revisor), controles, extras de [[bracos]] e a matriz de
falhas de IA (timeout real, 429, recusa, JSON invalido, modelo servido fora da lista, orcamento esgotado)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_decisao_comum import NS, T0, ctx_basico, dossie_basico, plano_rel

from robowin.core.tipos import Acao, Lado, Posicao, PropostaIA, RevisaoIA
from robowin.decisao.bracos import (
    BracoAleatorio as _BracoAleatorio,
)
from robowin.decisao.bracos import (
    BracoNaoOperar,
    BracoProponente,
    BracoProponenteRevisor,
    ErroConfigBraco,
    Geometria,
    ServicoRevisao,
    extras_de_config,
)
from robowin.decisao.bracos import (
    BracoQuant as _BracoQuant,
)
from robowin.decisao.bracos import (
    FabricaBracos as _FabricaBracos,
)
from robowin.decisao.bracos import (
    ServicoProposta as _ServicoProposta,
)
from robowin.decisao.validade import Prazos
from robowin.ia.catalogo import Catalogo, ErroCatalogo
from robowin.ia.orcamento import OrcamentoIA, ProvedorComOrcamento
from robowin.ia.provedores.base import RespostaBruta
from robowin.ia.provedores.falso import ProvedorFalso

RAIZ = Path(__file__).resolve().parents[2]
CATALOGO = Catalogo.carregar(RAIZ / "config" / "modelos.toml")
MODELO = "falso:claude-opus-5-5"
REV = "falso:gpt-6.1-sol"
GEOM = Geometria(30.0, 30.0, 60)


def ServicoProposta(*a, **k):
    k.setdefault("stop_max_pontos", 250.0)
    return _ServicoProposta(*a, **k)


def BracoQuant(*a, **k):
    k.setdefault("geometria", GEOM)
    return _BracoQuant(*a, **k)


def BracoAleatorio(*a, **k):
    k.setdefault("geometrias", (GEOM,))
    return _BracoAleatorio(*a, **k)


def FabricaBracos(*a, **k):
    k.setdefault("stop_max_pontos", 250.0)
    k.setdefault("geometria", GEOM)
    return _FabricaBracos(*a, **k)



def proposta(acao=Acao.COMPRAR, **kw) -> PropostaIA:
    base = {"acao": acao, "hipoteses": ("continuacao",), "evidencias_a_favor": ("movimento.ret_60s_pts = 12",),
            "evidencias_contra": ("microestrutura.spread_ticks = 1",), "riscos": ("reversao",), "incerteza": 0.5,
            "stop_pontos": 30.0, "alvo_pontos": 40.0, "validade_s": 60, "invalidacoes": ("muda de sinal",),
            "justificativa_curta": "teste"}
    if acao in (Acao.ENCERRAR, Acao.MANTER, Acao.NAO_OPERAR):
        base.update(stop_pontos=None, alvo_pontos=None, evidencias_a_favor=("movimento.ret_60s_pts = 12",))
    base.update(kw)
    return PropostaIA(**base)


APROVA = RevisaoIA(veredito="APROVAR", justificativa_curta="ok")
REJEITA = RevisaoIA(veredito="REJEITAR", justificativa_curta="nao")
POS = {"lado": "compra", "quantidade": 1, "preco_medio": 130_000.0, "aberta_ns": T0 - 30 * NS, "stop_pontos": 30.0,
       "alvo_pontos": 40.0}


def bracoB(prov, modo="imediato", prazos=None, **kw):
    return BracoProponente("B", ServicoProposta(prov, MODELO, **kw), modo, Geometria(30, 40, 300), prazos)


def bracoC(prov, rev, modo="imediato", prazos=None):
    return BracoProponenteRevisor("C", ServicoProposta(prov, MODELO), ServicoRevisao(rev, REV), modo,
                                  Geometria(30, 40, 300), prazos)


class Erro429(Exception):
    status_code = 429


# =============================================================================================== quant (A) ===
def dossie_quant(status="validado_oos", ec=3.0, ev=-2.0, ps_c=0.2, ps_v=0.4, direcional=None):
    saida = {"e_brl_compra": ec, "e_brl_venda": ev, "p_stop_compra": ps_c, "p_stop_venda": ps_v}
    sa = {"direcional_alvo": direcional} if direcional is not None else None
    return dossie_basico(extra_modelos={"mm": {"status": status, "saida": saida}}, statarb=sa)


async def test_quant_entra_quando_e_acima_de_theta_e_pstop_abaixo_do_limite():
    a = BracoQuant("quant", modelo="mm", theta_brl=1.0, p_stop_max=0.3, geometria=Geometria(30, 30, 60))
    r = await a.adecidir(ctx_basico(dossie_quant()))
    assert r.resultado == "entrada" and r.acao == Acao.COMPRAR and r.intencao.modo == "quant"
    assert r.intencao.stop_pontos == 30 and r.intencao.tempo_max_s == 60 and not r.rotulos
    assert r.proposta["origem"] == "quant" and r.proposta["rotulo"] == "validado"
    assert not a.ia


@pytest.mark.parametrize("kw,motivo", [
    ({"ec": 0.5, "ev": 0.2}, "sem_edge"),  # E <= theta nos dois lados
    ({"ps_c": 0.45}, "sem_edge"),  # P(stop) acima do limite
])
async def test_quant_nao_entra_sem_edge(kw, motivo):
    a = BracoQuant("quant", modelo="mm", theta_brl=1.0, p_stop_max=0.3)
    r = await a.adecidir(ctx_basico(dossie_quant(**kw)))
    assert r.resultado == "nao_operar" and motivo in r.motivos and r.intencao is None


async def test_quant_escolhe_o_lado_de_maior_e_e_statarb_nao_pode_contradizer():
    a = BracoQuant("quant", modelo="mm", theta_brl=0.0, p_stop_max=0.9)
    r = await a.adecidir(ctx_basico(dossie_quant(ec=1.0, ev=4.0)))
    assert r.acao == Acao.VENDER
    contra = await a.adecidir(ctx_basico(dossie_quant(ec=1.0, ev=4.0, direcional=0.6)))  # statarb quer comprar
    assert contra.resultado == "nao_operar" and "statarb_contradiz" in contra.motivos
    de_acordo = await a.adecidir(ctx_basico(dossie_quant(ec=1.0, ev=4.0, direcional=-0.6)))
    assert de_acordo.acao == Acao.VENDER
    sem = BracoQuant("q2", modelo="mm", theta_brl=0.0, p_stop_max=0.9, usar_statarb=False)
    assert (await sem.adecidir(ctx_basico(dossie_quant(ec=1.0, ev=4.0, direcional=0.6)))).acao == Acao.VENDER


async def test_quant_status_do_modelo_validado_exploratorio_ou_sem_operar():
    a = BracoQuant("quant", modelo="mm", theta_brl=0.0)
    expl = await a.adecidir(ctx_basico(dossie_quant(status="em_validacao")))
    assert expl.resultado == "entrada" and "exploratorio" in expl.rotulos
    assert expl.proposta["rotulo"] == "exploratorio" and expl.proposta["status_modelo"] == "em_validacao"
    for st in ("nao_treinado", "reprovado"):
        r = await a.adecidir(ctx_basico(dossie_quant(status=st)))
        assert r.resultado == "bloqueada" and r.intencao is None and f"modelo_status_inadequado:{st}" in r.motivos
    puro = BracoQuant("q3", modelo="mm", permitir_exploratorio=False)
    r = await puro.adecidir(ctx_basico(dossie_quant(status="em_validacao")))
    assert r.resultado == "bloqueada"
    ausente = await a.adecidir(ctx_basico(dossie_basico()))  # modelo nem aparece no dossie
    assert ausente.resultado == "bloqueada"


async def test_quant_com_posicao_aberta_nao_opera_e_condicional_gera_plano():
    a = BracoQuant("quant", modelo="mm")
    pos = Posicao(simbolo="W", lado=Lado.COMPRA, quantidade=1, preco_medio=130_000.0, ticket=1)
    assert (await a.adecidir(ctx_basico(dossie_quant(), posicao=pos))).resultado == "nao_operar"
    c = BracoQuant("quant_cond", "condicional", modelo="mm", pullback_ticks=2.0, expira_plano_s=180.0)
    r = await c.adecidir(ctx_basico(dossie_quant()))
    assert r.resultado == "plano" and r.plano is not None and r.intencao.modo == "condicional"
    assert r.plano.expira_ns == T0 + 180 * NS and {g.tipo for g in r.plano.gatilhos} == {"preco_toca", "spread_max_pontos"}
    assert r.plano.gatilhos[0].valor == -10.0 and r.plano.invalidacoes[0].tipo == "preco_rompe_acima"


async def test_quant_nao_usa_indicadores_tecnicos():
    import ast
    src = (RAIZ / "src" / "robowin" / "decisao" / "bracos" / "quant.py").read_text()
    nomes = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert not nomes & {"rsi", "macd", "sma", "ema", "bollinger"}
    assert "import pandas" not in src and "features" not in src.split('"""', 2)[2].split("class ")[0]


# ======================================================================================== controles =====
async def test_controle_nao_operar_nunca_entra():
    c = BracoNaoOperar()
    r = await c.adecidir(ctx_basico())
    assert r.resultado == "nao_operar" and r.intencao is None and not c.ia


async def test_controle_aleatorio_reprodutivel_por_seed_com_mesma_frequencia_e_geometria():
    async def rodar(seed, taxa=360.0, n=400, passo=15):
        b = BracoAleatorio("ctl", seed=seed, frequencia_por_hora=taxa, geometrias=(Geometria(25, 35, 90),))
        out = []
        for i in range(n):
            r = await b.adecidir(ctx_basico(t0=T0 + i * passo * NS, ciclo=f"c{i}"))
            out.append((r.acao, r.intencao.stop_pontos if r.intencao else None))
        return out
    a1, a2, b = await rodar(5), await rodar(5), await rodar(6)
    assert a1 == a2 and a1 != b  # mesma seed => mesma sequencia
    entradas = [x for x in a1 if x[0] != Acao.NAO_OPERAR]
    # taxa 360/h e passo de 15 s: ~ 1 - exp(-1.5) = 0.78 por ciclo; frequencia coerente com a taxa pedida
    assert 0.6 < len(entradas) / (len(a1) - 1) < 0.9
    assert {x[1] for x in entradas} == {25.0}  # geometria copiada
    assert {x[0] for x in entradas} == {Acao.COMPRAR, Acao.VENDER}


async def test_controle_aleatorio_segue_frequencia_dinamica_e_geometria_observada():
    taxa = {"v": 0.0}
    b = BracoAleatorio("ctl", seed=1, frequencia_fn=lambda t: taxa["v"], geometrias=(Geometria(30, 30, 60),))
    assert (await b.adecidir(ctx_basico(t0=T0))).resultado == "nao_operar"
    r0 = await b.adecidir(ctx_basico(t0=T0 + 3600 * NS))  # taxa 0 => nunca entra
    assert r0.resultado == "nao_operar"
    taxa["v"] = 1e6
    from robowin.core.tipos import Intencao
    b.observar(Intencao(id="x", braco="ref", acao=Acao.COMPRAR, stop_pontos=44.0, alvo_pontos=55.0, tempo_max_s=120,
                        validade_ns=T0))
    r = await b.adecidir(ctx_basico(t0=T0 + 7200 * NS))
    assert r.resultado == "entrada" and r.intencao.stop_pontos == 44.0 and r.intencao.alvo_pontos == 55.0
    assert r.intencao.tempo_max_s == 120  # a geometria observada substitui a padrao


async def test_controle_aleatorio_condicional_arma_rompimento_e_nao_entra_com_posicao():
    b = BracoAleatorio("ctl_c", "condicional", seed=2, frequencia_por_hora=1e6)
    await b.adecidir(ctx_basico(t0=T0))
    r = await b.adecidir(ctx_basico(t0=T0 + 3600 * NS))
    assert r.resultado == "plano" and r.plano is not None
    assert r.plano.gatilhos[0].tipo in ("preco_rompe_acima", "preco_rompe_abaixo")
    pos = Posicao(simbolo="W", lado=Lado.VENDA, quantidade=1, preco_medio=130_000.0, ticket=1)
    assert (await b.adecidir(ctx_basico(t0=T0 + 7200 * NS, posicao=pos))).resultado == "nao_operar"


# ============================================================================================= B (Claude) ==
async def test_b_proposta_vira_intencao_sem_revisor():
    prov = ProvedorFalso(roteiro=[proposta()])
    b = bracoB(prov)
    r = await b.adecidir(ctx_basico())
    assert r.resultado == "entrada" and r.acao == Acao.COMPRAR and r.intencao.quantidade == 1
    assert r.intencao.stop_pontos == 30 and r.intencao.alvo_pontos == 40 and r.intencao.tempo_max_s == 300
    assert r.intencao.validade_ns == T0 + 60 * NS and r.intencao.t0_ns == T0
    assert [rc.papel for rc in r.chamadas] == ["proponente"] and r.chamadas[0].chamada.valida
    assert r.chamadas[0].chamada.hash_dossie == ctx_basico().hash_dossie  # chamada ligada ao dossie
    assert r.revisao is None and b.revisor is None and prov.chamadas == 1
    assert [c.etapa for c in r.carimbos] == ["proposta_ia"]


@pytest.mark.parametrize("acao", [Acao.NAO_OPERAR])
async def test_b_nao_operar(acao):
    r = await bracoB(ProvedorFalso(roteiro=[proposta(acao)])).adecidir(ctx_basico())
    assert r.resultado == "nao_operar" and r.intencao is None


async def test_b_modo_condicional_converte_duracoes_e_exige_plano():
    p = proposta(plano=plano_rel(-10.0, 120, 50.0))
    b = bracoB(ProvedorFalso(roteiro=[p]), "condicional")
    r = await b.adecidir(ctx_basico())
    assert r.resultado == "plano" and r.plano.expira_ns == T0 + 120 * NS  # duracao -> instante absoluto
    assert r.intencao.plano == r.plano and r.intencao.modo == "condicional"
    sem = await bracoB(ProvedorFalso(roteiro=[proposta()]), "condicional").adecidir(ctx_basico())
    assert sem.resultado == "bloqueada" and sem.motivos == ("sem_plano_condicional",)
    # plano fora do modo condicional invalida a proposta no modo imediato
    fora = await bracoB(ProvedorFalso(roteiro=[p])).adecidir(ctx_basico())
    assert fora.resultado == "bloqueada" and fora.intencao is None


async def test_b_posicao_aberta_encerrar_manter_sem_revisao():
    d = dossie_basico(posicao=POS)
    for acao, esperado in ((Acao.ENCERRAR, "saida"), (Acao.MANTER, "manter")):
        r = await bracoB(ProvedorFalso(roteiro=[proposta(acao)])).adecidir(ctx_basico(d))
        assert r.resultado == esperado and r.intencao.acao == acao
    aumentar = await bracoB(ProvedorFalso(roteiro=[proposta(Acao.COMPRAR)])).adecidir(ctx_basico(d))
    assert aumentar.resultado == "bloqueada"  # nunca aumentar posicao
    sem_pos = await bracoB(ProvedorFalso(roteiro=[proposta(Acao.ENCERRAR)])).adecidir(ctx_basico())
    assert sem_pos.resultado == "bloqueada"  # ENCERRAR sem posicao


# =================================================================================== C (Claude + GPT) =====
async def test_c_revisao_aprovada_gera_intencao_e_registra_a_chamada_do_revisor():
    prov, rev = ProvedorFalso(roteiro=[proposta()]), ProvedorFalso(roteiro=[APROVA])
    r = await bracoC(prov, rev).adecidir(ctx_basico())
    assert r.resultado == "entrada" and r.revisao["veredito"] == "APROVAR"
    assert [rc.papel for rc in r.chamadas] == ["proponente", "revisor"] and rev.chamadas == 1
    assert [c.etapa for c in r.carimbos] == ["proposta_ia", "revisao_ia"]


async def test_c_revisor_rejeita_nenhuma_entrada_divergencia():
    r = await bracoC(ProvedorFalso(roteiro=[proposta()]), ProvedorFalso(roteiro=[REJEITA])).adecidir(ctx_basico())
    assert r.resultado == "bloqueada" and r.intencao is None and r.motivos == ("divergencia_revisor",)
    assert r.revisao["veredito"] == "REJEITAR"
    # aprovacao com ressalva (afirmacao sem suporte) tambem nao basta
    ressalva = RevisaoIA(veredito="APROVAR", afirmacoes_sem_suporte=("dado inventado",))
    r2 = await bracoC(ProvedorFalso(roteiro=[proposta()]), ProvedorFalso(roteiro=[ressalva])).adecidir(ctx_basico())
    assert r2.resultado == "bloqueada" and r2.intencao is None


@pytest.mark.parametrize("acao,esperado", [(Acao.ENCERRAR, "saida"), (Acao.MANTER, "manter"),
                                           (Acao.NAO_OPERAR, "nao_operar")])
async def test_c_revisao_so_para_entradas_encerrar_manter_nunca_esperam(acao, esperado):
    rev = ProvedorFalso(roteiro=[REJEITA], atraso_s=5.0)  # revisor lento E contrario: nao pode interferir
    d = dossie_basico(posicao=POS if acao != Acao.NAO_OPERAR else None)
    t = asyncio.get_event_loop().time()
    r = await bracoC(ProvedorFalso(roteiro=[proposta(acao)]), rev).adecidir(ctx_basico(d))
    assert r.resultado == esperado and rev.chamadas == 0  # o revisor nem foi chamado
    assert asyncio.get_event_loop().time() - t < 1.0
    assert r.revisao is None and all(rc.papel == "proponente" for rc in r.chamadas)


async def test_b_e_c_compartilham_uma_unica_chamada_do_claude():
    prov = ProvedorFalso(roteiro=[proposta()])
    servico = ServicoProposta(prov, MODELO)
    rev = ProvedorFalso(roteiro=[APROVA])
    b = BracoProponente("B", servico, "imediato", Geometria(30, 40, 300))
    c = BracoProponenteRevisor("C", servico, ServicoRevisao(rev, REV), "imediato", Geometria(30, 40, 300))
    assert b.grupo_ciclo == c.grupo_ciclo  # decidem no mesmo ciclo
    ctx = ctx_basico()
    rb, rc = await asyncio.gather(b.adecidir(ctx), c.adecidir(ctx))
    assert prov.chamadas == 1  # UMA chamada, comparacao pareada
    assert rb.proposta["compartilhada"] is False and rc.proposta["compartilhada"] is True
    assert rc.chamadas[0].compartilhada and not rb.chamadas[0].compartilhada  # so uma e gravada/cobrada
    assert rb.chamadas[0].braco == rc.chamadas[0].braco == "B"  # custo atribuido ao dono do servico
    assert rb.intencao.stop_pontos == rc.intencao.stop_pontos  # a MESMA proposta
    # modos distintos nao compartilham (variantes pre-registradas separadas)
    bc = BracoProponente("Bc", servico, "condicional", Geometria(30, 40, 300))
    assert bc.grupo_ciclo != b.grupo_ciclo


async def test_c_com_posicao_propria_aberta_nao_gasta_revisao_em_entrada():
    rev = ProvedorFalso(roteiro=[APROVA])
    pos = Posicao(simbolo="W", lado=Lado.COMPRA, quantidade=1, preco_medio=130_000.0, ticket=3)
    r = await bracoC(ProvedorFalso(roteiro=[proposta()]), rev).adecidir(ctx_basico(posicao=pos))
    assert r.resultado == "bloqueada" and rev.chamadas == 0


async def test_c_sem_tempo_para_revisar_bloqueia_a_entrada():
    # Claude "respondeu" aos 55 s: sobram < 8 s de margem para risco e OMS => nem chama o revisor
    resp = RespostaBruta(texto=proposta().model_dump_json(), objeto=proposta(), modelo_servido="claude-opus-5-5-F",
                         stop_reason="end_turn")
    prov = ProvedorFalso(roteiro=[resp])
    rev = ProvedorFalso(roteiro=[APROVA])
    c = bracoC(prov, rev)
    # injeta a latencia medida na chamada do proponente
    orig = c.proponente.chamar

    async def lenta(*a, **k):
        o, ch, m = await orig(*a, **k)
        return o, ch.model_copy(update={"total_ms": 55_000.0}), m
    c.proponente.chamar = lenta  # type: ignore[method-assign]
    r = await c.adecidir(ctx_basico())
    assert r.resultado == "bloqueada" and rev.chamadas == 0
    assert "ia:resposta_tardia" in r.motivos or "ia:sem_tempo_para_revisao" in r.motivos


# =============================================================== matriz de falhas de IA (B e C) ========
def prov_timeout():
    return ProvedorFalso(roteiro=[proposta()], atraso_s=0.5)  # asyncio.wait_for REAL estoura


def prov_429():
    return ProvedorFalso(roteiro=[Erro429("rate limit")])


def prov_recusa():
    return ProvedorFalso(roteiro=[RespostaBruta(recusa=True, categoria_recusa="politica", modelo_servido="x-FALSO")])


def prov_json_invalido():
    return ProvedorFalso(roteiro=["{isto nao e json"])


def prov_modelo_fora():
    resp = RespostaBruta(texto=proposta().model_dump_json(), objeto=proposta(), modelo_servido="outro-modelo-qualquer",
                         stop_reason="end_turn")
    return ProvedorFalso(roteiro=[resp], modelos_servidos_aceitos=["claude-opus-5-5"])


def prov_max_tokens():
    return ProvedorFalso(roteiro=[RespostaBruta(texto="{", modelo_servido="claude-opus-5-5-F", stop_reason="max_tokens")])


def prov_evidencia_inexistente():
    return ProvedorFalso(roteiro=[proposta(evidencias_a_favor=("movimento.campo_que_nao_existe = 9",))])


def prov_orcamento_esgotado():
    interno = ProvedorFalso(roteiro=[proposta()])
    interno.nome = "anthropic"
    return ProvedorComOrcamento(interno, OrcamentoIA(0.0001), CATALOGO)


FALHAS = [
    ("timeout", prov_timeout, "resposta_tardia"), ("http_429", prov_429, "erro_http"),
    ("recusa", prov_recusa, "recusa"), ("json_invalido", prov_json_invalido, "resposta_invalida"),
    ("modelo_fora_da_lista", prov_modelo_fora, "modelo_fora_da_lista"), ("max_tokens", prov_max_tokens, "max_tokens"),
    ("evidencia_inexistente", prov_evidencia_inexistente, "resposta_invalida"),
    ("orcamento_esgotado", prov_orcamento_esgotado, "orcamento_esgotado"),
]
PZ_CURTO = Prazos(claude_s=0.05, gpt_s=0.05, validade_s=60.0, desvio_max_ticks=4.0, margem_s=8.0)


@pytest.mark.parametrize("nome,fabrica,motivo", FALHAS, ids=[f[0] for f in FALHAS])
async def test_matriz_de_falhas_de_ia_nenhuma_entrada_no_braco_b(nome, fabrica, motivo):
    prov = fabrica()
    ref = "anthropic:claude-opus-5-5" if nome == "orcamento_esgotado" else MODELO
    b = BracoProponente("B", ServicoProposta(prov, ref, max_tokens=2000), "imediato", Geometria(30, 40, 300), PZ_CURTO)
    r = await b.adecidir(ctx_basico())
    assert r.resultado == "bloqueada" and r.intencao is None, nome
    assert f"ia:{motivo}" in r.motivos, (nome, r.motivos)
    assert r.chamadas and not r.chamadas[0].chamada.valida


@pytest.mark.parametrize("nome,fabrica,motivo", FALHAS, ids=[f[0] for f in FALHAS])
async def test_matriz_de_falhas_de_ia_nenhuma_entrada_no_braco_c(nome, fabrica, motivo):
    rev = ProvedorFalso(roteiro=[APROVA])
    ref = "anthropic:claude-opus-5-5" if nome == "orcamento_esgotado" else MODELO
    c = BracoProponenteRevisor("C", ServicoProposta(fabrica(), ref, max_tokens=2000), ServicoRevisao(rev, REV),
                               "imediato", Geometria(30, 40, 300), PZ_CURTO)
    r = await c.adecidir(ctx_basico())
    assert r.resultado == "bloqueada" and r.intencao is None and rev.chamadas == 0, nome  # nem chega ao revisor
    assert f"ia:{motivo}" in r.motivos, (nome, r.motivos)


@pytest.mark.parametrize("nome,fabrica,motivo", [f for f in FALHAS if f[0] in
                                                  ("timeout", "http_429", "recusa", "json_invalido", "modelo_fora_da_lista")])
async def test_matriz_de_falhas_do_revisor_nenhuma_entrada(nome, fabrica, motivo):
    prov = ProvedorFalso(roteiro=[proposta()])
    # o mesmo comportamento defeituoso, agora no REVISOR
    rev_ruim = {"timeout": ProvedorFalso(roteiro=[APROVA], atraso_s=0.5), "http_429": prov_429(),
                "recusa": prov_recusa(), "json_invalido": prov_json_invalido(),
                "modelo_fora_da_lista": ProvedorFalso(roteiro=[RespostaBruta(
                    texto=APROVA.model_dump_json(), objeto=APROVA, modelo_servido="gpt-intruso", stop_reason="end_turn")],
                    modelos_servidos_aceitos=["gpt-6.1-sol"])}[nome]
    c = BracoProponenteRevisor("C", ServicoProposta(prov, MODELO), ServicoRevisao(rev_ruim, REV), "imediato",
                               Geometria(30, 40, 300), PZ_CURTO.__class__(40.0, 0.05, 60.0, 20.0, 8.0))
    r = await c.adecidir(ctx_basico())
    assert r.resultado == "bloqueada" and r.intencao is None and f"ia:{motivo}" in r.motivos, (nome, r.motivos)


async def test_orcamento_esgotado_no_meio_do_pregao_bloqueia_so_as_proximas_entradas():
    interno = ProvedorFalso(roteiro=[proposta()], catalogo=CATALOGO)  # com catalogo: custo real por chamada
    interno.nome = "anthropic"
    orc = OrcamentoIA(0.2, pregoes_no_mes=1)  # a verba do dia acaba depois de algumas chamadas
    prov = ProvedorComOrcamento(interno, orc, CATALOGO)
    b = BracoProponente("B", ServicoProposta(prov, "anthropic:claude-opus-5-5", max_tokens=2000), "imediato",
                        Geometria(30, 40, 300))
    resultados = []
    for i in range(40):
        r = await b.adecidir(ctx_basico(ciclo=f"c{i}"))
        resultados.append(r.resultado)
        if r.resultado == "bloqueada":
            assert "ia:orcamento_esgotado" in r.motivos and r.intencao is None
    k = resultados.index("bloqueada")
    assert k >= 3 and set(resultados[:k]) == {"entrada"}  # antes de esgotar, opera normalmente
    assert set(resultados[k:]) == {"bloqueada"}  # depois, nenhuma entrada (e continua assim)
    assert orc.gasto_mes > 0 and interno.chamadas == k  # as chamadas bloqueadas nem chegaram ao provedor


async def test_defeito_inesperado_do_provedor_nunca_levanta_excecao():
    class Quebrado:
        nome = "quebrado"

        async def gerar_estruturado(self, **kw):
            raise RuntimeError("explodiu")
    r = await bracoB(Quebrado()).adecidir(ctx_basico())
    assert r.resultado == "bloqueada" and r.intencao is None and "ia:erro_provedor" in r.motivos


# ================================================================ extras via [[bracos]] e papeis plugaveis ==
CFG_EXTRA = {"bracos": [
    {"id": "D-gemini", "proponente": "google:gemini-x", "revisor": "anthropic:claude-opus-5-5", "modo": "condicional",
     "effort_revisor": "medium"},
    {"id": "E-local", "proponente": "ollama:qwen", "modo": "imediato"},
]}


def test_extras_de_config_sao_lidos_e_validados():
    ex = extras_de_config(CFG_EXTRA)
    assert [e.id for e in ex] == ["D-gemini", "E-local"] and ex[0].modo == "condicional" and ex[1].revisor is None
    with pytest.raises(ErroConfigBraco):
        extras_de_config({"bracos": [{"id": "x"}]})
    with pytest.raises(ErroConfigBraco):
        extras_de_config({"bracos": [{"id": "x", "proponente": "a:b", "modo": "diario"}]})
    with pytest.raises(ErroConfigBraco):
        extras_de_config({"bracos": [{"id": "x", "proponente": "a:b"}, {"id": "x", "proponente": "a:c"}]})


def _catalogo_extra() -> Catalogo:
    from robowin.core.tipos import EntradaCatalogoModelo
    base = list(CATALOGO)
    base += [EntradaCatalogoModelo(provedor="google", id="gemini-x", fixavel=False, corte_conhecimento="2026-03",
                                   preco_entrada_mtok=1.0, preco_saida_mtok=5.0),
             EntradaCatalogoModelo(provedor="ollama", id="qwen", fixavel=True, corte_conhecimento="2025-10",
                                   preco_entrada_mtok=0.0, preco_saida_mtok=0.0, versao="sha256:abc")]
    return Catalogo(base)


def test_braco_extra_com_outros_proponentes_e_revisores_sem_tocar_no_codigo():
    provs = {"google": ProvedorFalso(), "anthropic": ProvedorFalso(), "ollama": ProvedorFalso(),
             "openai": ProvedorFalso()}
    fab = FabricaBracos(provs, catalogo=_catalogo_extra(), papeis={}, prazos=None, cfg={})
    d, e = fab.extras(CFG_EXTRA)
    assert d.id == "D-gemini" and d.modo == "condicional" and isinstance(d, BracoProponenteRevisor)
    assert d.proponente.ref == "google:gemini-x" and d.revisor.ref == "anthropic:claude-opus-5-5"
    assert d.revisor.effort == "medium"
    assert e.id == "E-local" and isinstance(e, BracoProponente) and e.revisor is None
    m = d.manifesto()  # entra no manifesto do experimento
    assert m["proponente"]["modelo"] == "google:gemini-x" and m["revisor"]["modelo"] == "anthropic:claude-opus-5-5"
    assert m["prazos"]["claude_s"] == 90.0  # prazos do modo condicional


def test_extra_com_modelo_fora_do_catalogo_ou_sem_adaptador_e_recusado():
    provs = {"google": ProvedorFalso(), "anthropic": ProvedorFalso()}
    with pytest.raises(ErroCatalogo):  # google:gemini-x nao esta no catalogo padrao: sem preco/corte => recusado
        FabricaBracos(provs, catalogo=CATALOGO, cfg={}).extras(CFG_EXTRA)
    with pytest.raises(ErroConfigBraco):  # sem adaptador para 'ollama'
        FabricaBracos(provs, catalogo=_catalogo_extra(), cfg={}).extras({"bracos": CFG_EXTRA["bracos"][1:]})


def test_papeis_plugaveis_trocar_o_modelo_so_na_configuracao():
    from robowin.core.tipos import Papel
    provs = {"anthropic": ProvedorFalso(), "openai": ProvedorFalso(), "google": ProvedorFalso()}
    papeis = {"proponente": Papel(nome="proponente", modelo="anthropic:claude-opus-5-5", effort="low"),
              "revisor": Papel(nome="revisor", modelo="openai:gpt-6.1-sol", effort="low")}
    fab = FabricaBracos(provs, catalogo=_catalogo_extra(), papeis=papeis, cfg={})
    b, c, bc, cc = fab.padrao()
    assert [x.id for x in (b, c, bc, cc)] == ["claude", "claude_gpt", "claude_cond", "claude_gpt_cond"]
    assert b.proponente is c.proponente  # B e C compartilham o servico (e a chamada)
    assert c.revisor.effort == "low" and cc.revisor.effort == "medium"  # effort do revisor: low imediato, medium plano
    outros = {**papeis, "proponente": Papel(nome="proponente", modelo="google:gemini-x", effort="low")}
    b2 = FabricaBracos(provs, catalogo=_catalogo_extra(), papeis=outros, cfg={}).padrao()[0]
    assert b2.proponente.ref == "google:gemini-x"  # trocou o proponente sem mexer em codigo


def test_atributos_basicos_dos_bracos_e_protocolo_sincrono():
    a = BracoQuant("quant", modelo="mm", theta_brl=0.0, p_stop_max=0.9)
    ctx = ctx_basico(dossie_quant(ec=2.0))
    a.definir_contexto(ctx)
    it = a.decidir(ctx.t0_ns, ctx.dossie)  # Protocolo core.protocolos.Braco
    assert it is not None and it.acao == Acao.COMPRAR
    with pytest.raises(RuntimeError):
        a.decidir(T0 + 1, ctx.dossie)
    with pytest.raises(ValueError):
        BracoNaoOperar("x", "diario")  # type: ignore[arg-type]
