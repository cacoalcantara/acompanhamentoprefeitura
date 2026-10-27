import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import AckOrdem, ComandoOrdem, EstadoOrdem, Execucao, Intencao, Lado
from robowin.execucao.oms import OMS, TRANSICOES, ErroTransicao

S = int(1e9)
E = EstadoOrdem


class Exec:
    def __init__(self, *acks):
        self.acks, self.chamadas = list(acks), []

    def enviar(self, cmd):
        self.chamadas.append(cmd)
        a = self.acks.pop(0) if len(self.acks) > 1 else self.acks[0]
        if isinstance(a, Exception):
            raise a
        return a


class Banco:
    def __init__(self):
        self.linhas = []

    def inserir(self, tabela, linha):
        self.linhas.append((tabela, dict(linha)))

    def consultar(self, sql, params=()):
        return []


def cmd(iid="i1", lado=Lado.COMPRA, tipo="mercado", q=1, preco=None, magic=11):
    return ComandoOrdem(intencao_id=iid, magic=magic, lado=lado, tipo=tipo, quantidade=q, preco=preco)


def ack(rc, order=0, preco=0.0):
    return AckOrdem(retcode=rc, order=order, preco=preco)


@pytest.fixture
def rel():
    return RelogioSimulado(1000 * S)


@pytest.fixture
def oms(rel):
    return OMS(rel, {"b1": 11, "b2": 22}, janela_duplicada_s=2.0)


def test_intencao_gravada_antes_do_envio(rel):
    banco = Banco()
    oms = OMS(rel, {"b1": 11}, banco=banco)
    visto = {}

    class Ex:
        def enviar(self, c):
            visto["linhas"] = list(banco.linhas)
            return ack(10008, 5)
    oms.enviar(cmd(), Ex())
    assert len(visto["linhas"]) == 1 and visto["linhas"][0][1]["estado"] == "NOVA"
    assert visto["linhas"][0][0] == "ordens" and visto["linhas"][0][1]["intencao_id"] == "i1"


@pytest.mark.parametrize("rc", [None, 10012, 10031])
def test_incerta_bloqueia_e_nunca_reenvia(oms, rel, rc):
    ex = Exec(ack(rc))
    r = oms.enviar(cmd("i1"), ex)
    assert r.ordem.estado == E.INCERTA and r.enviado
    # mesma intencao: idempotente, sem novo envio
    r2 = oms.enviar(cmd("i1"), ex)
    assert r2.motivo == "idempotente" and r2.ordem.id == r.ordem.id and len(ex.chamadas) == 1
    # nova intencao do mesmo braco: bloqueada enquanto houver INCERTA
    rel.avancar(10 * S)
    r3 = oms.enviar(cmd("i2", lado=Lado.VENDA), ex)
    assert not r3.enviado and r3.motivo == "ordem_incerta_pendente" and len(ex.chamadas) == 1
    # outro braco nao e bloqueado
    r4 = oms.enviar(cmd("i3", magic=22), Exec(ack(10008, 9)))
    assert r4.enviado and r4.ordem.estado == E.ACEITA
    assert oms.pendentes_entrada("b1") == 1  # INCERTA conta como exposicao pendente


def test_ack_none_e_excecao_viram_incerta(oms):
    class ExNone:
        def enviar(self, c):
            return None
    assert oms.enviar(cmd("a"), ExNone()).ordem.estado == E.INCERTA
    oms2 = OMS(RelogioSimulado(5 * S), {"b1": 11})
    assert oms2.enviar(cmd("a"), Exec(TimeoutError())).ordem.estado == E.INCERTA


def test_10008_e_colocada_nao_executada(oms):
    r = oms.enviar(cmd(), Exec(ack(10008, 77)))
    assert r.ordem.estado == E.ACEITA and r.ordem.qtd_exec == 0 and oms.pendentes_entrada("b1") == 1
    assert oms.por_ticket(77).id == r.ordem.id  # mapa proprio ordem<->ticket


def test_10009_executada(oms):
    r = oms.enviar(cmd(), Exec(ack(10009, 3, 130000.0)))
    assert r.ordem.estado == E.EXECUTADA and r.ordem.preco_medio_exec == 130000.0
    assert oms.pendentes_entrada("b1") == 0


def test_10010_parcial_e_execucoes_completam(oms):
    r = oms.enviar(cmd(q=3, tipo="limitada", preco=130000.0), Exec(ack(10010, 8)))
    assert r.ordem.estado == E.PARCIAL
    oms.ao_execucao(8, Execucao(ordem_id="8", preco=130000.0, quantidade=1, t_ns=1))
    assert r.ordem.estado == E.PARCIAL and r.ordem.restante == 2 and oms.pendentes_entrada("b1") == 2
    oms.ao_execucao(8, Execucao(ordem_id="8", preco=130005.0, quantidade=2, t_ns=2))
    assert r.ordem.estado == E.EXECUTADA and r.ordem.preco_medio_exec == pytest.approx(130003.333, abs=1e-2)


def test_aceita_depois_executada_cancelada_expirada(oms, rel):
    a = oms.enviar(cmd("a", tipo="limitada", preco=1.0), Exec(ack(10008, 1))).ordem
    oms.ao_execucao(1, Execucao(ordem_id="1", preco=1.0, quantidade=1, t_ns=1))
    assert a.estado == E.EXECUTADA
    rel.avancar(5 * S)
    b = oms.enviar(cmd("b", tipo="limitada", preco=2.0), Exec(ack(10008, 2))).ordem
    oms.ao_cancelada(2)
    assert b.estado == E.CANCELADA
    rel.avancar(5 * S)
    c = oms.enviar(cmd("c", tipo="limitada", preco=3.0), Exec(ack(10008, 3))).ordem
    oms.ao_expirada(3)
    assert c.estado == E.EXPIRADA
    oms.ao_execucao(3, Execucao(ordem_id="3", preco=3.0, quantidade=1, t_ns=1))  # terminal e imutavel
    assert c.estado == E.EXPIRADA


def test_10024_recua_sem_loop_e_dobra(oms, rel):
    ex = Exec(ack(10024))
    r = oms.enviar(cmd("a"), ex)
    assert r.ordem.estado == E.REJEITADA and "10024" in r.ordem.motivo and len(ex.chamadas) == 1
    r2 = oms.enviar(cmd("b", lado=Lado.VENDA), ex)  # dentro do recuo de 1 s
    assert not r2.enviado and r2.motivo == "recuo_ativo" and len(ex.chamadas) == 1
    rel.avancar(int(1.5 * S))
    oms.enviar(cmd("c", q=2), ex)  # recuo acabou; recebe 10024 de novo -> 2 s
    assert oms.bloqueado_ate_ns - rel.agora_ns() == 2 * S
    rel.avancar(int(2.5 * S))
    oms.enviar(cmd("d", lado=Lado.VENDA), ex)
    assert oms.bloqueado_ate_ns - rel.agora_ns() == 4 * S
    rel.avancar(5 * S)
    assert oms.enviar(cmd("e"), Exec(ack(10008, 4))).ordem.estado == E.ACEITA
    assert oms._recuo_atual_s == 0  # sucesso zera o recuo


def test_10027_algotrading_desligado_bloqueia_com_alerta(oms, rel):
    r = oms.enviar(cmd("a"), Exec(ack(10027)))
    assert r.ordem.estado == E.REJEITADA and oms.algotrading_desligado and oms.alertas
    rel.avancar(60 * S)
    ex = Exec(ack(10008, 1))
    assert oms.enviar(cmd("b"), ex).motivo == "algotrading_desligado" and not ex.chamadas
    oms.limpar_algotrading()
    assert oms.enviar(cmd("c"), ex).ordem.estado == E.ACEITA


def test_outros_retcodes_rejeitam_sem_reenvio(oms):
    ex = Exec(ack(10006))
    assert oms.enviar(cmd("a"), ex).ordem.estado == E.REJEITADA and len(ex.chamadas) == 1


def test_duplicadas_na_janela(oms, rel):
    ex = Exec(ack(10008, 1), ack(10008, 2), ack(10008, 3))
    assert oms.enviar(cmd("a"), ex).enviado
    rel.avancar(1 * S)
    r = oms.enviar(cmd("b"), ex)  # outra intencao, mesmo braco/lado/tipo/qtd dentro da janela
    assert not r.enviado and r.motivo == "ordem_duplicada_na_janela"
    rel.avancar(3 * S)
    assert oms.enviar(cmd("c"), ex).enviado


def test_saida_nao_duplica_enquanto_outra_em_andamento(oms, rel):
    ex = Exec(ack(10008, 1))
    assert oms.enviar(cmd("s1", lado=Lado.VENDA, tipo="saida"), ex).enviado
    rel.avancar(5 * S)
    r = oms.enviar(cmd("s2", lado=Lado.VENDA, tipo="saida"), ex)
    assert r.motivo == "saida_ja_em_andamento"
    assert oms.pendentes_entrada("b1") == 0  # saida nao e exposicao de entrada


def test_transicoes_invalidas(oms):
    o = oms.enviar(cmd(), Exec(ack(10009, 1, 1.0))).ordem
    with pytest.raises(ErroTransicao):
        oms._transitar(o, E.ACEITA)
    assert all(not TRANSICOES[e] for e in (E.EXECUTADA, E.CANCELADA, E.REJEITADA, E.EXPIRADA))
    assert E.INCERTA not in TRANSICOES[E.NOVA]


def test_resolver_incerta_nunca_reenvia(oms):
    ex = Exec(ack(None))
    o = oms.enviar(cmd("a"), ex).ordem
    oms.resolver_incerta(o.id, "aceita", ticket=55)
    assert o.estado == E.ACEITA and oms.por_ticket(55) is o
    o2 = OMS(RelogioSimulado(5 * S), {"b1": 11})
    x = o2.enviar(cmd("a"), Exec(ack(None))).ordem
    o2.resolver_incerta(x.id, "inexistente")
    assert x.estado == E.CANCELADA and len(ex.chamadas) == 1
    o3 = OMS(RelogioSimulado(5 * S), {"b1": 11})
    y = o3.enviar(cmd("a"), Exec(ack(None))).ordem
    o3.resolver_incerta(y.id, "executada", quantidade=1, preco=100.0)
    assert y.estado == E.EXECUTADA and y.preco_medio_exec == 100.0


def test_cancelar_pedido_unico(oms):
    o = oms.enviar(cmd(tipo="limitada", preco=1.0), Exec(ack(10008, 5))).ordem
    chamadas = []

    def cancelador(tk):
        chamadas.append(tk)
        return ack(10009, tk)
    assert oms.cancelar(o.id, cancelador) and o.estado == E.CANCELADA and chamadas == [5]
    assert not oms.cancelar(o.id, cancelador)


def test_intencao_registrada_na_auditoria(rel):
    class Aud:
        eventos = []

        def registrar(self, tipo, dados):
            self.eventos.append(tipo)
            return "h"
    a = Aud()
    oms = OMS(rel, {"b1": 11}, auditoria=a)
    oms.registrar_intencao(Intencao(id="i1", braco="b1", acao="COMPRAR", validade_ns=1))
    oms.enviar(cmd(), Exec(ack(10008, 1)))
    assert a.eventos[0] == "oms_intencao" and a.eventos.count("oms_estado") >= 3


@settings(max_examples=150, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 4), st.sampled_from([None, 10008, 10009, 10010, 10012, 10024, 10027, 10031, 10006]),
                          st.integers(0, 4000)), max_size=30))
def test_propriedade_nunca_reenvia_e_so_transicoes_validas(passos):
    rel = RelogioSimulado(1000 * S)
    oms = OMS(rel, {"b1": 11})
    enviados = 0
    rc = None

    class Ex:
        def enviar(self, c):
            nonlocal enviados
            enviados += 1
            return ack(rc, enviados)
    for i, rc_, dt_ms in passos:
        rc = rc_
        rel.avancar(dt_ms * 1_000_000)
        antes = enviados
        r = oms.enviar(cmd(f"i{i}"), Ex())
        if r.motivo == "idempotente" or not r.enviado:
            assert enviados == antes  # bloqueada/idempotente: nenhuma chamada ao executor
    intencoes = {o.intencao_id for o in oms._ordens.values()}
    assert enviados <= len(intencoes)  # no maximo UM envio por intencao
    for o in oms._ordens.values():
        for (_, a, _m1), (_, b, _m2) in zip(o.historico, o.historico[1:], strict=False):
            assert EstadoOrdem(b) in TRANSICOES[EstadoOrdem(a)]
