"""Orquestrador: plano condicional de ponta a ponta, um dono de saida, anti-martingale, falha segura."""
from __future__ import annotations

import json

import pytest
from test_decisao_comum import (
    INS,
    NS,
    T0,
    BracoRoteirizado,
    alimentar,
    montar_orq,
    plano_rel,
    serie,
    tk,
)

from robowin.core.tipos import Acao
from robowin.decisao.plano_condicional import converter_plano
from robowin.decisao.validade import BloqueioDados
from robowin.risco.motor import EventoAgenda


def _decisoes(banco, **filtro):
    sql = "SELECT * FROM decisoes"
    if filtro:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in filtro)
    return banco.consultar(sql + " ORDER BY t_ns", tuple(filtro.values()))


def _plano_passo(toca=-10.0, expira=120, inval=50.0):
    def passo(self, ctx):
        return self.entrada(ctx, plano_abs=converter_plano(plano_rel(toca, expira, inval), ctx.t0_ns), stop=30.0,
                            alvo=30.0)
    return passo


def _roteiro_plano(**kw):
    return [_plano_passo(**kw), lambda s, c: s.nada(c)]


# ---------------------------------------------------------------------------------------------------------
# plano condicional: armar / disparar / cancelar / expirar / invalidar
# ---------------------------------------------------------------------------------------------------------
async def test_plano_armar_disparar_e_stop_recalculado_a_partir_do_preco_real():
    arm = BracoRoteirizado("a", _roteiro_plano(toca=-10.0), modo="condicional")
    orq, banco, _ = montar_orq([arm])
    ticks = serie([130000.0] * 4 + [129985.0] * 6)  # ref = 130002.5; toca em meio <= 129992.5
    await alimentar(orq, ticks)
    armado = [d for d in _decisoes(banco, braco="a") if d["resultado"] == "plano_armado"]
    assert len(armado) == 1
    disp = [d for d in _decisoes(banco, braco="a") if d["gatilho"] == "plano_disparado"]
    assert len(disp) == 1 and disp[0]["resultado"] == "executada"
    prop = json.loads(disp[0]["proposta_json"])
    assert prop["preco_real"] == 129990.0  # ask do tick do disparo (nao a referencia de t0)
    assert prop["ref_plano"] == 130002.5
    assert prop["stop_recalculado"] == 129960.0 and prop["alvo_recalculado"] == 130020.0
    assert prop["plano_idade_ns"] > 0  # a idade do plano e registrada
    pos = orq.sim.posicao("a")
    assert pos.quantidade == 1 and pos.stop == 129960.0  # stop do simulador veio do preco real
    ev = [e.tipo for e in orq.contas["a"].planos.historico]
    assert ev == ["armado", "disparado"]
    assert orq.contas["a"].planos.armado is None  # um plano, um disparo


async def test_plano_risco_refeito_no_disparo_veta_stop_acima_do_maximo():
    def passo(self, ctx):
        return self.entrada(ctx, plano_abs=converter_plano(plano_rel(-10.0, 120, None), ctx.t0_ns), stop=290.0)
    arm = BracoRoteirizado("a", [passo, lambda s, c: s.nada(c)], modo="condicional")
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 4 + [129985.0] * 4))
    d = [x for x in _decisoes(banco, braco="a") if x["gatilho"] == "plano_disparado"]
    assert len(d) == 1 and d[0]["resultado"] == "vetada"
    assert "perda_por_operacao_acima_do_maximo" in d[0]["motivos"]
    assert orq.sim.posicao("a").quantidade == 0


async def test_plano_cancelado_por_kill_switch_nao_dispara():
    arm = BracoRoteirizado("a", _roteiro_plano(), modo="condicional")
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 3))
    assert orq.contas["a"].planos.armado is not None
    orq.acionar_emergencia("teste", orq.t_ultimo_ns)
    assert orq.contas["a"].planos.armado is None  # o efeito do kill cancela o plano na hora
    await alimentar(orq, serie([129980.0] * 4, inicio_ms=3000))
    assert orq.sim.posicao("a").quantidade == 0
    h = orq.contas["a"].planos.historico
    assert [e.tipo for e in h] == ["armado", "cancelado"] and h[-1].motivo == "kill_switch"
    assert not [d for d in _decisoes(banco) if d["gatilho"] == "plano_disparado"]


async def test_plano_cancelado_por_janela_de_evento():
    ev = EventoAgenda(t_ns=T0 + 480 * NS, nome="copom")  # bloqueio de +-5 min: a janela abre em 180 s
    arm = BracoRoteirizado("a", _roteiro_plano(expira=500, inval=None), modo="condicional")
    orq, banco, _ = montar_orq([arm], eventos=[ev])
    await alimentar(orq, serie([130000.0] * 200, passo_ms=1000))  # 200 s, sem tocar no gatilho
    h = orq.contas["a"].planos.historico
    assert h[-1].tipo == "cancelado" and h[-1].motivo == "janela_de_evento"
    assert h[-1].t_ns > T0 + 175 * NS
    assert orq.sim.posicao("a").quantidade == 0


async def test_plano_expira_e_invalida():
    arm = BracoRoteirizado("a", _roteiro_plano(toca=-100.0, expira=5, inval=None), modo="condicional")
    orq, _, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 8))
    assert [e.tipo for e in orq.contas["a"].planos.historico][:2] == ["armado", "expirado"]
    arm2 = BracoRoteirizado("b", _roteiro_plano(toca=-100.0, expira=120, inval=50.0), modo="condicional")
    orq2, _, _ = montar_orq([arm2])
    await alimentar(orq2, serie([130000.0] * 3 + [130080.0] * 3))  # +80 pts: invalida (rompe acima de +50)
    h = orq2.contas["b"].planos.historico
    assert h[1].tipo == "invalidado" and "preco_rompe_acima" in h[1].motivo


async def test_um_plano_por_braco_novo_plano_substitui_o_anterior():
    arm = BracoRoteirizado("a", [_plano_passo(toca=-80.0), _plano_passo(toca=-90.0), lambda s, c: s.nada(c)],
                           modo="condicional")
    orq, _, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 4))
    tipos = [e.tipo for e in orq.contas["a"].planos.historico]
    assert tipos.count("armado") == 2 and "substituido" in tipos
    assert orq.contas["a"].planos.armado is not None  # exatamente um armado


# ---------------------------------------------------------------------------------------------------------
# um dono de saida, anti-martingale, falha segura
# ---------------------------------------------------------------------------------------------------------
async def test_saida_por_tempo_tem_um_unico_dono_e_nao_inverte():
    arm = BracoRoteirizado("a", [lambda s, c: s.entrada(c, stop=60.0, alvo=None, tempo=3), lambda s, c: s.nada(c)])
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 12))
    conta = orq.contas["a"]
    tr = banco.consultar("SELECT * FROM trades")
    assert len(tr) == 1 and tr[0]["motivo_saida"] == "tempo"
    assert orq.sim.posicao("a").quantidade == 0  # nunca virou posicao vendida
    saidas = [o for o in banco.consultar("SELECT * FROM ordens") if o["tipo"] == "saida"]
    assert len(saidas) == 1  # UMA ordem de saida
    assert not orq.saidas.tem_dono("a")  # reivindicacao liberada ao fechar
    assert conta.stats["saida_tempo"] == 1


async def test_reivindicacoes_concorrentes_de_saida_sao_recusadas():
    arm = BracoRoteirizado("a", [lambda s, c: s.entrada(c, stop=60.0, alvo=None, tempo=600), lambda s, c: s.nada(c)])
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 5))
    conta = orq.contas["a"]
    assert orq.sim.posicao("a").quantidade == 1
    t = orq.t_ultimo_ns
    assert orq._sair(conta, "tempo", t) is True
    assert orq._sair(conta, "encerrar", t + 1) is False  # ENCERRAR concorrente: recusado
    assert orq._sair(conta, "zeragem", t + 2, forcado=True) is False  # zeragem concorrente: recusada
    assert orq.saidas.n_recusadas == 2
    await alimentar(orq, serie([130000.0] * 3, inicio_ms=5000))
    assert len([o for o in banco.consultar("SELECT * FROM ordens") if o["tipo"] == "saida"]) == 1
    assert orq.sim.posicao("a").quantidade == 0 and len(banco.consultar("SELECT * FROM trades")) == 1


async def test_encerrar_da_ia_com_saida_ja_reivindicada_e_ignorado():
    def entra_e_encerra(self, ctx):
        return self.encerrar(ctx) if ctx.posicao.quantidade else self.entrada(ctx, stop=60.0, alvo=None, tempo=600)
    arm = BracoRoteirizado("a", [entra_e_encerra])
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 12))
    tr = banco.consultar("SELECT * FROM trades")
    assert len(tr) == 1 and tr[0]["motivo_saida"] == "encerrar"
    assert len([o for o in banco.consultar("SELECT * FROM ordens") if o["tipo"] == "saida"]) == 1


async def test_stop_e_saida_por_tempo_concorrentes_resultam_em_uma_so_saida():
    arm = BracoRoteirizado("a", [lambda s, c: s.entrada(c, stop=30.0, alvo=None, tempo=4), lambda s, c: s.nada(c)])
    orq, banco, _ = montar_orq([arm], latencia_exec_ns=1 * NS)
    precos = [130000.0] * 6 + [129960.0] * 2 + [129900.0] * 8  # stop (30 pts) estoura perto da saida por tempo
    await alimentar(orq, serie(precos))
    assert orq.sim.posicao("a").quantidade == 0
    assert len(banco.consultar("SELECT * FROM trades")) == 1  # nunca duas saidas fechando a mesma posicao
    assert orq.sim.posicao("a").lado is None


async def test_anti_martingale_preservado_apos_perda():
    def passo(self, ctx):
        if self.i == 1:
            return self.entrada(ctx, stop=30.0, alvo=None, tempo=600)
        if self.i >= 40:
            return self.entrada(ctx, quantidade=2, stop=30.0, alvo=None, tempo=60)  # tenta dobrar apos a perda
        return self.nada(ctx)
    arm = BracoRoteirizado("a", [passo])
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 5 + [129900.0] * 60))  # a primeira operacao perde no stop
    tr = banco.consultar("SELECT * FROM trades")
    assert len(tr) == 1 and tr[0]["liquido_brl"] < 0 and tr[0]["motivo_saida"] == "stop"
    vet = [d for d in _decisoes(banco, braco="a") if d["resultado"] == "vetada"]
    assert vet and "martingale_proibido" in vet[-1]["motivos"]
    assert orq.sim.posicao("a").quantidade == 0
    assert orq.contas["a"].stats["entradas"] == 1  # a segunda nunca saiu


async def test_resposta_tardia_e_desvio_de_preco_bloqueiam_a_entrada():
    tarde = BracoRoteirizado("tarde", [lambda s, c: s.entrada(c, latencia_ns=61 * NS), lambda s, c: s.nada(c)])
    desvio = BracoRoteirizado("desvio", [lambda s, c: s.entrada(c, latencia_ns=10 * NS), lambda s, c: s.nada(c)])
    orq, banco, _ = montar_orq([tarde, desvio])
    await alimentar(orq, serie([130000.0] * 10 + [130040.0] * 60))  # +40 pts durante a espera (> 20)
    for braco, trecho in (("tarde", "vencida"), ("desvio", "desvio")):
        d = [x for x in _decisoes(banco, braco=braco) if x["resultado"] == "bloqueada"]
        assert d and trecho in d[0]["motivos"], braco
        assert orq.contas[braco].stats["entradas"] == 0


async def test_lacuna_de_sequencia_bloqueia_entradas_mas_a_saida_continua():
    bd = BloqueioDados(retencao_lacuna_s=30.0, idade_max_dado_s=60.0)
    arm = BracoRoteirizado("a", [lambda s, c: s.entrada(c, stop=60.0, alvo=None, tempo=6), lambda s, c: s.nada(c)])
    orq, banco, _ = montar_orq([arm], bloqueio=bd)
    await alimentar(orq, serie([130000.0] * 4))
    assert orq.sim.posicao("a").quantidade == 1
    bd.lacuna_sequencia(orq.t_ultimo_ns, "teste")  # lacuna com posicao aberta
    await alimentar(orq, serie([130000.0] * 12, inicio_ms=4000))
    assert len(banco.consultar("SELECT * FROM trades")) == 1  # a saida por tempo funcionou durante o bloqueio
    # nova entrada durante o bloqueio e recusada
    arm2 = BracoRoteirizado("b", [lambda s, c: s.entrada(c, latencia_ns=5 * NS), lambda s, c: s.nada(c)])
    bd2 = BloqueioDados(retencao_lacuna_s=30.0, idade_max_dado_s=60.0)
    orq2, banco2, _ = montar_orq([arm2], bloqueio=bd2)
    await alimentar(orq2, serie([130000.0] * 2))
    bd2.lacuna_sequencia(orq2.t_ultimo_ns, "teste")
    await alimentar(orq2, serie([130000.0] * 12, inicio_ms=2000))
    d = [x for x in _decisoes(banco2, braco="b") if x["resultado"] == "bloqueada"]
    assert d and "dado:lacuna_sequencia" in d[0]["motivos"]
    assert orq2.sim.posicao("b").quantidade == 0


async def test_falha_de_ia_nao_para_a_protecao_da_posicao_aberta():
    def passo(self, ctx):
        if self.i == 1:
            return self.entrada(ctx, stop=60.0, alvo=None, tempo=5)
        return self.bloqueada(ctx, "ia:resposta_tardia")
    arm = BracoRoteirizado("a", [passo], ia=True)
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 14))
    assert len(banco.consultar("SELECT * FROM trades")) == 1  # tempo-limite deterministico, sem IA
    assert [d for d in _decisoes(banco, braco="a") if "ia:resposta_tardia" in d["motivos"]]


async def test_kill_switch_zera_e_bloqueia_novas_entradas():
    arm = BracoRoteirizado("a", [lambda s, c: s.entrada(c, stop=60.0, alvo=None, tempo=600)])
    orq, banco, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 4))
    assert orq.sim.posicao("a").quantidade == 1
    orq.acionar_emergencia("teste", orq.t_ultimo_ns)
    await alimentar(orq, serie([130000.0] * 6, inicio_ms=4000))
    assert orq.sim.posicao("a").quantidade == 0
    tr = banco.consultar("SELECT * FROM trades")
    assert len(tr) == 1
    ev = {e["tipo"] for e in banco.consultar("SELECT * FROM eventos_risco")}
    assert "kill_switch" in ev and "saida_kill" in ev
    assert orq.contas["a"].stats["entradas"] == 1  # nada novo depois do kill


async def test_um_braco_nunca_depende_do_outro_contas_separadas():
    a = BracoRoteirizado("a", [lambda s, c: s.entrada(c, stop=30.0, alvo=None, tempo=600)])
    b = BracoRoteirizado("b", [lambda s, c: s.nada(c)])
    orq, _, _ = montar_orq([a, b])
    await alimentar(orq, serie([130000.0] * 4))
    assert orq.sim.posicao("a").quantidade == 1 and orq.sim.posicao("b").quantidade == 0
    assert orq.contas["a"].motor is not orq.contas["b"].motor and orq.contas["a"].kill is not orq.contas["b"].kill


def test_bracos_duplicados_sao_recusados():
    from robowin.core.config import ErroConfig
    a1 = BracoRoteirizado("a", [lambda s, c: s.nada(c)])
    a2 = BracoRoteirizado("a", [lambda s, c: s.nada(c)])
    with pytest.raises(ErroConfig):
        montar_orq([a1, a2])


def test_acao_enum_e_instrumento_de_teste():
    assert Acao.COMPRAR.value == "COMPRAR" and INS.tick_pontos == 5.0 and T0 > 0 and tk(0, 1.0).bid == 1.0


async def test_livro_atrasado_ao_vivo_vale_a_partir_da_chegada_sem_quebrar_a_causalidade():
    from robowin.core.tipos import LivroSnapshot, NivelBook
    arm = BracoRoteirizado("a", [lambda s, c: s.nada(c)])
    orq, _, _ = montar_orq([arm])
    await alimentar(orq, serie([130000.0] * 3))
    velho = LivroSnapshot(t_utc_ns=T0, niveis=(NivelBook(preco=129995.0, volume=10.0, lado="compra"),
                                               NivelBook(preco=130005.0, volume=10.0, lado="venda")))
    orq.on_livro(velho)  # mais antigo que o ultimo tick
    assert orq.fora_de_ordem == 0
    await alimentar(orq, serie([130000.0], inicio_ms=3000))
    assert orq.ticks_processados == 4
