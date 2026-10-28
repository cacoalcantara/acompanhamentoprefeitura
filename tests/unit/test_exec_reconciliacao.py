from types import SimpleNamespace as NS

import pytest

from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import AckOrdem, ComandoOrdem, EstadoOrdem, Lado, Posicao
from robowin.execucao.oms import OMS
from robowin.execucao.reconciliacao import ClasseDeal, Reconciliador, classificar_deal
from robowin.risco.kill_switch import KillSwitch

S = int(1e9)
SIM = "WINZ26"


class Terminal:
    def __init__(self):
        self.pos, self.ordens, self.deals = [], [], []

    def positions_get(self, **kw):
        return tuple(self.pos)

    def orders_get(self, **kw):
        return tuple(self.ordens)

    def history_deals_get(self, *a, **kw):
        return tuple(self.deals)


def posicao(magic=11, tipo=0, vol=1, ticket=100):
    return NS(ticket=ticket, magic=magic, type=tipo, volume=vol, symbol=SIM, price_open=130000.0)


def deal(tk, reason=3, magic=11, order=500, vol=1, entry=0, time_msc=0, price=130000.0):
    return NS(ticket=tk, reason=reason, magic=magic, order=order, volume=vol, entry=entry, time_msc=time_msc,
              price=price, symbol=SIM)


def ordem_t(tk, magic=11, vol=1, time_setup_msc=0):
    return NS(ticket=tk, magic=magic, volume_current=vol, time_setup_msc=time_setup_msc, symbol=SIM)


class Aud:
    def __init__(self):
        self.l = []

    def registrar(self, tipo, dados):
        self.l.append((tipo, dados))
        return "h"


def montar(interna=None, **kw):
    rel = RelogioSimulado(1000 * S)
    oms = OMS(rel, {"b1": 11, "b2": 22})
    term = Terminal()
    interna = interna or {}
    ks = KillSwitch()
    aud = Aud()
    rec = Reconciliador(term, oms, SIM, lambda b: interna.get(b, Posicao(simbolo=SIM)), kill_switch=ks,
                        auditoria=aud, **kw)
    return rel, oms, term, interna, ks, rec, aud


def test_tudo_consistente():
    _, _, term, interna, ks, rec, _ = montar({"b1": Posicao(simbolo=SIM, lado=Lado.COMPRA, quantidade=1)})
    term.pos = [posicao()]
    r = rec.reconciliar(1000 * S)
    assert r.ok and not r.bloqueio and not ks.ativo and not rec.bloqueado


def test_par_compra_venda_de_liquido_zero_e_divergencia():
    _, _, term, _, _, rec, _ = montar({})
    term.pos = [posicao(ticket=100, tipo=0), posicao(ticket=101, tipo=1)]  # hedging: liquido 0, duas posicoes
    r = rec.reconciliar(1000 * S)
    assert not r.ok and r.bloqueio
    assert any(d.tipo == "posicoes_multiplas" and "100" in d.detalhe and "101" in d.detalhe for d in r.divergencias)


def test_posicao_divergente_bloqueia_e_aciona_kill():
    _, _, term, _, ks, rec, aud = montar({})
    term.pos = [posicao()]  # terminal tem posicao que o robo nao conhece
    r = rec.reconciliar(1000 * S)
    assert not r.ok and r.bloqueio and rec.bloqueado
    assert r.divergencias[0].tipo == "posicao_divergente" and "interna=0 terminal=1" in r.divergencias[0].detalhe
    assert ks.ativo and ks.ultimo.origem == "divergencia_reconciliacao" and r.kill_acionado
    assert any(t == "reconciliacao" for t, _ in aud.l)


def test_posicao_externa_magic_desconhecido():
    _, _, term, _, _, rec, _ = montar({})
    term.pos = [posicao(magic=999)]
    assert "posicao_externa" in {d.tipo for d in rec.reconciliar(1000 * S).divergencias}


def test_sinal_invertido_e_detectado():
    _, _, term, _, ks, rec, _ = montar({"b1": Posicao(simbolo=SIM, lado=Lado.COMPRA, quantidade=1)})
    term.pos = [posicao(tipo=1)]  # terminal vendido: inversao
    r = rec.reconciliar(1000 * S)
    assert r.divergencias[0].detalhe == "interna=1 terminal=-1" and ks.ativo


@pytest.mark.parametrize("reason,cls", [
    (3, ClasseDeal.ROBO), (4, ClasseDeal.SERVIDOR), (5, ClasseDeal.SERVIDOR), (6, ClasseDeal.SERVIDOR),
    (0, ClasseDeal.MANUAL), (1, ClasseDeal.MANUAL), (2, ClasseDeal.MANUAL), (99, ClasseDeal.DESCONHECIDO),
])
def test_classificacao_de_deals(reason, cls):
    assert classificar_deal(reason) == cls


def test_deals_sl_tp_esperados_nao_bloqueiam():
    _, _, term, _, ks, rec, _ = montar({})
    term.deals = [deal(1, reason=4), deal(2, reason=5), deal(3, reason=3)]
    r = rec.reconciliar(1000 * S)
    assert r.ok and r.deals_servidor == 2 and r.deals_robo == 1 and not r.bloqueio and not ks.ativo


def test_deal_manual_bloqueia_alerta_e_exige_reconhecimento():
    _, _, term, _, ks, rec, aud = montar({})
    term.deals = [deal(1, reason=0)]  # CLIENT
    r = rec.reconciliar(1000 * S)
    assert r.deals_manuais == 1 and r.bloqueio and rec.bloqueado and not ks.ativo
    assert rec.reconciliar(1010 * S).bloqueio  # continua bloqueado ate reconhecimento humano
    with pytest.raises(ValueError):
        rec.reconhecer_intervencao_manual("", "x", 1)
    rec.reconhecer_intervencao_manual("ana", "fechei posicao manualmente", 1020 * S)
    assert not rec.reconciliar(1030 * S).bloqueio
    assert any(t == "reconciliacao_manual_reconhecida" for t, _ in aud.l)


def test_ordem_no_terminal_desconhecida_e_ordem_interna_ausente():
    rel, oms, term, _, _, rec, _ = montar({})
    term.ordens = [ordem_t(777)]
    assert "ordem_desconhecida_no_terminal" in {d.tipo for d in rec.reconciliar(1000 * S).divergencias}
    term.ordens = []
    o = oms.enviar(ComandoOrdem(intencao_id="a", magic=11, lado=Lado.COMPRA, tipo="limitada", preco=1.0),
                   type("E", (), {"enviar": lambda self, c: AckOrdem(retcode=10008, order=5)})()).ordem
    assert o.estado == EstadoOrdem.ACEITA
    assert "ordem_ausente_no_terminal" in {d.tipo for d in rec.reconciliar(1001 * S).divergencias}
    term.deals = [deal(9, order=5)]  # mas ela executou: explicado pelo deal
    assert "ordem_ausente_no_terminal" not in {d.tipo for d in rec.reconciliar(1002 * S).divergencias}


def _incerta(oms, iid="a"):
    ex = type("E", (), {"enviar": lambda self, c: AckOrdem(retcode=None)})()
    return oms.enviar(ComandoOrdem(intencao_id=iid, magic=11, lado=Lado.COMPRA, tipo="mercado"), ex).ordem


def test_incerta_resolvida_por_ordem_no_terminal_sem_reenvio():
    rel, oms, term, _, ks, rec, _ = montar({})
    o = _incerta(oms)
    t_ms = o.t_envio_ns // 1_000_000
    term.ordens = [ordem_t(42, time_setup_msc=t_ms + 5)]
    r = rec.reconciliar(1001 * S)
    assert o.estado == EstadoOrdem.ACEITA and o.ticket == 42 and r.incertas_resolvidas == 1
    assert r.incertas_pendentes == 0 and not ks.ativo


def test_incerta_resolvida_por_deal_no_terminal():
    rel, oms, term, interna, ks, rec, _ = montar({})
    o = _incerta(oms)
    t_ms = o.t_envio_ns // 1_000_000
    term.deals = [deal(1, order=88, time_msc=t_ms + 3, entry=0, price=130005.0)]
    term.pos = [posicao()]
    r = rec.reconciliar(1001 * S)
    assert o.estado == EstadoOrdem.EXECUTADA and o.preco_medio_exec == 130005.0
    assert not ks.ativo  # divergencia de posicao explicada pela INCERTA: bloqueia mas nao aciona kill
    assert r.bloqueio


def test_incerta_ausente_so_cancelada_apos_confirmacoes_e_espera():
    rel, oms, term, _, _, rec, _ = montar({}, confirmacoes_ausencia=2, espera_incerta_s=10)
    o = _incerta(oms)
    r1 = rec.reconciliar(1001 * S)
    assert o.estado == EstadoOrdem.INCERTA and r1.incertas_pendentes == 1 and r1.bloqueio
    r2 = rec.reconciliar(1005 * S)  # segunda confirmacao, mas antes da espera de 10 s
    assert o.estado == EstadoOrdem.INCERTA and r2.bloqueio
    r3 = rec.reconciliar(1012 * S)
    assert o.estado == EstadoOrdem.CANCELADA and r3.incertas_resolvidas == 1 and not r3.bloqueio
