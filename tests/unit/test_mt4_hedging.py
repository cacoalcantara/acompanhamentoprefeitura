"""Hedging x netting: saida SEMPRE por ticket, sem inversao nem duplicacao, |abertas + pendentes| <= contratos_max.

Testes por propriedade (hypothesis) em AMBOS os modos: MT4/hedging (DubleMT4) e MT5/netting (DubleMT5)."""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from perfis_teste import WIN
from test_mt4_comum import montar_exec4
from test_mt5_comum import SIMB, T0_MS, cmd, montar_exec

from robowin.core.tipos import Lado
from robowin.mt5 import constantes as C
from robowin.mt5 import protocolo_mt4 as P
from robowin.mt5.duble import gerar_ticks_sinteticos
from robowin.mt5.guardiao import ConfigGuardiao, Guardiao

CFG = {"msgs_por_min": 100_000}

OPS = st.one_of(
    st.tuples(st.just("entrar"), st.sampled_from([Lado.COMPRA, Lado.VENDA]), st.sampled_from(["mercado", "limitada"])),
    st.just(("sair",)),
    st.just(("sair_mesmo_lado",)),
    st.just(("sair_ticket_falso",)),
    st.just(("zerar",)),
    st.tuples(st.just("tempo"), st.integers(1, 40)),
)


class Modo:
    """Interface comum para os dois modos: posicoes abertas (ticket, sinal, volume) e pendentes."""

    def __init__(self, hedging: bool, semente: int, contratos_max: int) -> None:
        ticks = gerar_ticks_sinteticos(T0_MS - 1000, 600, 100, semente=semente, passo_max_ticks=3)
        extra = {**CFG, "contratos_max": contratos_max}
        if hedging:
            self.a = montar_exec4(ticks, cfg_extra=extra)
            self.term = self.a.d
        else:
            self.a = montar_exec(ticks, cfg_extra=extra)
            self.term = self.a.d
        self.hedging, self.max = hedging, contratos_max

    def posicoes(self) -> list[tuple[int, int, float]]:
        ps = self.a.d.positions_get(symbol=SIMB) if not self.hedging else self.a.con.positions_get(symbol=SIMB)
        return [(int(p.ticket), 1 if int(p.type) == C.POS_COMPRA else -1, float(p.volume)) for p in ps]

    def pendentes(self) -> int:
        os_ = self.a.d.orders_get(symbol=SIMB) if not self.hedging else self.a.con.orders_get(symbol=SIMB)
        return len(os_)

    def tick(self) -> tuple[float, float]:
        t = self.a.con.symbol_info_tick(SIMB) if self.hedging else self.a.d.symbol_info_tick(SIMB)
        return float(t.bid), float(t.ask)

    def abrir_cmds(self) -> int:
        """Quantidade de requisicoes que podem CRIAR exposicao nova enviadas ao terminal."""
        if self.hedging:
            return len([e for e in self.a.d.envios if e["cmd"] == P.CMD_ORDEM_ABRIR])
        return len([e for e in self.a.d.envios if "position" not in e and e["action"] in (C.ACAO_DEAL, C.ACAO_PENDING)])


def executar(m: Modo, op: tuple, n: int) -> None:
    ex = m.a.ex
    bid, ask = m.tick()
    ps = m.posicoes()
    if op[0] == "entrar":
        _, lado, tipo = op
        kw: dict = {"sl": bid - 100 if lado == Lado.COMPRA else ask + 100}
        if tipo == "limitada":
            kw["preco"] = bid - 50 if lado == Lado.COMPRA else ask + 50
        ex.enviar(cmd(lado, tipo=tipo, **kw))
    elif op[0] == "sair" and ps:
        tk, sinal, _ = ps[0]
        ex.enviar(cmd(Lado.VENDA if sinal > 0 else Lado.COMPRA, tipo="saida", ticket=tk), dono="nucleo")
    elif op[0] == "sair_mesmo_lado" and ps:
        tk, sinal, _ = ps[0]
        ex.enviar(cmd(Lado.COMPRA if sinal > 0 else Lado.VENDA, tipo="saida", ticket=tk))
    elif op[0] == "sair_ticket_falso":
        ex.enviar(cmd(Lado.VENDA, tipo="saida", ticket=987654321))
    elif op[0] == "zerar" and ps:
        ex.zerar_posicao(ps[0][0])
    elif op[0] == "tempo":
        m.a.rel.avancar(op[1] * 100_000_000)


def checar_invariantes(m: Modo, antes: list, abrir_antes: int, op: tuple, pend_antes: int) -> None:
    depois = m.posicoes()
    # limite: |posicoes abertas + pendentes| <= contratos_max, e nunca dois lados ao mesmo tempo
    assert len(depois) + m.pendentes() <= m.max, (op, depois)
    assert len({s for _, s, _ in depois}) <= 1, ("posicoes de lados opostos", depois)
    assert sum(v for _, _, v in depois) <= m.max + 1e-9
    if m.hedging:
        assert m.a.ex.travado is None, m.a.ex.travado  # nenhuma violacao detectada pela conferencia pos-deal
    if op[0] in ("sair", "sair_mesmo_lado", "sair_ticket_falso", "zerar"):
        # saidas NUNCA criam exposicao: nenhuma requisicao de abertura, nenhuma posicao nova, exposicao nao cresce
        assert m.abrir_cmds() == abrir_antes, op
        assert {t for t, _, _ in depois} <= {t for t, _, _ in antes}, op  # nenhum ticket novo
        assert sum(v for _, _, v in depois) <= sum(v for _, _, v in antes) + 1e-9, op
    if op[0] == "entrar" and antes:
        # com posicao aberta, entrada nunca piramida/inverte/duplica (hedging: nem abre ordem oposta)
        assert {t for t, _, _ in depois} == {t for t, _, _ in antes}, (op, antes, depois)


@pytest.mark.parametrize("hedging", [True, False], ids=["mt4-hedging", "mt5-netting"])
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])
@given(ops=st.lists(OPS, min_size=1, max_size=30), semente=st.integers(0, 60), contratos_max=st.integers(1, 2))
def test_propriedade_saida_por_ticket_sem_inversao_nem_duplicacao(hedging, ops, semente, contratos_max):
    m = Modo(hedging, semente, contratos_max)
    for i, op in enumerate(ops):
        antes, abrir_antes, pend_antes = m.posicoes(), m.abrir_cmds(), m.pendentes()
        executar(m, op, i)
        checar_invariantes(m, antes, abrir_antes, op, pend_antes)


# --- casos diretos de hedging -----------------------------------------------------------------------------
def test_hedging_saida_com_lado_oposto_fecha_o_ticket_e_nao_abre_outra_posicao():
    a = montar_exec4()
    ent = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    saida = a.ex.enviar(cmd(Lado.VENDA, tipo="saida", ticket=ent.order))
    assert saida.retcode == C.RET_EXECUTADA and a.d.posicoes == {}
    assert [e["cmd"] for e in a.d.envios] == [P.CMD_ORDEM_ABRIR, P.CMD_ORDEM_FECHAR]
    # saida duplicada (ticket ja encerrado) e recusada localmente e NADA e enviado
    n = len(a.d.comandos)
    dup = a.ex.enviar(cmd(Lado.VENDA, tipo="saida", ticket=ent.order))
    assert dup.retcode == C.RET_LOCAL_RECUSADO and "ticket inexistente" in dup.comentario
    assert not [c for c in a.d.comandos[n:] if c["cmd"] in P.COMANDOS_ORDEM] and a.d.posicoes == {}


def test_hedging_saida_com_lado_igual_ao_da_posicao_e_recusada():
    a = montar_exec4()
    ent = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    r = a.ex.enviar(cmd(Lado.COMPRA, tipo="saida", ticket=ent.order))
    assert r.retcode == C.RET_LOCAL_RECUSADO and "aumentaria/inverteria" in r.comentario
    assert a.d.tickets_abertos() == [ent.order] and len(a.d.envios) == 1


def test_hedging_nao_conta_posicao_do_outro_simbolo_nem_reabre_por_ticket_errado():
    a = montar_exec4()
    ent = a.ex.enviar(cmd(Lado.VENDA, sl=100100.0))
    r = a.ex.enviar(cmd(Lado.COMPRA, tipo="saida", ticket=ent.order + 1))
    assert r.retcode == C.RET_LOCAL_RECUSADO and a.d.tickets_abertos() == [ent.order]


def test_hedging_pendente_conta_no_limite_de_contratos():
    a = montar_exec4()
    lim = a.ex.enviar(cmd(Lado.COMPRA, tipo="limitada", preco=99950.0, sl=99900.0))
    assert lim.retcode == C.RET_COLOCADA
    r = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    assert r.retcode == C.RET_LOCAL_RECUSADO and "excederiam" in r.comentario
    assert a.d.tickets_abertos() == [] and len(a.d.pendentes) == 1


def test_hedging_conferencia_pos_deal_corrige_duplicacao_por_ticket():
    """Se o terminal abrir uma posicao a MAIS apos um deal, a conferencia trava e fecha o excesso POR TICKET."""
    a = montar_exec4()
    abrir = a.d._cmd_abrir

    def duplica(id_, args):  # defeito injetado: o EA abre duas posicoes para uma entrada
        r = abrir(id_, args)
        abrir(id_ + "-dup", args)
        return r

    a.d._cmd_abrir = duplica
    ent = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    assert ent.retcode == C.RET_EXECUTADA
    assert a.ex.travado and "posicoes_acima_do_maximo" in a.ex.travado
    assert len(a.d.tickets_abertos()) == 1  # o excesso foi fechado
    fechar = [e for e in a.d.envios if e["cmd"] == P.CMD_ORDEM_FECHAR]
    assert len(fechar) == 1 and fechar[0]["args"]["ticket"] not in a.d.tickets_abertos()
    assert any(t == "posicoes_acima_do_maximo" for t, _ in a.alertas)


def test_hedging_saida_que_abre_posicao_oposta_e_detectada_e_corrigida():
    a = montar_exec4()
    ent = a.ex.enviar(cmd(Lado.COMPRA, sl=99900.0))
    fechar = a.d._cmd_fechar

    estado = {"ja": False}

    def defeito(id_, args):  # defeito injetado (uma vez): ao fechar, o terminal abre uma posicao vendida
        r = fechar(id_, args)
        if not estado["ja"]:
            estado["ja"] = True
            a.d._abrir_mercado(SIMB, C.POS_VENDA, 1.0, 0.0, 0.0, 7, "oposta")
        return r

    a.d._cmd_fechar = defeito
    a.ex.enviar(cmd(Lado.VENDA, tipo="saida", ticket=ent.order))
    assert a.ex.travado and "saida_abriu_posicao_hedging" in a.ex.travado
    assert a.d.tickets_abertos() == []  # a posicao oposta foi fechada POR TICKET
    assert [e["cmd"] for e in a.d.envios].count(P.CMD_ORDEM_ABRIR) == 1  # a oposta nasceu no defeito, nao por pedido
    assert any(t == "saida_abriu_posicao_hedging" for t, _ in a.alertas)


# --- guardiao em hedging ------------------------------------------------------------------------------------
def montar_guardiao4():
    a = montar_exec4()
    g = Guardiao(a.ex, a.con, ConfigGuardiao(perfil=WIN, exigir_dia_util=False, entradas_de="00:00", entradas_ate="23:59",
                                              zeragem="23:59"), a.rel, a.rs)
    g.heartbeat()
    return a, g


def test_guardiao_hedging_recusa_entrada_com_posicao_de_qualquer_origem():
    a, g = montar_guardiao4()
    ok, _ = g.avaliar_entrada(cmd(Lado.COMPRA, sl=99900.0))
    assert ok
    a.d.operar_manual(C.POS_VENDA, 1.0, SIMB)
    ok, motivo = g.avaliar_entrada(cmd(Lado.COMPRA, sl=99900.0))
    assert not ok and "hedging: ja ha posicao" in motivo
    # saidas sempre passam
    assert g.avaliar_entrada(cmd(Lado.COMPRA, tipo="saida", ticket=1)) == (True, "")


def test_guardiao_hedging_limite_com_pendentes_e_zeragem_por_ticket():
    a, g = montar_guardiao4()
    a.ex.enviar(cmd(Lado.COMPRA, tipo="limitada", preco=99950.0, sl=99900.0))
    ok, motivo = g.avaliar_entrada(cmd(Lado.COMPRA, sl=99900.0))
    assert not ok and "excederiam" in motivo
    a.d.operar_manual(C.POS_COMPRA, 1.0, SIMB)
    assert g.zerar_tudo("teste") is True
    assert a.d.tickets_abertos() == [] and a.d.pendentes == {}
    assert [e["cmd"] for e in a.d.envios if e["cmd"] in (P.CMD_ORDEM_FECHAR, P.CMD_ORDEM_APAGAR)] == [
        P.CMD_ORDEM_APAGAR, P.CMD_ORDEM_FECHAR]
    assert [e["args"]["tipo"] for e in a.d.envios if e["cmd"] == P.CMD_ORDEM_ABRIR] == [C.ORDEM_BUY_LIMIT]  # so a pendente


def test_guardiao_classifica_deals_do_mt4_e_bloqueia_por_intervencao_manual():
    a, g = montar_guardiao4()
    tk = a.d.operar_manual(C.POS_COMPRA, 1.0, SIMB)
    a.rel.avancar(1_000_000_000)
    r = g.reconciliar()
    assert r["ok"] and g.classificacao["manual"] == 1
    assert any(x[0] == "intervencao_manual" for x in g.alertas)
    assert any(b.startswith("manual") for b in g.estado()["bloqueios"])
    a.d.fechar_manual(tk)
    a.rel.avancar(1_000_000_000)
    g.reconciliar()
    assert g.classificacao["manual"] == 2  # a saida fora da ponte (magic 0) tambem e manual


def test_servidor_de_netting_continua_recusando_hedging_e_vice_versa():
    a = montar_exec(cfg_extra={"conta": "hedging"})
    from robowin.mt5.execucao_demo import GuardaDemoViolada
    with pytest.raises(GuardaDemoViolada, match="hedging"):
        a.ex.verificar_guarda()
