"""WP2: protecao (SL/TP) modificavel, alvo com q>1, parciais e ajuste do braco planejador no roteador."""
import datetime as dt

import pytest
from perfis_teste import WIN

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    AckOrdem,
    AjustePosicao,
    ComandoOrdem,
    ConfigRisco,
    DecisaoRisco,
    Intencao,
    Lado,
    Posicao,
    Tick,
)
from robowin.execucao.aprovacao import ErroOrdemNaoAprovada
from robowin.execucao.oms import OMS
from robowin.execucao.roteador import Roteador
from robowin.execucao.simulador import RET_INVALID_STOPS, RET_PLACED, SimuladorExecucao
from robowin.risco.motor import MotorRiscoPadrao

T0 = dt_utc_ns(dt.datetime(2026, 11, 10, 10, 0))
MS = 1_000_000
MAGIC = 11
INS = WIN.instrumento("WINZ26")
TK = INS.tick_pontos


def tick(dt_ms, bid, ask, last=None, vol=1.0):
    t = T0 + dt_ms * MS
    return Tick(t_servidor_ms=t // MS, t_utc_ns=t, t_recebido_ns=t, bid=bid, ask=ask,
                last=last if last is not None else bid, volume=vol)


def cmd(lado=Lado.COMPRA, tipo="mercado", q=1, preco=None, sl=None, tp=None, ticket=None, iid="i"):
    return ComandoOrdem(intencao_id=iid, magic=MAGIC, lado=lado, tipo=tipo, quantidade=q, preco=preco, sl=sl, tp=tp,
                        position_ticket=ticket)


def novo_sim():
    rel = RelogioSimulado(T0)
    s = SimuladorExecucao(INS, rel, latencia_ns=0, seed=3)
    s.registrar_braco("b1", MAGIC)
    return s, rel


def abrir(s, q=1, sl=129910.0, tp=None, lado=Lado.COMPRA):
    """Abre posicao a 130010 (compra, ask) ou 130000 (venda, bid)."""
    s.on_tick(tick(-1000, 130000.0, 130010.0))
    assert s.enviar(cmd(lado=lado, q=q, sl=sl, tp=tp, iid="ent")).retcode == RET_PLACED
    ev = s.on_tick(tick(10, 130000.0, 130010.0))
    assert s.posicao("b1").quantidade == q
    return ev


def alvos(s):
    return [o for o in s.ordens_abertas("b1") if o.kind == "alvo"]


# ---- simulador: modificar_protecao ---------------------------------------------------------------------------
def test_aperto_aplicado_e_afrouxamento_recusado():
    s, _ = novo_sim()
    abrir(s)
    tk = s.posicao("b1").ticket
    assert s.modificar_protecao("b1", tk, 129950.0, None) is None
    assert s.posicao("b1").stop == 129950.0
    assert s.modificar_protecao("b1", tk, 129900.0, None) == "stop_afrouxaria"
    assert s.posicao("b1").stop == 129950.0
    assert s.modificar_stop("b1", 129960.0) and not s.modificar_stop("b1", 129955.0)


def test_recusas_de_protecao_sao_atomicas_e_nomeadas():
    s, _ = novo_sim()
    assert s.modificar_protecao("b1", None, 129950.0, None) == "sem_posicao"
    abrir(s, tp=130100.0)
    tk = s.posicao("b1").ticket
    assert s.modificar_protecao("b1", tk + 999, 129950.0, None) == "ticket_divergente"
    assert s.modificar_protecao("b1", tk, 129951.0, None) == "fora_da_grade"
    assert s.modificar_protecao("b1", tk, 130000.0, None) == "stop_lado_errado"  # stop no bid de saida
    assert s.modificar_protecao("b1", tk, 129950.0, 130000.0) == "alvo_lado_errado"
    p = s.posicao("b1")
    assert p.stop == 129910.0 and p.alvo == 130100.0  # nada aplicado (tudo ou nada)


def test_stop_pendente_recusa_modificacao():
    s, _ = novo_sim()
    abrir(s)
    s.on_tick(tick(20, 129900.0, 129905.0, last=129905.0))  # last <= sl: stop disparado, execucao no proximo tick
    assert s.modificar_protecao("b1", None, 129950.0, None) == "stop_pendente"


def test_tp_novo_cancela_e_recria_alvo_com_fila_do_zero():
    s, _ = novo_sim()
    abrir(s, tp=130100.0)
    antigo = alvos(s)
    assert len(antigo) == 1
    s.on_tick(tick(20, 130000.0, 130010.0))  # ativa o alvo (fila estimada)
    assert alvos(s)[0].fila is not None
    assert s.modificar_protecao("b1", None, None, 130080.0) is None
    novo = alvos(s)
    assert len(novo) == 1 and novo[0].ticket != antigo[0].ticket and novo[0].preco == 130080.0
    assert novo[0].fila is None and not novo[0].ativada and s.posicao("b1").alvo == 130080.0
    assert s.modificar_protecao("b1", None, None, 130080.0) is None
    assert alvos(s)[0].ticket == novo[0].ticket  # mesmo TP: fila preservada


def test_venda_lado_certo_da_protecao():
    s, _ = novo_sim()
    abrir(s, lado=Lado.VENDA, sl=130100.0, tp=129900.0)
    assert s.modificar_protecao("b1", None, 130010.0, None) == "stop_lado_errado"  # no ask de saida
    assert s.modificar_protecao("b1", None, 130050.0, 129950.0) is None
    assert s.modificar_protecao("b1", None, 130080.0, None) == "stop_afrouxaria"


# ---- simulador: entrada com SL/TP do lado errado -------------------------------------------------------------
def test_entrada_com_sl_ou_tp_do_lado_errado_e_recusada():
    s, _ = novo_sim()
    s.on_tick(tick(-1000, 130000.0, 130010.0))
    assert s.enviar(cmd(sl=130005.0)).retcode == RET_INVALID_STOPS
    assert s.enviar(cmd(sl=129900.0, tp=129995.0)).retcode == RET_INVALID_STOPS
    assert s.enviar(cmd(lado=Lado.VENDA, sl=129995.0)).retcode == RET_INVALID_STOPS
    assert s.enviar(cmd(tipo="limitada", preco=129900.0, sl=129950.0)).retcode == RET_INVALID_STOPS
    assert s.enviar(cmd(tipo="limitada", preco=129900.0, sl=129850.0, tp=129950.0)).retcode == RET_PLACED
    assert s.enviar(cmd(sl=129900.0, tp=130100.0)).retcode == RET_PLACED


# ---- simulador: alvo com q>1 e parciais ----------------------------------------------------------------------
def test_alvo_com_q3_fecha_as_3_unidades():
    s, _ = novo_sim()
    abrir(s, q=3, tp=130100.0)
    assert [o.restante for o in alvos(s)] == [3]
    ev = s.on_tick(tick(20, 130100.0, 130110.0, last=130105.0, vol=5))
    ev += s.on_tick(tick(30, 130100.0, 130110.0, last=130105.0, vol=5))
    trades = [e for e in ev if e.tipo == "trade"]
    assert s.posicao("b1").quantidade == 0
    assert sum(e.quantidade for e in trades) == 3 and trades[-1].restante == 0


def test_parcial_redimensiona_alvo_e_stop_age_sobre_o_restante():
    s, rel = novo_sim()
    abrir(s, q=3, sl=129910.0, tp=130100.0)
    rel.avancar_para(T0 + 15 * MS)
    s.enviar(cmd(lado=Lado.VENDA, tipo="saida", q=1, ticket=s.posicao("b1").ticket, iid="parcial"))
    ev = s.on_tick(tick(20, 130000.0, 130010.0))
    tr = [e for e in ev if e.tipo == "trade"]
    assert len(tr) == 1 and tr[0].quantidade == 1 and tr[0].restante == 2
    assert s.posicao("b1").quantidade == 2 and [o.restante for o in alvos(s)] == [2]
    assert s.posicao("b1").stop == 129910.0 and s.posicao("b1").alvo == 130100.0  # protecao mantida
    ev = s.on_tick(tick(30, 129900.0, 129905.0, last=129905.0))
    ev += s.on_tick(tick(40, 129900.0, 129905.0, last=129900.0))
    tr = [e for e in ev if e.tipo == "trade"]
    assert len(tr) == 1 and tr[0].quantidade == 2 and tr[0].restante == 0
    assert s.posicao("b1").quantidade == 0 and not alvos(s)
    assert [t.motivo_saida for t in s.carteira("b1").trades] == ["saida", "stop"]


def test_alvo_parcialmente_preenchido_nao_excede_a_posicao():
    s, rel = novo_sim()
    abrir(s, q=3, tp=130100.0)
    s.on_tick(tick(20, 130000.0, 130010.0))  # ativa o alvo com fila padrao (10)
    ev = s.on_tick(tick(30, 130095.0, 130100.0, last=130100.0, vol=11))  # 1 unidade passa a fila
    assert [e.restante for e in ev if e.tipo == "trade"] == [2]
    rel.avancar_para(T0 + 35 * MS)
    s.enviar(cmd(lado=Lado.VENDA, tipo="saida", q=1, ticket=s.posicao("b1").ticket, iid="parcial"))
    s.on_tick(tick(40, 130000.0, 130010.0))
    assert s.posicao("b1").quantidade == 1
    a = alvos(s)
    assert [o.restante for o in a] == [1] and a[0].fila is not None and a[0].fila.restante == 1


def test_posicao_guarda_intencao_que_abriu():
    s, _ = novo_sim()
    abrir(s)
    assert s.carteira("b1").pos.intencao_id == "ent"


# ---- roteador ------------------------------------------------------------------------------------------------
class Aprova:
    """Motor dublê que aprova tudo: isola o roteador/simulador do veto (o veto e coberto no WP1)."""

    def avaliar(self, it, pos, t):
        return DecisaoRisco(intencao_id=it.id, aprovada=True)


class MotorEspiao(MotorRiscoPadrao):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.parciais, self.fechados = [], []

    def registrar_parcial(self, braco, liquido_brl, quantidade, t_ns):
        self.parciais.append((braco, quantidade))

    def registrar_trade(self, braco, liquido_brl, quantidade, t_ns):
        self.fechados.append((braco, quantidade))
        return super().registrar_trade(braco, liquido_brl, quantidade, t_ns)


def montar(motor=None):
    s, rel = novo_sim()
    motor = motor or Aprova()
    rot = Roteador("simulado", INS, rel, OMS(rel, {"b1": MAGIC}, janela_duplicada_s=0.0), motor, simulador=s)
    return s, rel, rot


def manter(pos, iid="aj", **ajuste):
    return Intencao(id=iid, braco="b1", acao=Acao.MANTER, validade_ns=T0 + int(60e9), preco_referencia=130005.0,
                    posicao_ticket=pos.ticket, quantidade_t0=pos.quantidade, ajuste=AjustePosicao(**ajuste))


def test_exigir_aprovacao_e_o_padrao():
    s, rel, rot = montar()
    assert rot.exigir_aprovacao is True
    with pytest.raises(ErroOrdemNaoAprovada):
        rot.enviar(cmd(sl=129900.0, iid="sem-risco"))
    assert rot.enviar(cmd(lado=Lado.VENDA, tipo="saida", iid="zerar")).retcode is not None  # saida nunca espera


def test_ajuste_aplica_niveis_desde_o_preco_medio_arredondados_para_o_preco():
    s, rel, rot = montar()
    abrir(s, tp=130100.0)  # preco medio 130010
    pos = s.posicao("b1")
    r = rot.executar_intencao(manter(pos, stop_resultado_pontos=-32.0, alvo_resultado_pontos=58.0), pos, T0)
    # stop 129978 -> 129980 (sobe, em direcao ao preco); alvo 130068 -> 130065 (desce, em direcao ao preco)
    assert r.motivo == "nada_a_enviar" and r.niveis_aplicados == (129980.0, 130065.0)
    assert s.posicao("b1").stop == 129980.0 and s.posicao("b1").alvo == 130065.0
    assert r.niveis_aplicados[0] % TK == 0


def test_ajuste_em_venda_e_empate():
    s, rel, rot = montar()
    abrir(s, lado=Lado.VENDA, sl=130100.0)  # preco medio 130000
    s.on_tick(tick(20, 129950.0, 129960.0))
    pos = s.posicao("b1")
    r = rot.executar_intencao(manter(pos, stop_resultado_pontos=0.0), pos, T0)
    assert r.niveis_aplicados == (130000.0, None) and s.posicao("b1").stop == 130000.0


def test_recusa_do_simulador_aparece_no_motivo():
    s, rel, rot = montar()
    abrir(s)  # stop inicial 129910 (resultado -100)
    pos = s.posicao("b1")
    r = rot.executar_intencao(manter(pos, stop_resultado_pontos=-200.0), pos, T0)
    assert r.decisao.aprovada and r.motivo == "protecao_nao_aplicada:stop_afrouxaria"
    assert r.niveis_aplicados is None and s.posicao("b1").stop == 129910.0
    velho = pos.model_copy(update={"ticket": (pos.ticket or 0) + 50})
    r = rot.executar_intencao(manter(velho, iid="aj2", stop_resultado_pontos=-50.0), pos, T0)
    assert r.motivo == "protecao_nao_aplicada:ticket_divergente"


def test_manter_legado_confere_o_retorno_do_simulador():
    s, rel, rot = montar()
    abrir(s)
    pos = s.posicao("b1")
    it = Intencao(id="leg", braco="b1", acao=Acao.MANTER, stop_pontos=500.0, validade_ns=T0 + int(60e9),
                  preco_referencia=130005.0)
    r = rot.executar_intencao(it, pos, T0)
    assert r.motivo == "protecao_nao_aplicada:stop_afrouxaria" and s.posicao("b1").stop == 129910.0
    ok = it.model_copy(update={"id": "leg2", "stop_pontos": 50.0})
    r = rot.executar_intencao(ok, pos, T0)
    assert r.motivo == "nada_a_enviar" and r.niveis_aplicados == (129955.0, None)


def test_reducao_vira_saida_parcial_e_registra_parcial_no_risco():
    motor = MotorEspiao(ConfigRisco(), INS)
    s, rel, rot = montar(motor)
    abrir(s, q=3, tp=130100.0)
    pos = s.posicao("b1")
    rel.avancar_para(T0 + 15 * MS)
    r = rot.executar_intencao(manter(pos, reduzir_quantidade=1), pos, T0 + 15 * MS)
    assert r.decisao.aprovada, r.decisao.motivos
    assert r.cmd is not None and r.cmd.tipo == "saida" and r.cmd.quantidade == 1
    assert r.cmd.position_ticket == pos.ticket and r.envio is not None and r.envio.enviado
    rot.processar_eventos(s.on_tick(tick(20, 130000.0, 130010.0)), T0 + 20 * MS)
    assert s.posicao("b1").quantidade == 2 and [o.restante for o in alvos(s)] == [2]
    assert motor.parciais == [("b1", 1)] and motor.fechados == []
    rel.avancar_para(T0 + 25 * MS)
    fim = Intencao(id="enc", braco="b1", acao=Acao.ENCERRAR, validade_ns=T0 + int(60e9), posicao_ticket=pos.ticket)
    rot.executar_intencao(fim, s.posicao("b1"), T0 + 25 * MS)
    rot.processar_eventos(s.on_tick(tick(30, 130000.0, 130010.0)), T0 + 30 * MS)
    assert s.posicao("b1").quantidade == 0 and motor.fechados == [("b1", 2)]


@pytest.mark.parametrize("r", [0, 3, 5])
def test_reducao_que_zeraria_ou_aumentaria_nao_e_enviada(r):
    s, rel, rot = montar()
    abrir(s, q=3)
    pos = s.posicao("b1")
    res = rot.executar_intencao(manter(pos, reduzir_quantidade=r), pos, T0)
    assert res.cmd is None and res.motivo == "reducao_invalida" and s.posicao("b1").quantidade == 3


def test_encerrar_usa_o_ticket_visto_em_t0():
    s, rel, rot = montar()
    abrir(s)
    pos = s.posicao("b1")
    it = Intencao(id="enc", braco="b1", acao=Acao.ENCERRAR, validade_ns=T0 + int(60e9),
                  posicao_ticket=(pos.ticket or 0) + 7)
    r = rot.executar_intencao(it, pos, T0)
    assert r.cmd is not None and r.cmd.position_ticket == (pos.ticket or 0) + 7
    assert r.envio is not None and (not r.envio.enviado or r.envio.ordem.estado.value == "REJEITADA")
    assert s.posicao("b1").quantidade == 1  # ticket velho: o simulador nao fecha a posicao nova


def test_ajuste_sem_simulador_nao_e_dado_como_aplicado():
    rel = RelogioSimulado(T0)
    enviados: list[ComandoOrdem] = []

    def emissor(c):
        enviados.append(c)
        return AckOrdem(retcode=10008, order=901)
    rot = Roteador("gateway_demo", INS, rel, OMS(rel, {"b1": MAGIC}), Aprova(), emissor_gateway=emissor)
    pos = Posicao(simbolo=INS.simbolo, lado=Lado.COMPRA, quantidade=1, preco_medio=130010.0, ticket=5,
                  stop=129910.0)
    r = rot.executar_intencao(manter(pos, stop_resultado_pontos=-50.0), pos, T0)
    assert r.motivo == "protecao_nao_aplicada:sem_simulador" and r.niveis_aplicados is None and not enviados
