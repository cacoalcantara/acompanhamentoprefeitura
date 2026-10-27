import ast
import datetime as dt
from pathlib import Path

import pytest

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    AckOrdem,
    ConfigRisco,
    EstadoOrdem,
    Instrumento,
    Intencao,
    Lado,
    Posicao,
    Tick,
)
from robowin.execucao.oms import OMS
from robowin.execucao.politicas import PoliticaExecucao, PoliticaPadrao, PoliticaPassiva, tipo_de_ordem
from robowin.execucao.roteador import ExecucaoRealNaoAutorizada, Roteador
from robowin.execucao.simulador import SimuladorExecucao
from robowin.risco.kill_switch import KillSwitch
from robowin.risco.motor import MotorRiscoPadrao

RAIZ = Path(__file__).resolve().parents[2] / "src" / "robowin"
MS = 1_000_000
T0 = dt_utc_ns(dt.datetime(2026, 11, 10, 10, 0))
INS = Instrumento(simbolo="WINZ26")


def tick(rel, dt_ms, bid, ask, last=None, vol=1.0):
    t = T0 + dt_ms * MS
    return Tick(t_servidor_ms=t // MS, t_utc_ns=t, t_recebido_ns=t, bid=bid, ask=ask,
                last=last if last is not None else bid, volume=vol)


def montar():
    rel = RelogioSimulado(T0)
    sim = SimuladorExecucao(INS, rel, latencia_ns=0, seed=2)
    sim.registrar_braco("b1", 11)
    ks = KillSwitch()
    motor = MotorRiscoPadrao(ConfigRisco(), INS, kill_switch=ks)
    oms = OMS(rel, {"b1": 11}, janela_duplicada_s=0.0)
    rot = Roteador("simulado", INS, rel, oms, motor, simulador=sim)
    ks.registrar_efeito(rot.efeito_kill)
    return rel, sim, ks, motor, oms, rot


def intencao(acao=Acao.COMPRAR, stop=100.0, alvo=None, iid=None, t=T0):
    return Intencao(id=iid or f"{acao.value}-{t}", braco="b1", acao=acao, stop_pontos=stop, alvo_pontos=alvo,
                    validade_ns=t + int(60e9), preco_referencia=130005.0)


# ---- garantias estaticas -----------------------------------------------------------------------------------
@pytest.mark.parametrize("pacote", ["execucao", "risco"])
def test_sem_metatrader5_e_sem_order_send(pacote):
    for arq in (RAIZ / pacote).glob("*.py"):
        arvore = ast.parse(arq.read_text())
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import):
                assert not any(a.name.split(".")[0] == "MetaTrader5" for a in no.names), arq
            if isinstance(no, ast.ImportFrom):
                assert (no.module or "").split(".")[0] != "MetaTrader5", arq
            if isinstance(no, ast.Call):
                f = no.func
                nome = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                assert nome != "order_send", f"{arq}: chamada a order_send"


def test_modo_real_nao_autorizado():
    rel = RelogioSimulado(T0)
    with pytest.raises(ExecucaoRealNaoAutorizada):
        Roteador("real", INS, rel, OMS(rel, {"b1": 11}), MotorRiscoPadrao(ConfigRisco(), INS))


def test_politicas():
    assert PoliticaPadrao().escolher({}) == "agredir" and tipo_de_ordem("agredir") == "mercado"
    assert isinstance(PoliticaPadrao(), PoliticaExecucao)
    assert PoliticaPassiva().escolher({"spread_ticks": 3.0}) == "passiva"
    assert PoliticaPassiva().escolher({"spread_ticks": 1.0}) == "agredir"
    assert tipo_de_ordem("qualquer") == "mercado"  # desconhecido => seguro


# ---- pipeline simulado ---------------------------------------------------------------------------------------
def test_entrada_aprovada_gera_ordem_com_stop_e_posicao_aberta():
    rel, sim, ks, motor, oms, rot = montar()
    sim.on_tick(tick(rel, -10, 130000.0, 130005.0))
    r = rot.executar_intencao(intencao(alvo=200.0), sim.posicao("b1"), T0)
    assert r.decisao.aprovada and r.cmd.tipo == "mercado" and r.cmd.magic == 11
    assert r.cmd.sl == 129905.0 and r.cmd.tp == 130205.0  # ref 130005 -/+ stop 100 / alvo 200
    assert r.envio.ordem.estado == EstadoOrdem.ACEITA and motor.pendentes_braco("b1") == 1
    rot.processar_eventos(sim.on_tick(tick(rel, 1, 130000.0, 130005.0)), T0)
    assert sim.posicao("b1").quantidade == 1 and sim.posicao("b1").stop == 129905.0
    assert r.envio.ordem.estado == EstadoOrdem.EXECUTADA and motor.pendentes_braco("b1") == 0


def test_segunda_entrada_e_vetada_por_pendente_e_depois_por_posicao():
    rel, sim, ks, motor, oms, rot = montar()
    sim.on_tick(tick(rel, -10, 130000.0, 130005.0))
    rot.executar_intencao(intencao(iid="a"), sim.posicao("b1"), T0)
    r2 = rot.executar_intencao(intencao(Acao.VENDER, iid="b"), sim.posicao("b1"), T0)
    assert not r2.decisao.aprovada and "ordem_de_entrada_pendente" in r2.decisao.motivos
    rot.processar_eventos(sim.on_tick(tick(rel, 1, 130000.0, 130005.0)), T0)
    r3 = rot.executar_intencao(intencao(iid="c"), sim.posicao("b1"), T0)
    assert "sem_aumento_de_posicao" in r3.decisao.motivos
    assert len(sim.ordens_abertas("b1")) == 0 and sim.posicao("b1").quantidade == 1


def test_intencao_vetada_nao_chega_ao_oms():
    rel, sim, ks, motor, oms, rot = montar()
    r = rot.executar_intencao(intencao(stop=None), sim.posicao("b1"), T0)
    assert not r.decisao.aprovada and r.envio is None and not oms.abertas()


def test_encerrar_zera_sem_inverter_e_registra_trade_no_risco():
    rel, sim, ks, motor, oms, rot = montar()
    sim.on_tick(tick(rel, -10, 130000.0, 130005.0))
    rot.executar_intencao(intencao(iid="a"), sim.posicao("b1"), T0)
    rot.processar_eventos(sim.on_tick(tick(rel, 1, 130000.0, 130005.0)), T0)
    rel.avancar_para(T0 + 50 * MS)
    r = rot.executar_intencao(intencao(Acao.ENCERRAR, stop=None, iid="enc"), sim.posicao("b1"), T0 + 50 * MS)
    assert r.cmd.tipo == "saida" and r.cmd.lado == Lado.VENDA and r.cmd.position_ticket == sim.posicao("b1").ticket
    rot.processar_eventos(sim.on_tick(tick(rel, 60, 129990.0, 129995.0)), T0 + 60 * MS)
    assert sim.posicao("b1").quantidade == 0 and sim.posicao("b1").lado is None
    est = motor.contab.estado("b1", T0)
    assert est.pnl_dia < 0 and est.perdas_seguidas == 1  # perda realizada alimentou o risco


def test_stop_do_simulador_alimenta_risco_e_libera():
    rel, sim, ks, motor, oms, rot = montar()
    sim.on_tick(tick(rel, -10, 130000.0, 130005.0))
    rot.executar_intencao(intencao(iid="a", stop=100.0), sim.posicao("b1"), T0)
    rot.processar_eventos(sim.on_tick(tick(rel, 1, 130000.0, 130005.0)), T0)
    rot.processar_eventos(sim.on_tick(tick(rel, 100, 129890.0, 129895.0, last=129900.0)), T0)
    rot.processar_eventos(sim.on_tick(tick(rel, 200, 129880.0, 129885.0, last=129880.0)), T0)
    assert sim.posicao("b1").quantidade == 0
    assert motor.contab.estado("b1", T0).pnl_dia < -20
    assert sim.carteira("b1").trades[0].motivo_saida == "stop"


def test_zeragem_no_horario():
    rel, sim, ks, motor, oms, rot = montar()
    sim.on_tick(tick(rel, -10, 130000.0, 130005.0))
    rot.executar_intencao(intencao(iid="a"), sim.posicao("b1"), T0)
    rot.processar_eventos(sim.on_tick(tick(rel, 1, 130000.0, 130005.0)), T0)
    assert rot.verificar_zeragem("b1", T0 + int(3600e9)) is None
    t = dt_utc_ns(dt.datetime(2026, 11, 10, 17, 45))
    rel.avancar_para(t)
    env = rot.verificar_zeragem("b1", t)
    assert env is not None and env.ordem.cmd.tipo == "saida"
    ev = sim.on_tick(Tick(t_servidor_ms=t // MS, t_utc_ns=t + MS, t_recebido_ns=t, bid=130000.0, ask=130005.0,
                          last=130000.0, volume=1))
    rot.processar_eventos(ev, t)
    assert sim.posicao("b1").quantidade == 0 and sim.carteira("b1").trades[0].motivo_saida == "zeragem"


def test_kill_switch_cancela_zera_e_bloqueia():
    rel, sim, ks, motor, oms, rot = montar()
    sim.on_tick(tick(rel, -10, 130000.0, 130005.0))
    rot.executar_intencao(intencao(iid="a"), sim.posicao("b1"), T0)
    rot.processar_eventos(sim.on_tick(tick(rel, 1, 130000.0, 130005.0)), T0)
    assert sim.posicao("b1").quantidade == 1
    rel.avancar_para(T0 + 100 * MS)
    ks.acionar("teste", "manual_cli", T0 + 100 * MS)
    assert any(o.kind == "saida" for o in sim.ordens_abertas("b1"))  # zeragem emitida
    rot.processar_eventos(sim.on_tick(tick(rel, 200, 130000.0, 130005.0)), T0 + 200 * MS)
    assert sim.posicao("b1").quantidade == 0
    r = rot.executar_intencao(intencao(iid="nova"), sim.posicao("b1"), T0 + 300 * MS)
    assert "kill_switch_ativo" in r.decisao.motivos


def test_bloqueio_externo_da_reconciliacao():
    rel = RelogioSimulado(T0)
    sim = SimuladorExecucao(INS, rel)
    sim.registrar_braco("b1", 11)
    rot = Roteador("simulado", INS, rel, OMS(rel, {"b1": 11}), MotorRiscoPadrao(ConfigRisco(), INS), simulador=sim,
                   bloqueio_externo=lambda: "divergencia")
    r = rot.executar_intencao(intencao(), Posicao(simbolo="WINZ26"), T0)
    assert not r.decisao.aprovada and r.decisao.motivos == ("bloqueio:divergencia",)


# ---- gateway demo (sem order_send aqui) ----------------------------------------------------------------------
def test_gateway_demo_apenas_emite_comando_via_callback():
    rel = RelogioSimulado(T0)
    emitidos = []

    def emissor(cmd):
        emitidos.append(cmd)
        return AckOrdem(retcode=10008, order=900)
    oms = OMS(rel, {"b1": 11})
    motor = MotorRiscoPadrao(ConfigRisco(), INS)
    rot = Roteador("gateway_demo", INS, rel, oms, motor, emissor_gateway=emissor)
    r = rot.executar_intencao(intencao(), Posicao(simbolo="WINZ26"), T0)
    assert len(emitidos) == 1 and emitidos[0].sl == 129905.0
    assert r.envio.ordem.estado == EstadoOrdem.ACEITA and oms.por_ticket(900) is r.envio.ordem


def test_gateway_sem_ack_vira_incerta_e_bloqueia_novas_entradas():
    rel = RelogioSimulado(T0)
    oms = OMS(rel, {"b1": 11})
    motor = MotorRiscoPadrao(ConfigRisco(), INS)
    chamadas = []
    rot = Roteador("gateway_demo", INS, rel, oms, motor, emissor_gateway=lambda c: chamadas.append(c))
    r = rot.executar_intencao(intencao(iid="a"), Posicao(simbolo="WINZ26"), T0)
    assert r.envio.ordem.estado == EstadoOrdem.INCERTA and motor.pendentes_braco("b1") == 1
    rel.avancar(int(30e9))
    r2 = rot.executar_intencao(intencao(Acao.VENDER, iid="b"), Posicao(simbolo="WINZ26"), T0 + int(30e9))
    assert not r2.decisao.aprovada and len(chamadas) == 1  # nunca reenvia nem abre outra
