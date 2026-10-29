"""Orquestrador com o braco planejador (docs/16 WP7): gestao por tick, parciais como UM episodio, ancoragem em t0,
MANTER/ENCERRAR presos ao ticket de t0, dossie por braco posicionado (C6), C19, vetos deduplicados, plano invalidado
ao armar, sombras no mesmo tick, stop atual no dossie (C4), tempo nunca 0 e ponta a ponta com o provedor FALSO."""
from __future__ import annotations

import contextlib
import io
import json
import math
import sqlite3
from typing import Any

import pytest
from test_decisao_comum import (
    INS,
    NS,
    T0,
    BracoRoteirizado,
    alimentar,
    montar_orq,
    serie,
    tk,
)

from robowin.core.tipos import (
    Acao,
    AjustePosicao,
    Condicao,
    ConfigPlanejador,
    ConfigRisco,
    PlanoCondicional,
    PlanoGestaoCompilado,
    RegraCompilada,
)
from robowin.decisao.bracos.base import ContextoDecisao, Geometria, ResultadoDecisao
from robowin.decisao.bracos.planejador import BracoPlanejador
from robowin.decisao.bracos.sombra import BracoSombra
from robowin.decisao.orquestrador import FEATURES_DOSSIE, FEATURES_DOSSIE_V2
from robowin.decisao.plano_condicional import converter_plano
from robowin.risco.motor import EstadoMercado

Passo = Any


def _db(banco, sql: str, *a: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in banco.consultar(sql, tuple(a))]


def _gestao(*regras: RegraCompilada, intervalo_s: float = 0.0, reavaliar_se: tuple[str, ...] = ()) -> PlanoGestaoCompilado:
    return PlanoGestaoCompilado(regras=regras, passo_min_pontos=INS.tick_pontos, intervalo_ajuste_s=intervalo_s,
                                reavaliar_se=reavaliar_se)


def _entrada(ctx: ContextoDecisao, braco: Any, *, stop: float = 50.0, alvo: float = 100.0, tempo: int | None = 120,
             gestao: PlanoGestaoCompilado | None = None, q: int = 1, acao: Acao = Acao.COMPRAR,
             plano: PlanoCondicional | None = None, ancora: bool = False, latencia_ns: int = 0) -> ResultadoDecisao:
    extras: dict[str, Any] = {}
    if gestao is not None:
        extras["gestao"] = gestao
    if ancora:
        extras["ancora_t0"] = ctx.preco_ref
    it = braco._intencao(ctx, acao, stop=stop, alvo=alvo, tempo_max_s=tempo,
                         validade_ns=plano.expira_ns if plano else ctx.t0_ns + 60 * NS, plano=plano, quantidade=q,
                         **extras)
    return braco._resultado(ctx, acao, "plano" if plano else "entrada", intencao=it, validade_s=60.0,
                            desvio_max_pts=50.0, plano=plano, latencia_ns=latencia_ns)


def _nada(braco: Any, ctx: ContextoDecisao) -> ResultadoDecisao:
    return braco._resultado(ctx, Acao.NAO_OPERAR, "nao_operar")


class ServicoFake:
    """Servico de planejamento falso: so o que o BracoPlanejador usa na construcao."""
    chave = "fake"
    ref = "falso:planejador"
    effort = None
    cfg = ConfigPlanejador(ativo=True)

    def registrar_braco(self, *_a: Any) -> None:
        return None

    def dono_de(self, _modo: str) -> str:
        return "planejador"


class PlanejadorRoteirizado(BracoPlanejador):
    """BracoPlanejador com respostas roteirizadas (sem IA): exercita o orquestrador como o braco real."""

    def __init__(self, id: str, passos: list[Passo]) -> None:
        super().__init__(id, ServicoFake())  # type: ignore[arg-type]
        self.passos = passos
        self.i = 0
        self.contextos: list[ContextoDecisao] = []

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        self.contextos.append(ctx)
        fn = self.passos[min(self.i, len(self.passos) - 1)]
        self.i += 1
        return fn(self, ctx)


class EspelhoFalso:
    """Registra as chamadas do orquestrador ao espelho (sem gateway)."""

    def __init__(self, braco: str) -> None:
        self.cfg = type("C", (), {"braco": braco})()
        self.chamadas: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, nome: str) -> Any:
        if nome.startswith("_"):
            raise AttributeError(nome)
        return lambda *a: self.chamadas.append((nome, a))


# ---------------------------------------------------------------------------------------------------------
# tempo
# ---------------------------------------------------------------------------------------------------------
async def test_tempo_da_ia_fecha_por_saidas_e_acima_do_teto_e_vetado():
    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, tempo=40), _nada])
    orq, banco, _ = montar_orq([b], risco=ConfigRisco(tempo_max_posicao_s=60))
    await alimentar(orq, serie([130000.0] * 60))
    tr = _db(banco, "SELECT * FROM trades WHERE braco='a'")
    assert len(tr) == 1 and tr[0]["motivo_saida"] == "tempo"
    dur = (tr[0]["t_saida_ns"] - tr[0]["t_entrada_ns"]) / NS
    assert 40 <= dur <= 42  # o tempo escolhido pela IA fecha por `_saidas`
    ep = _db(banco, "SELECT * FROM posicoes_episodio WHERE braco='a'")
    assert len(ep) == 1 and ep[0]["motivo_final"] == "tempo" and ep[0]["tempo_max_s"] == 40
    # acima do teto do usuario: o motor veta a entrada (o teto nunca e furado)
    b2 = BracoRoteirizado("b", [lambda s, c: _entrada(c, s, tempo=200), _nada])
    orq2, banco2, _ = montar_orq([b2], risco=ConfigRisco(tempo_max_posicao_s=60))
    await alimentar(orq2, serie([130000.0] * 3))
    dec = _db(banco2, "SELECT resultado, motivos FROM decisoes WHERE braco='b' AND acao='COMPRAR'")
    assert dec[0]["resultado"] == "vetada" and "tempo_max_acima_do_limite" in dec[0]["motivos"]


def test_teto_do_usuario_limita_o_tempo_da_meta():
    b = BracoRoteirizado("a", [_nada])
    orq, _, _ = montar_orq([b], risco=ConfigRisco(tempo_max_posicao_s=60))
    conta = orq.contas["a"]
    it = b._intencao(type("C", (), {"ciclo_id": "c", "preco_ref": 1.0, "t0_ns": T0})(), Acao.COMPRAR, stop=10.0,
                     alvo=10.0, tempo_max_s=500, validade_ns=T0)
    assert orq._tempo_da_posicao(conta, it) == 60
    from robowin.decisao.orquestrador import MetaPosicao
    conta.meta = MetaPosicao(intencao=it, aberta_ns=T0, tempo_max_s=500)
    assert orq._teto_tempo_s(conta) == 60


async def test_sem_tempo_zero_com_a_geometria_sentinela():
    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, tempo=None), _nada])
    b.geometria = Geometria(0.0, 0.0, 0)
    orq, _, _ = montar_orq([b], risco=ConfigRisco(tempo_max_posicao_s=45))
    await alimentar(orq, serie([130000.0] * 5))
    meta = orq.contas["a"].meta
    assert meta is not None and meta.tempo_max_s == 45  # nunca 0: sem tempo pedido, vale o teto do usuario
    await alimentar(orq, serie([130000.0] * 50, inicio_ms=5000))
    assert orq.sim.posicao("a").quantidade == 0


# ---------------------------------------------------------------------------------------------------------
# gestao automatica
# ---------------------------------------------------------------------------------------------------------
async def test_regra_aperta_stop_no_simulador_e_espelho_recebe_protecao():
    g = _gestao(RegraCompilada(tipo="breakeven", mfe_pontos=20.0, folga_pontos=0.0))
    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, stop=50.0, alvo=200.0, gestao=g), _nada])
    orq, banco, _ = montar_orq([b])
    esp = EspelhoFalso("a")
    orq.espelho = esp
    await alimentar(orq, serie([130000.0] * 3))
    pos = orq.sim.posicao("a")
    sl0 = pos.stop
    assert pos.quantidade == 1 and sl0 is not None
    await alimentar(orq, serie([130040.0] * 3, inicio_ms=3000))  # MFE >= 20 => breakeven
    pos2 = orq.sim.posicao("a")
    assert pos2.stop is not None and pos2.stop > sl0  # o SL do simulador apertou
    ev = _db(banco, "SELECT * FROM gestao_eventos WHERE braco='a' AND origem='auto' AND aplicado=1")
    assert ev and ev[0]["regra"] == "breakeven" and ev[0]["sl_depois"] == pos2.stop and ev[0]["sl_antes"] == sl0
    assert any(n == "espelhar_protecao" and a[0] == pos2.stop for n, a in esp.chamadas)
    assert orq.contas["a"].meta.estado.origem_ultimo_stop == "auto"
    await alimentar(orq, serie([130000.0] * 3, inicio_ms=6000))  # volta: stop movido executa
    ep = _db(banco, "SELECT * FROM posicoes_episodio WHERE braco='a'")
    assert ep[0]["motivo_final"] == "stop_movido_auto" and ep[0]["n_ajustes_auto"] >= 1
    assert orq.resumo()["a"]["gestoes_auto"] >= 1


async def test_regra_sem_progresso_encerra_pelo_gestor_de_saidas():
    g = _gestao(RegraCompilada(tipo="sem_progresso", apos_s=5, mfe_pontos=50.0))
    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, gestao=g), _nada])
    orq, banco, _ = montar_orq([b])
    await alimentar(orq, serie([130000.0] * 12))
    ep = _db(banco, "SELECT * FROM posicoes_episodio WHERE braco='a'")
    assert len(ep) == 1 and ep[0]["motivo_final"] == "gestao:sem_progresso"


def _orq_forex(bracos: list[Any]) -> tuple[Any, Any, int, Any]:
    """Orquestrador num perfil FOREX (EURUSD, conta em USD, `volume_max_lotes=0.05` => 5 unidades de 0.01 lote)."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    from robowin.core.instrumentos import carregar_perfil
    from robowin.core.relogio import RelogioSimulado
    from robowin.dados.banco import BancoSQLite
    from robowin.decisao.gatilhos import ConfigGatilhos
    from robowin.decisao.orquestrador import ConfigOrquestrador, Orquestrador
    from robowin.decisao.registro import Registro
    ins = carregar_perfil("EURUSD", permitir_a_confirmar=True).instrumento("EURUSD", moeda_conta="USD")
    t0 = int(dt.datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)
    risco = ConfigRisco(volume_max_lotes=0.05, stop_max_pontos=0.0050, slippage_folga_pontos=0.0002,
                        perda_max_operacao_brl=60.0, entradas_de="09:15", entradas_ate="11:30", zeragem="12:00")
    banco = BancoSQLite(":memory:")
    cfg = ConfigOrquestrador(
        instrumento=ins, risco=risco, sessao_id="fx", passo_s=1.0, checagem_s=1.0, exigir_aquecimento=False,
        latencia_exec_ns=0, gatilhos=ConfigGatilhos(periodo_s=1.0, intervalo_min_s=1.0, e_brl_min=1e18,
                                                    z_statarb_min=1e18, evento_posicao=False, max_chamadas_dia=None))
    orq = Orquestrador(cfg, relogio=RelogioSimulado(t0), registro=Registro(banco, None, "fx"), bracos=bracos)
    return orq, banco, t0, ins


async def test_parcial_em_perfil_forex_conta_um_episodio():
    from robowin.core.tipos import Tick
    tp = 0.00001
    g = PlanoGestaoCompilado(regras=(RegraCompilada(tipo="parcial", mfe_pontos=20 * tp, quantidade=1),),
                             passo_min_pontos=tp, intervalo_ajuste_s=0.0)
    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, stop=30 * tp, alvo=300 * tp, gestao=g, q=2), _nada])
    orq, banco, t0, _ = _orq_forex([b])

    def f(i: int, bid: float) -> Tick:
        t = t0 + i * NS
        return Tick(t_servidor_ms=t // 1_000_000, t_utc_ns=t, t_recebido_ns=t, bid=bid, ask=bid + 2 * tp, last=0.0,
                    volume=0.0, flags=6)

    base = 1.08
    await alimentar(orq, [f(i, base) for i in range(3)])
    assert orq.sim.posicao("a").quantidade == 2
    await alimentar(orq, [f(i, base + 25 * tp) for i in range(3, 6)])  # MFE >= 20 pips/10 => parcial de 1
    assert orq.sim.posicao("a").quantidade == 1 and orq.contas["a"].meta is not None
    await alimentar(orq, [f(i, base - 100 * tp) for i in range(6, 9)])  # stop no restante
    assert orq.sim.posicao("a").quantidade == 0
    ep = _db(banco, "SELECT * FROM posicoes_episodio WHERE braco='a'")
    assert len(ep) == 1 and ep[0]["n_parciais"] == 1 and ep[0]["q_inicial"] == 2
    trades = _db(banco, "SELECT * FROM trades WHERE braco='a'")
    assert len(trades) == 2 and len(json.loads(ep[0]["trades_json"])) == 2
    assert ep[0]["liquido_total"] == pytest.approx(sum(t["liquido_brl"] for t in trades))
    est = orq.contas["a"].motor.contab.estado("a", orq.t_ultimo_ns)
    assert est.perdas_seguidas == (1 if ep[0]["liquido_total"] < 0 else 0)  # UM episodio, nao dois trades
    assert est.operacoes_dia == 1
    assert orq.resumo()["a"]["parciais"] == 1
    ges = _db(banco, "SELECT * FROM gestao_eventos WHERE braco='a' AND acao='reduzir'")
    assert ges and ges[0]["aplicado"] == 1 and ges[0]["regra"] == "parcial"


# ---------------------------------------------------------------------------------------------------------
# entrega presa a posicao de t0
# ---------------------------------------------------------------------------------------------------------
def _forjar_t0(res: ResultadoDecisao, ticket: int | None, q: int) -> ResultadoDecisao:
    res.posicao_ticket_t0, res.quantidade_t0 = ticket, q
    return res


async def test_manter_ou_encerrar_de_ticket_velho_vira_ignorada():
    def encerrar_velho(s, c):
        it = s._intencao(c, Acao.ENCERRAR, stop=None, alvo=None, tempo_max_s=None, validade_ns=c.t0_ns + 60 * NS,
                         posicao_ticket=999)
        return _forjar_t0(s._resultado(c, Acao.ENCERRAR, "saida", intencao=it), 999, 1)

    def ajuste_velho(s, c):
        it = s._intencao(c, Acao.MANTER, stop=None, alvo=None, tempo_max_s=None, validade_ns=c.t0_ns + 60 * NS,
                         ajuste=AjustePosicao(stop_resultado_pontos=0.0), posicao_ticket=999, quantidade_t0=1)
        return _forjar_t0(s._resultado(c, Acao.MANTER, "ajuste", intencao=it), 999, 1)

    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, stop=100.0, alvo=300.0, tempo=300), encerrar_velho,
                               ajuste_velho, _nada])
    orq, banco, _ = montar_orq([b])
    await alimentar(orq, serie([130000.0] * 4))
    dec = _db(banco, "SELECT acao, resultado, motivos FROM decisoes WHERE braco='a' AND acao IN ('ENCERRAR','MANTER')")
    assert len(dec) == 2 and all(d["resultado"] == "ignorada" and "posicao_mudou_desde_t0" in d["motivos"] for d in dec)
    assert orq.sim.posicao("a").quantidade == 1  # nada aconteceu com a posicao atual


async def test_ajuste_da_ia_aplicado_vira_executada_e_gestao_ia():
    def travar(s, c):
        it = s._intencao(c, Acao.MANTER, stop=None, alvo=None, tempo_max_s=None, validade_ns=c.t0_ns + 60 * NS,
                         ajuste=AjustePosicao(stop_resultado_pontos=0.0), posicao_ticket=c.posicao.ticket,
                         quantidade_t0=c.posicao.quantidade)
        return s._resultado(c, Acao.MANTER, "ajuste", intencao=it)

    precos = [130000.0] * 2 + [130050.0] * 4
    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, stop=50.0, alvo=300.0, tempo=300), _nada, travar, _nada],
                         ia=True)
    orq, banco, _ = montar_orq([b])
    await alimentar(orq, serie(precos))
    dec = _db(banco, "SELECT resultado FROM decisoes WHERE braco='a' AND acao='MANTER'")
    assert dec and dec[0]["resultado"] == "executada"
    pos = orq.sim.posicao("a")
    assert pos.stop == pytest.approx(pos.preco_medio)
    ev = _db(banco, "SELECT * FROM gestao_eventos WHERE braco='a' AND origem='ia'")
    assert ev and ev[0]["aplicado"] == 1 and ev[0]["sl_depois"] == pos.stop


async def test_ajuste_que_afrouxa_e_vetado_e_nunca_executado():
    def afrouxar(s, c):
        it = s._intencao(c, Acao.MANTER, stop=None, alvo=None, tempo_max_s=None, validade_ns=c.t0_ns + 60 * NS,
                         ajuste=AjustePosicao(stop_resultado_pontos=-90.0), posicao_ticket=c.posicao.ticket,
                         quantidade_t0=c.posicao.quantidade)
        return s._resultado(c, Acao.MANTER, "ajuste", intencao=it)

    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, stop=50.0, alvo=300.0, tempo=300), afrouxar, _nada],
                         ia=True)
    orq, banco, _ = montar_orq([b])
    await alimentar(orq, serie([130000.0] * 4))
    dec = _db(banco, "SELECT resultado, motivos FROM decisoes WHERE braco='a' AND acao='MANTER'")
    assert dec[0]["resultado"] == "vetada" and "stop_so_pode_apertar" in dec[0]["motivos"]
    assert orq.resumo()["a"]["gestoes_vetadas"] == 1


# ---------------------------------------------------------------------------------------------------------
# ciclos
# ---------------------------------------------------------------------------------------------------------
class Agrupado(BracoRoteirizado):
    @property
    def grupo_ciclo(self) -> str:
        return "g"


async def test_c_posicionado_e_b_zerado_fazem_chamadas_separadas():
    c_br = Agrupado("c", [lambda s, c: _entrada(c, s, stop=100.0, alvo=300.0, tempo=300), _nada], ia=True)
    b_br = Agrupado("b", [_nada], ia=True)
    orq, _, _ = montar_orq([b_br, c_br])
    await alimentar(orq, serie([130000.0] * 4))
    assert orq.sim.posicao("c").quantidade == 1
    ult_b, ult_c = b_br.contextos[-1], c_br.contextos[-1]
    assert ult_b.t0_ns == ult_c.t0_ns
    assert ult_c.dossie["posicao"]["quantidade"] == 1 and ult_b.dossie["posicao"]["quantidade"] == 0
    assert ult_b.hash_dossie != ult_c.hash_dossie  # dossie proprio do braco posicionado (C6)


async def test_zero_chamadas_de_ia_sem_entrada_possivel():
    b = BracoRoteirizado("a", [_nada], ia=True)
    q = BracoRoteirizado("ctl", [_nada])
    q.familia = "controle"
    orq, _, _ = montar_orq([b, q])
    for c in orq.contas.values():
        c.motor.atualizar_mercado(EstadoMercado(leilao=True))
    await alimentar(orq, serie([130000.0] * 6))
    assert b.contextos == []  # o motor vetaria qualquer entrada: a IA nao e chamada (C19)
    assert q.contextos  # controles mantem o fluxo de sorteios


async def test_veto_repetido_conta_um_episodio():
    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, stop=2000.0)])
    orq, _, _ = montar_orq([b])
    await alimentar(orq, serie([130000.0] * 6))
    r = orq.resumo()["a"]
    assert r["vetadas"] >= 3 and r["tentativas_veto"] == r["vetadas"] and r["episodios_veto"] == 1


async def test_plano_invalidado_ao_armar():
    def plano(s, c):
        rel = PlanoCondicional(gatilhos=(Condicao(tipo="preco_toca", valor=-100.0),),
                               invalidacoes=(Condicao(tipo="preco_rompe_acima", valor=20.0),), expira_ns=120 * NS)
        return _entrada(c, s, plano=converter_plano(rel, c.t0_ns), latencia_ns=2 * NS)
    b = BracoRoteirizado("a", [plano, _nada], modo="condicional")
    orq, banco, _ = montar_orq([b])
    await alimentar(orq, serie([130000.0] + [130100.0] * 4))  # na chegada, ja rompeu +20
    dec = _db(banco, "SELECT resultado, motivos FROM decisoes WHERE braco='a' AND acao='COMPRAR'")
    assert dec[0]["resultado"] == "bloqueada" and "invalidado_ao_armar" in dec[0]["motivos"]
    assert orq.contas["a"].planos.armado is None
    assert [e.tipo for e in orq.contas["a"].planos.historico] == ["invalidado_ao_armar"]


# ---------------------------------------------------------------------------------------------------------
# planejador: ancoragem, sombras, previsoes, dossie
# ---------------------------------------------------------------------------------------------------------
def _plano_agora(s, c, *, favor: float = 10.0, contra: float = 10.0, stop: float = 50.0, alvo: float = 100.0,
                 g: PlanoGestaoCompilado | None = None) -> ResultadoDecisao:
    rel = PlanoCondicional(gatilhos=(Condicao(tipo="janela_tempo", valor=120.0),),
                           invalidacoes=(Condicao(tipo="preco_rompe_acima", valor=favor),
                                         Condicao(tipo="preco_rompe_abaixo", valor=-contra)), expira_ns=120 * NS)
    res = _entrada(c, s, stop=stop, alvo=alvo, plano=converter_plano(rel, c.t0_ns), ancora=True, gestao=g)
    res.proposta = {"acao": "COMPRAR", "p_alvo": 0.4, "incerteza": 0.5, "hipoteses": ["h"],
                    "justificativa_curta": "ok"}
    return res


async def test_planejador_ancora_em_t0_sombras_replicam_no_mesmo_tick_e_previsao():
    p = PlanejadorRoteirizado("planejador", [_plano_agora, _nada])
    s1 = BracoSombra("sombra_sem_gestao", "planejador", "sem_gestao")
    s2 = BracoSombra("sombra_trailing_fixo", "planejador", "trailing_fixo")
    orq, banco, _ = montar_orq([p, s1, s2])
    orq.contas["sombra_trailing_fixo"].kill.acionar("teste", "manual_cli", T0)  # a sombra sofre o proprio veto
    await alimentar(orq, [tk(0, 130000.0), tk(1000, 130000.0), tk(2000, 130005.0), tk(3000, 130005.0)])
    pos = orq.sim.posicao("planejador")
    assert pos.quantidade == 1
    # ancorado: nivel absoluto do stop = referencia de t0 - 50 (nao anda junto com o preco)
    ref_t0 = p.contextos[0].preco_ref
    assert pos.stop == pytest.approx(math.ceil((ref_t0 - 50.0) / INS.tick_pontos) * INS.tick_pontos)
    ent = _db(banco, "SELECT braco, t_ns, resultado, gatilho FROM decisoes WHERE gatilho IN ('plano_disparado','sombra')")
    por = {d["braco"]: d for d in ent}
    assert por["planejador"]["resultado"] == "executada"
    assert por["sombra_sem_gestao"]["resultado"] == "executada"
    assert por["sombra_sem_gestao"]["t_ns"] == por["planejador"]["t_ns"]  # mesmo tick
    assert por["sombra_trailing_fixo"]["resultado"] != "executada"
    assert orq.sim.posicao("sombra_sem_gestao").stop == pos.stop  # niveis ja ancorados copiados
    assert orq.resumo()["sombra_trailing_fixo"]["pares_incompletos_sombra"] == 1
    assert not _db(banco, "SELECT * FROM decisoes WHERE braco LIKE 'sombra%' AND gatilho NOT IN ('sombra')")
    prv = _db(banco, "SELECT * FROM previsoes_ia WHERE braco='planejador'")
    assert len(prv) == 1 and prv[0]["executada"] == 1 and prv[0]["p_alvo"] == pytest.approx(0.4)
    ep = _db(banco, "SELECT * FROM posicoes_episodio WHERE braco='sombra_sem_gestao'")
    assert ep and ep[0]["sombra_de"] == "planejador"


async def test_ancoragem_bloqueia_perseguicao_acima_da_faixa():
    p = PlanejadorRoteirizado("planejador", [lambda s, c: _plano_agora(s, c, favor=30.0), _nada])
    orq, banco, _ = montar_orq([p])
    # dentro da faixa a favor (o ask anda meio spread desde a referencia de t0): entra
    await alimentar(orq, [tk(0, 130000.0), tk(1000, 130000.0), tk(2000, 130000.0)])
    assert orq.sim.posicao("planejador").quantidade == 1  # dentro da faixa: entra

    p2 = PlanejadorRoteirizado("planejador", [lambda s, c: _plano_agora(s, c, favor=5.0, contra=50.0), _nada])
    orq2, banco2, _ = montar_orq([p2])
    await alimentar(orq2, [tk(0, 130000.0), tk(1000, 130000.0, ask=130010.0), tk(2000, 130000.0, ask=130010.0)])
    dec = _db(banco2, "SELECT resultado, motivos FROM decisoes WHERE gatilho='plano_disparado'")
    assert dec and dec[0]["resultado"] == "bloqueada" and "perseguicao_acima_do_aceito" in dec[0]["motivos"]
    assert orq2.sim.posicao("planejador").quantidade == 0


async def test_contexto_do_planejador_tem_envelope_e_dossie_v2_sem_limites():
    p = PlanejadorRoteirizado("planejador", [_nada])
    orq, _, _ = montar_orq([p])
    await alimentar(orq, serie([130000.0] * 3))
    ctx = p.contextos[-1]
    assert ctx.envelope is not None and ctx.envelope.entradas_possiveis
    assert ctx.dossie["meta"]["versao_dossie"] == 2 and "limites" not in ctx.dossie
    assert ctx.dossie["envelope"]["unidades_max"] == ctx.envelope.unidades_max
    assert ctx.custo_ida_volta_pontos > 0 and ctx.plano_vigente is False
    texto = json.dumps(ctx.dossie)
    assert "_brl" not in texto and "perda_max" not in texto
    assert "rv_1m" not in FEATURES_DOSSIE_V2 and set(FEATURES_DOSSIE_V2) < set(FEATURES_DOSSIE)


async def test_dossie_mostra_o_stop_atual_depois_de_um_aperto():
    def apertar(s, c):
        it = s._intencao(c, Acao.MANTER, stop=20.0, alvo=None, tempo_max_s=None, validade_ns=c.t0_ns + 60 * NS)
        return s._resultado(c, Acao.MANTER, "manter", intencao=it)

    b = BracoRoteirizado("a", [lambda s, c: _entrada(c, s, stop=100.0, alvo=300.0, tempo=300), _nada, apertar, _nada],
                         ia=True)
    orq, _, _ = montar_orq([b])
    await alimentar(orq, serie([130000.0] * 2 + [130050.0] * 4))
    pos = orq.sim.posicao("a")
    assert pos.stop is not None and pos.preco_medio - pos.stop < 100.0  # apertou
    d = b.contextos[-1].dossie
    assert d["posicao"]["stop_pontos"] == pytest.approx(abs(pos.preco_medio - pos.stop))  # atual, nao o original (C4)


def test_espelhar_sombra_ou_controle_e_recusado_na_construcao():
    from robowin.core.config import ErroConfig
    s1 = BracoSombra("sombra_sem_gestao", "a", "sem_gestao")
    with pytest.raises(ErroConfig):
        montar_orq([BracoRoteirizado("a", [_nada]), s1], espelhar="sombra_sem_gestao")


# ---------------------------------------------------------------------------------------------------------
# ponta a ponta com o provedor FALSO
# ---------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sessao_falso(tmp_path_factory):
    from robowin.cli import main
    saida = tmp_path_factory.mktemp("simp") / "s"
    with contextlib.redirect_stdout(io.StringIO()):
        rc = main(["simular", "--saida", str(saida), "--seed", "7"])
    c = sqlite3.connect(saida / "robowin.db")
    c.row_factory = sqlite3.Row
    return rc, c


def test_e2e_falso_planejador_episodios_gestao_e_invariantes(sessao_falso):
    rc, c = sessao_falso
    assert rc == 0
    q = lambda s, *a: [dict(r) for r in c.execute(s, a).fetchall()]  # noqa: E731
    assert q("SELECT COUNT(*) n FROM posicoes_episodio WHERE braco='planejador'")[0]["n"] >= 1
    assert q("SELECT COUNT(*) n FROM gestao_eventos WHERE origem='auto' AND aplicado=1")[0]["n"] >= 1
    for ep in q("SELECT * FROM posicoes_episodio WHERE braco='planejador' AND t_fechada_ns IS NOT NULL"):
        assert ep["t_fechada_ns"] - ep["t_aberta_ns"] <= (300 + 2) * NS
    for (ticket,) in c.execute("SELECT DISTINCT ticket FROM gestao_eventos").fetchall():
        linhas = q("SELECT g.sl_depois, p.lado FROM gestao_eventos g JOIN posicoes_episodio p ON p.ticket=g.ticket "
                   "AND p.braco=g.braco WHERE g.ticket=? ORDER BY g.t_ns", ticket)
        sls = [x["sl_depois"] for x in linhas if x["sl_depois"] is not None]
        sinal = 1 if linhas and linhas[0]["lado"] == "compra" else -1
        assert all(sinal * (b - a) >= -1e-9 for a, b in zip(sls, sls[1:], strict=False))  # nunca afrouxa
    assert q("SELECT COUNT(*) n FROM previsoes_ia WHERE braco LIKE 'planejador%'")[0]["n"] >= 1
    assert q("SELECT COUNT(*) n FROM decisoes WHERE braco='sombra_sem_gestao' AND gatilho='sombra'")[0]["n"] >= 1
    # nenhuma decisao executada de MANTER/ENCERRAR de outra posicao: a entrega confere o ticket de t0
    for d in q("SELECT * FROM decisoes WHERE resultado='executada' AND acao IN ('MANTER','ENCERRAR') "
               "AND braco LIKE 'planejador%'"):
        it = json.loads(d["intencao_json"]) if d.get("intencao_json") else {}
        assert it.get("posicao_ticket") is not None
