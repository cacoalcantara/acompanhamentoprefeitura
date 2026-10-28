"""Maquina do Tempo integrada a decisao: evidencia anonimizada ao revisor, veto opcional por odds (desligado por
padrao), indisponivel nunca bloqueia nem aprova por omissao, prompt do revisor versionado por hash."""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from perfis_teste import WIN
from test_decisao_bracos import APROVA, MODELO, REJEITA, REV, ServicoProposta, bracoC, proposta
from test_decisao_comum import INS, NS, T0, BracoRoteirizado, ctx_basico, dossie_basico, serie
from test_maquina_do_tempo import _sessao, piscina

from robowin.avaliacao import maquina_do_tempo as mt
from robowin.core.config import carregar_toml
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import Acao, ConfigRisco, Lado
from robowin.dados.banco import BancoSQLite
from robowin.decisao import odds as od
from robowin.decisao.bracos import BracoProponente, Geometria, ServicoRevisao
from robowin.decisao.gatilhos import ConfigGatilhos
from robowin.decisao.orquestrador import ConfigOrquestrador, Orquestrador, _chave_bloqueio
from robowin.decisao.registro import Registro
from robowin.ia.dossie import verificar_anonimizacao
from robowin.ia.prompts import carregar_prompt, hashes_prompts
from robowin.ia.provedores.falso import ProvedorFalso

RAIZ = Path(__file__).resolve().parents[2]
HASH_REVISOR_ANTES = "5cbfbcd4925e4130ee88749c8b4fa27465bd3b8ff57fb4a688b23dc159eb4956"  # revisor_v1.md sem o campo
PRECO = 130_000.0


class ProvedorGravador(ProvedorFalso):
    """Provedor falso que guarda as mensagens que recebeu (para ver o que o revisor/proponente enxerga)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.mensagens: list[str] = []

    async def _executar(self, *, mensagem, **kw):
        self.mensagens.append(mensagem)
        return await super()._executar(mensagem=mensagem, **kw)


def avaliador(pool="padrao", **cfg) -> od.AvaliadorOdds:
    pool = piscina(25) if pool == "padrao" else pool
    return od.AvaliadorOdds(WIN, pool, od.ConfigOdds(replays=1000, **cfg))


def avaliar(av: od.AvaliadorOdds, t0: int = T0, stop: float = 30.0, alvo: float = 60.0, lado: Lado = Lado.COMPRA):
    return av.avaliar(t0_ns=t0, lado=lado, stop_pontos=stop, alvo_pontos=alvo, preco_ref=PRECO)


def ctx_com_odds(av: od.AvaliadorOdds | None):
    ctx = ctx_basico()
    if av is not None:
        ctx.odds = lambda c, lado, stop, alvo: av.avaliar(t0_ns=c.t0_ns, lado=lado, stop_pontos=stop, alvo_pontos=alvo,
                                                         preco_ref=c.preco_ref)
    return ctx


# --------------------------------------------------------------------------------------------- campo anonimizado
def test_campo_do_dossie_tem_probabilidades_r_veredito_e_n_sessoes_sem_datas_nem_niveis():
    a = avaliar(avaliador())
    assert a.disponivel and a.campo["status"] == "ok"
    for k in ("p_alvo_primeiro", "p_stop_primeiro", "p_nenhum", "r_medio_bruto", "r_medio_liquido", "veredito",
              "n_sessoes", "equilibrio_liquido"):
        assert k in a.campo, k
    assert a.campo["n_sessoes"] == 25 and a.campo["natureza"] == od.NATUREZA and "previsao" in a.campo["natureza"]
    assert verificar_anonimizacao({od.CAMPO: a.campo}, [PRECO, a.resultado.preco_entrada]) == []
    dossie = {**dossie_basico(), od.CAMPO: a.campo}  # o dossie completo da revisao continua anonimizado
    assert verificar_anonimizacao(dossie, [PRECO]) == []
    txt = json.dumps(a.campo)
    assert not any(ano in txt for ano in ("2025", "2026", "WIN", "preco_entrada")) and "percentis" not in txt


def test_campo_indisponivel_passa_pela_anonimizacao():
    assert verificar_anonimizacao({od.CAMPO: od.campo_indisponivel("sessoes_insuficientes")}, [PRECO]) == []


def test_causal_em_backtest_a_piscina_so_tem_sessoes_ate_o_dia_de_t0():
    base = piscina(25)
    dia = dt.date(2026, 11, 10)  # dia de T0
    rng = np.random.default_rng(5)
    futuras = tuple(_sessao(dia + dt.timedelta(days=k), rng, base=999_999.0, vol_ticks=80.0) for k in range(6))
    com_futuro = mt.PiscinaSessoes(base.sessoes + futuras, {}, base.intervalo_s, True, "t")
    a, b = avaliar(avaliador(base)), avaliar(avaliador(com_futuro))
    assert a.campo == b.campo and a.campo["n_sessoes"] == 25  # o proprio dia de t0 e o futuro nao entram


# --------------------------------------------------------------------------------------------- indisponivel
def test_indisponivel_nunca_bloqueia_nem_aprova_por_omissao(monkeypatch):
    casos = {
        "sem piscina": avaliador(None, veto_por_odds=True),
        "sessoes insuficientes": avaliador(piscina(12), veto_por_odds=True),
        "sem rotulo": avaliador(piscina(25, rotulo=None), veto_por_odds=True),
        "fora da sessao": avaliador(veto_por_odds=True),
    }
    saidas = {k: avaliar(v) for k, v in casos.items() if k != "fora da sessao"}
    fora = casos["fora da sessao"].avaliar(t0_ns=T0 - 3 * 3600 * NS, lado=Lado.COMPRA, stop_pontos=30.0,
                                           alvo_pontos=60.0, preco_ref=PRECO)
    saidas["fora da sessao"] = fora
    for nome, a in saidas.items():
        assert not a.disponivel and not a.veto and a.resultado is None, nome
        assert a.campo["status"] == od.STATUS_INDISPONIVEL and a.campo["motivo"], nome
    assert saidas["sessoes insuficientes"].campo["motivo"] == "falha:ErroSessoesInsuficientes"

    def explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(mt, "simular", explode)
    b = avaliar(avaliador(veto_por_odds=True))
    assert not b.disponivel and not b.veto and b.campo["motivo"] == "falha:RuntimeError"


# --------------------------------------------------------------------------------------------- veto por odds
def test_veto_por_odds_desligado_por_padrao():
    assert od.ConfigOdds().veto_por_odds is False and od.config_odds({}).veto_por_odds is False
    cfg = carregar_toml(RAIZ / "config" / "robowin.toml")
    assert cfg["decisao"]["veto_por_odds"] is False
    c = od.config_odds(cfg)
    assert c.veto_por_odds is False and c.margem_veto_odds == pytest.approx(0.05)
    contra = piscina(25, deriva_ticks=-2.0, vol_ticks=1.0)  # alvo quase nunca primeiro
    a = avaliar(od.AvaliadorOdds(WIN, contra, od.ConfigOdds(replays=1000)))
    assert a.disponivel and a.campo["veredito"] == mt.V_FORTE_STOP and not a.veto  # desligado: so informa
    ligado = od.config_odds({"decisao": {"veto_por_odds": True, "margem_veto_odds": 0.1}})
    assert ligado.veto_por_odds and ligado.margem_veto_odds == pytest.approx(0.1)


def test_veto_ligado_bloqueia_com_motivo_rotulado_so_quando_abaixo_do_equilibrio_mais_margem():
    contra = avaliar(avaliador(piscina(25, deriva_ticks=-2.0, vol_ticks=1.0), veto_por_odds=True))
    assert contra.veto and contra.motivo_bloqueio.startswith("veto_por_odds:")
    assert "equilibrio_liquido+margem" in contra.motivo_bloqueio
    a_favor = avaliar(avaliador(piscina(25, deriva_ticks=2.0, vol_ticks=1.0), veto_por_odds=True))
    assert a_favor.disponivel and not a_favor.veto
    # a margem faz diferenca: perto do equilibrio, uma margem enorme veta
    neutro = piscina(40, seed=3)
    sem = avaliar(avaliador(neutro, veto_por_odds=True, margem_veto_odds=-1.0))
    com = avaliar(avaliador(neutro, veto_por_odds=True, margem_veto_odds=1.0))
    assert not sem.veto and com.veto
    # sem desfechos resolvidos (barras planas) nao ha base: nao veta
    plano_chato = avaliar(avaliador(piscina(25, vol_ticks=0.0, amplitude_ticks=0.0, fixa=True), veto_por_odds=True,
                                    margem_veto_odds=1.0))
    assert plano_chato.disponivel and not plano_chato.veto


def test_chave_de_bloqueio_agrupa_vetos_por_odds():
    assert _chave_bloqueio("veto_por_odds:p_alvo=0.1<x") == "veto_por_odds"
    assert _chave_bloqueio("ia:resposta_tardia") == "resposta_tardia" and _chave_bloqueio("sem_edge") == "sem_edge"


# --------------------------------------------------------------------------------------------- revisor (braco C)
async def test_revisor_recebe_o_campo_no_dossie_da_revisao_e_o_proponente_nao():
    prop, rev = ProvedorGravador(roteiro=[proposta()]), ProvedorGravador(roteiro=[APROVA])
    ctx = ctx_com_odds(avaliador())
    r = await bracoC(prop, rev).adecidir(ctx)
    assert r.resultado == "entrada"
    assert od.CAMPO not in prop.mensagens[0]  # o dossie do proponente (compartilhado) nao muda
    msg = rev.mensagens[0]
    assert od.CAMPO in msg and "p_alvo_primeiro" in msg and '"status":"ok"' in msg
    dossie_rev = json.loads(msg.split("DOSSIE (JSON anonimizado):\n", 1)[1].split("\n\nPROPOSTA", 1)[0])
    assert verificar_anonimizacao(dossie_rev, [PRECO]) == []
    assert r.proposta[od.CAMPO]["status"] == "ok"  # fica na decisao gravada
    rev_ch = next(c.chamada for c in r.chamadas if c.papel == "revisor")
    assert rev_ch.hash_dossie != ctx.hash_dossie  # outro dossie, outro hash


async def test_sem_avaliador_o_revisor_ve_exatamente_o_mesmo_dossie_de_antes():
    rev = ProvedorGravador(roteiro=[APROVA])
    r = await bracoC(ProvedorGravador(roteiro=[proposta()]), rev).adecidir(ctx_com_odds(None))
    assert r.resultado == "entrada" and od.CAMPO not in rev.mensagens[0] and od.CAMPO not in r.proposta


async def test_odds_indisponivel_vai_ao_revisor_como_indisponivel_e_nao_decide_nada():
    av = avaliador(None, veto_por_odds=True)
    rev = ProvedorGravador(roteiro=[APROVA])
    r = await bracoC(ProvedorGravador(roteiro=[proposta()]), rev).adecidir(ctx_com_odds(av))
    assert r.resultado == "entrada"  # indisponivel nao bloqueia
    assert "indisponivel" in rev.mensagens[0] and od.CAMPO in rev.mensagens[0]
    r2 = await bracoC(ProvedorGravador(roteiro=[proposta()]), ProvedorGravador(roteiro=[REJEITA])).adecidir(ctx_com_odds(av))
    assert r2.resultado == "bloqueada"  # nem aprova: o veredito do revisor continua valendo
    assert r2.motivos == ("divergencia_revisor",)


async def test_falha_do_callable_de_odds_vira_indisponivel():
    ctx = ctx_basico()

    def quebra(*a):
        raise ValueError("x")
    ctx.odds = quebra
    rev = ProvedorGravador(roteiro=[APROVA])
    r = await bracoC(ProvedorGravador(roteiro=[proposta()]), rev).adecidir(ctx)
    assert r.resultado == "entrada" and "falha:ValueError" in rev.mensagens[0]


async def test_veto_ligado_bloqueia_antes_de_gastar_o_revisor_e_rotula_o_motivo():
    av = avaliador(piscina(25, deriva_ticks=-2.0, vol_ticks=1.0), veto_por_odds=True)
    rev = ProvedorGravador(roteiro=[APROVA])
    r = await bracoC(ProvedorGravador(roteiro=[proposta()]), rev).adecidir(ctx_com_odds(av))
    assert r.resultado == "bloqueada" and r.intencao is None
    assert len(r.motivos) == 1 and r.motivos[0].startswith("veto_por_odds:")
    assert rev.mensagens == [] and r.proposta[od.CAMPO]["status"] == "ok"
    # braco B (sem revisor) tambem respeita o veto; desligado, a mesma proposta passa
    b = BracoProponente("B", ServicoProposta(ProvedorFalso(roteiro=[proposta()]), MODELO), "imediato",
                        Geometria(30, 40, 300))
    assert (await b.adecidir(ctx_com_odds(av))).resultado == "bloqueada"
    desligado = avaliador(piscina(25, deriva_ticks=-2.0, vol_ticks=1.0), veto_por_odds=False)
    b2 = BracoProponente("B", ServicoProposta(ProvedorFalso(roteiro=[proposta()]), MODELO), "imediato",
                         Geometria(30, 40, 300))
    assert (await b2.adecidir(ctx_com_odds(desligado))).resultado == "entrada"


async def test_acoes_que_nao_sao_entrada_nao_consultam_as_odds():
    chamadas = []
    ctx = ctx_basico()
    ctx.odds = lambda *a: chamadas.append(a)
    r = await bracoC(ProvedorFalso(roteiro=[proposta(Acao.NAO_OPERAR)]), ProvedorFalso(roteiro=[APROVA])).adecidir(ctx)
    assert r.resultado == "nao_operar" and chamadas == []


async def test_servico_de_revisao_recusa_vazar_nivel_absoluto_no_campo():
    ctx = ctx_basico()
    rev = ProvedorGravador(roteiro=[APROVA])
    sujo = {"status": "ok", "nota": "entrada em 2026-03-10"}
    await ServicoRevisao(rev, REV).revisar(ctx, proposta(), 10.0, probabilidades=sujo)
    assert "2026-03-10" not in rev.mensagens[0] and "anonimizacao_falhou" in rev.mensagens[0]


# --------------------------------------------------------------------------------------------- orquestrador
def _orq(av):
    rel = RelogioSimulado(T0)
    cfg = ConfigOrquestrador(instrumento=INS, risco=ConfigRisco(), sessao_id="t", passo_s=1.0, checagem_s=1.0,
                             exigir_aquecimento=False, latencia_exec_ns=0,
                             gatilhos=ConfigGatilhos(periodo_s=1.0, intervalo_min_s=1.0, e_brl_min=1e18, z_statarb_min=1e18,
                                                     evento_posicao=False, max_chamadas_dia=None))
    arm = BracoRoteirizado("a", [lambda s, c: s.nada(c)], ia=True)
    return Orquestrador(cfg, relogio=rel, registro=Registro(BancoSQLite(":memory:"), None, "t"), bracos=[arm],
                        avaliador_odds=av), arm


async def test_orquestrador_entrega_o_avaliador_no_contexto_do_braco():
    av = avaliador()
    orq, arm = _orq(av)
    for t in serie([PRECO] * 5):
        await orq.on_tick(t)
    assert arm.contextos and all(c.odds is not None for c in arm.contextos)
    res = arm.contextos[-1].odds(arm.contextos[-1], Lado.VENDA, 30.0, 60.0)
    assert res.disponivel and res.campo["status"] == "ok"
    orq2, arm2 = _orq(None)
    for t in serie([PRECO] * 5):
        await orq2.on_tick(t)
    assert arm2.contextos and all(c.odds is None for c in arm2.contextos)


def test_t0_cai_na_proxima_fronteira_de_barra_e_respeita_o_limite_de_entradas():
    av = avaliador()
    assert avaliar(av, t0=T0 + 120 * NS).disponivel  # 10:02 -> entrada 10:05
    tarde = T0 + (17 * 3600 + 40 * 60 - 10 * 3600) * NS  # 17:40: depois do limite de entradas (17:30)
    a = avaliar(av, t0=tarde)
    assert not a.disponivel and not a.veto


# --------------------------------------------------------------------------------------------- prompt versionado
def test_prompt_do_revisor_mudou_de_hash_e_diz_simulacao_nao_previsao():
    p = carregar_prompt("revisor")
    assert p.hash != HASH_REVISOR_ANTES and hashes_prompts()["revisor_v1.md"] == p.hash
    for trecho in ("probabilidades_do_plano", "SIMULACAO", "NAO e previsao", "NAO prova lucro", "indisponivel"):
        assert trecho in p.texto, trecho


# --------------------------------------------------------------------------------------------- e2e sintetico
def test_simular_com_odds_sessoes_liga_a_maquina_do_tempo_ponta_a_ponta(tmp_path):
    from robowin.cli import main
    saida = tmp_path / "s"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(["simular", "--n-ticks", "12000", "--saida", str(saida), "--seed", "11", "--odds-sessoes", "21"])
    assert rc == 0 and "SIMULACAO / SINTETICO" in buf.getvalue()
    con = sqlite3.connect(saida / "robowin.db")
    props = [json.loads(r[0]) for r in con.execute(
        "SELECT proposta_json FROM decisoes WHERE braco IN ('claude','claude_gpt') AND proposta_json IS NOT NULL")]
    com = [p[od.CAMPO] for p in props if od.CAMPO in p]
    assert com, "nenhuma proposta de entrada carregou o campo"
    assert all(c["status"] in ("ok", "indisponivel") for c in com)
    assert all(c["n_sessoes"] >= 20 for c in com if c["status"] == "ok")
    assert all(verificar_anonimizacao({od.CAMPO: c}, []) == [] for c in com)
