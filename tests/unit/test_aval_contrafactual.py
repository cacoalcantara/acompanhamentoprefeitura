"""Contrafactual: replay deterministico (mesma seed, mesmo resultado), paridade com o trade real sem gestao,
saida por tempo e contrafactuais da sessao (y_alvo resolvido pelo plano original sem gestao). Dados SINTETICOS."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
from perfis_teste import WIN

from robowin.avaliacao import contrafactual as cf
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import Acao, ComandoOrdem, Intencao, Lado, Tick
from robowin.dados import decisao_ia
from robowin.dados.banco import BancoSQLite
from robowin.execucao.custos import Custos
from robowin.execucao.simulador import SimuladorExecucao

NS = 1_000_000_000
MS = 1_000_000
T0 = int(dt.datetime(2026, 10, 23, 13, 0, tzinfo=dt.UTC).timestamp()) * NS  # 10h em Sao Paulo
TK = WIN.tick_pontos


def _tick(t: int, preco: float) -> Tick:
    return Tick(t_servidor_ms=t // MS, t_utc_ns=t, t_recebido_ns=t, bid=preco, ask=preco + TK, last=preco,
                volume=1.0, flags=8 | 16)


def _caminho(passos: list[int], inicio: float, passo_s: float = 1.0) -> list[Tick]:
    preco, out = inicio, []
    for i, d in enumerate(passos):
        preco += d * TK
        out.append(_tick(T0 + int(i * passo_s * NS), preco))
    return out


def _aleatorio(seed: int, n: int = 600, deriva: float = 0.0) -> list[Tick]:
    rng = np.random.default_rng(seed)
    passos = rng.choice([-1, 0, 1], n, p=[0.4 - deriva, 0.2, 0.4 + deriva]).tolist()
    return _caminho([0] * 5 + passos, 100_000 * TK)


def _kw(**extra):
    kw = dict(t_entrada_ns=T0 + 2 * NS, lado=Lado.COMPRA, stop=40 * TK, alvo=40 * TK, tempo_max_s=10_000, q=1,
              gestao=None, seed=11, latencia_ns=200 * MS)
    kw.update(extra)
    return kw


def test_mesma_seed_mesmo_resultado():
    ticks = _aleatorio(1)
    a = cf.replay_operacao(ticks, [], WIN, **_kw(stop=12 * TK, alvo=12 * TK))
    b = cf.replay_operacao(ticks, [], WIN, **_kw(stop=12 * TK, alvo=12 * TK))
    sem_id = [t.model_dump(exclude={"id"}) for t in a.trades]
    assert sem_id == [t.model_dump(exclude={"id"}) for t in b.trades]  # o id do trade e um contador global
    assert (a.liquido, a.motivo, a.t_fim, a.y_alvo) == (b.liquido, b.motivo, b.t_fim, b.y_alvo)
    assert a.motivo in ("alvo", "stop")
    assert cf.semente_de("dec-1") == cf.semente_de("dec-1") != cf.semente_de("dec-2")
    assert 0 <= cf.semente_de("x") < 2 ** 63


def test_replay_sem_gestao_bate_com_o_trade_real():
    ticks = _aleatorio(7, deriva=0.05)
    r = cf.replay_operacao(ticks, [], WIN, **_kw(seed=5, stop=12 * TK, alvo=12 * TK))
    # o "trade real": o mesmo simulador dirigido a mao, como o orquestrador faz (tick primeiro, depois a ordem)
    ins = WIN.instrumento()
    rel = RelogioSimulado(ticks[0].t_utc_ns)
    sim = SimuladorExecucao(ins, rel, Custos.do_instrumento(ins), latencia_ns=200 * MS, seed=5)
    sim.registrar_braco("real", 7)
    enviado = False
    for tk in ticks:
        rel.avancar_para(tk.t_utc_ns)
        sim.on_tick(tk)
        if not enviado and tk.t_utc_ns >= T0 + 2 * NS:
            enviado = True
            ref = tk.meio
            sim.enviar(ComandoOrdem(intencao_id="x", magic=7, lado=Lado.COMPRA, tipo="mercado",
                                    sl=round(np.ceil((ref - 12 * TK) / TK - 1e-9) * TK, 10),
                                    tp=round(np.floor((ref + 12 * TK) / TK + 1e-9) * TK, 10)))
        if sim.carteira("real").trades:
            break
    real = sim.carteira("real").trades[0]
    assert len(r.trades) == 1
    assert r.liquido == pytest.approx(real.liquido_brl)
    assert r.motivo == real.motivo_saida and r.t_fim == real.t_saida_ns
    assert r.preco_entrada == real.entrada and r.y_alvo == int(real.motivo_saida == "alvo")


def test_saida_por_tempo_e_referencia_ancorada():
    ticks = _caminho([0] * 200, 100_000 * TK)  # mercado parado: nem stop nem alvo
    r = cf.replay_operacao(ticks, [], WIN, **_kw(tempo_max_s=30, latencia_ns=0))
    assert r.motivo == "tempo" and r.y_alvo == 0
    assert 30 * NS <= r.t_fim - r.t_entrada_ns <= 32 * NS
    assert r.liquido < 0  # spread + custos
    # compra com ancora ABAIXO do preco de execucao: alvo mais perto (niveis absolutos fixos da ancora de t0)
    sobe = _caminho([0] * 5 + [1] * 12, 100_000 * TK)
    perto = cf.replay_operacao(sobe, [], WIN, **_kw(referencia=100_000 * TK - 10 * TK, alvo=15 * TK,
                                                     latencia_ns=0))
    longe = cf.replay_operacao(sobe, [], WIN, **_kw(alvo=15 * TK, latencia_ns=0))
    assert perto.motivo == "alvo" and longe.motivo == "fim_dos_dados"


def test_sem_entrada_e_validacoes():
    ticks = _caminho([0] * 10, 100_000 * TK)
    assert cf.replay_operacao(ticks, [], WIN, **_kw(t_entrada_ns=T0 + 999 * NS)).motivo == "sem_entrada"
    assert cf.replay_operacao([], [], WIN, **_kw()).motivo == "sem_entrada"
    with pytest.raises(ValueError):
        cf.replay_operacao(ticks, [], WIN, **_kw(stop=0.0))


def test_trailing_fixo_pre_registrado():
    g = cf.trailing_fixo(40 * TK, TK)
    assert len(g.regras) == 1 and g.regras[0].tipo == "trailing"
    assert g.regras[0].mfe_pontos == pytest.approx(40 * TK) and g.regras[0].distancia_pontos == pytest.approx(40 * TK)
    assert g.passo_min_pontos == TK


def test_replay_com_gestao_trailing_trava_lucro():
    pytest.importorskip("robowin.decisao.gestao")
    sobe_e_cai = _caminho([0] * 5 + [1] * 120 + [-1] * 200, 100_000 * TK)
    sem = cf.replay_operacao(sobe_e_cai, [], WIN, **_kw(alvo=None, latencia_ns=0))
    com = cf.replay_operacao(sobe_e_cai, [], WIN, **_kw(alvo=None, latencia_ns=0,
                                                        gestao=cf.trailing_fixo(40 * TK, TK)))
    assert sem.motivo == "stop" and sem.liquido < 0
    assert com.acoes_gestao > 0 and com.liquido > sem.liquido


def _banco_com_previsoes(tmp_path, executada: bool):
    b = BancoSQLite(tmp_path / "s.db")
    decisao_ia.garantir_tabelas(b)
    it = Intencao(id="int-1", braco="planejador", acao=Acao.COMPRAR, stop_pontos=40 * TK, alvo_pontos=40 * TK,
                  tempo_max_s=120, validade_ns=T0 + 60 * NS, preco_referencia=100_000 * TK)
    b.inserir("decisoes", {"id": "dec-1", "t_ns": T0 + 2 * NS, "braco": "planejador", "acao": "COMPRAR",
                           "intencao_json": it.model_dump_json(), "resultado": "executada" if executada else "vetada"})
    b.inserir(decisao_ia.PREVISOES, decisao_ia.linha(decisao_ia.PREVISOES, {
        "id": "prev-1", "decisao_id": "dec-1", "t0_ns": T0, "braco": "planejador", "lado": "compra", "p_alvo": 0.6,
        "incerteza": 0.4, "stop_pts": 40 * TK, "alvo_pts": 40 * TK, "tempo_s": 120, "executada": executada}))
    b.inserir(decisao_ia.PREVISOES, decisao_ia.linha(decisao_ia.PREVISOES, {
        "id": "prev-2", "decisao_id": "dec-2", "t0_ns": T0, "braco": "planejador", "lado": None, "p_alvo": None}))
    return b


def test_contrafactuais_da_sessao_nao_executada_resolve_y(tmp_path):
    b = _banco_com_previsoes(tmp_path, executada=False)
    ticks = _caminho([0] * 5 + [1] * 60, 100_000 * TK)  # sobe: o plano teria atingido o alvo
    linhas = cf.contrafactuais_da_sessao(b, ticks, perfil=WIN)
    assert {ln["politica"] for ln in linhas} == {"plano_sem_gestao", "plano_ia"}
    assert all(ln["decisao_id"] == "dec-1" and ln["seed"] == cf.semente_de("dec-1") for ln in linhas)
    assert all(ln["motivo"] == "alvo" and ln["y_alvo"] == 1 for ln in linhas)
    prev = {r["id"]: r for r in decisao_ia.ler(tmp_path / "s.db", decisao_ia.PREVISOES)}
    assert prev["prev-1"]["y_alvo"] == 1 and prev["prev-1"]["fonte_y"] == "replay_sem_gestao"
    assert prev["prev-1"]["resolvido_ns"] > T0 and prev["prev-2"]["y_alvo"] is None
    gravadas = decisao_ia.ler(tmp_path / "s.db", decisao_ia.CONTRAFACTUAIS)
    assert len(gravadas) == 2
    # idempotente: rodar de novo nao duplica
    cf.contrafactuais_da_sessao(b, ticks, perfil=WIN)
    assert len(decisao_ia.ler(tmp_path / "s.db", decisao_ia.CONTRAFACTUAIS)) == 2


def test_contrafactuais_da_sessao_executada_tem_sombras(tmp_path):
    pytest.importorskip("robowin.decisao.gestao")
    b = _banco_com_previsoes(tmp_path, executada=True)
    ticks = _caminho([0] * 5 + [-1] * 60, 100_000 * TK)
    linhas = cf.contrafactuais_da_sessao(b, ticks, perfil=WIN, gravar=False)
    assert {ln["politica"] for ln in linhas} == {"plano_sem_gestao", "sem_gestao", "trailing_fixo"}
    assert decisao_ia.ler(tmp_path / "s.db", decisao_ia.CONTRAFACTUAIS) == []


def test_banco_sem_tabelas_devolve_vazio(tmp_path):
    assert cf.contrafactuais_da_sessao(BancoSQLite(tmp_path / "v.db"), [], perfil=WIN) == []


def _banco_latencia(tmp_path, *, executada: bool, t_exec: int | None = None):
    """Dossie em T0, decisao ENTREGUE em T0+10 s (latencia da IA); o preco sobe 40 ticks durante a latencia."""
    b = BancoSQLite(tmp_path / "lat.db")
    decisao_ia.garantir_tabelas(b)
    it = Intencao(id="int-9", braco="planejador", acao=Acao.COMPRAR, stop_pontos=40 * TK, alvo_pontos=40 * TK,
                  tempo_max_s=60, validade_ns=T0 + 60 * NS, preco_referencia=100_000 * TK)
    b.inserir("decisoes", {"id": "dec-9", "t_ns": T0, "braco": "planejador", "acao": "COMPRAR",
                           "intencao_json": it.model_dump_json(), "resultado": "plano_armado"})
    b.inserir(decisao_ia.PREVISOES, decisao_ia.linha(decisao_ia.PREVISOES, {
        "id": "prev-9", "decisao_id": "dec-9", "t0_ns": T0, "t_entrega_ns": T0 + 10 * NS, "braco": "planejador",
        "lado": "compra", "p_alvo": 0.6, "stop_pts": 40 * TK, "alvo_pts": 40 * TK, "tempo_s": 60,
        "executada": executada, "t_execucao_ns": t_exec}))
    return b


def test_contrafactual_parte_da_entrega_e_nao_de_t0(tmp_path):
    """Causalidade: o movimento ocorrido durante a latencia da IA nao entra no y_alvo nem no plano_ia."""
    b = _banco_latencia(tmp_path, executada=False)
    ticks = _caminho([0] + [4] * 10 + [0] * 200, 100_000 * TK)  # +40 ticks entre T0 e a entrega, depois parado
    linhas = {ln["politica"]: ln for ln in cf.contrafactuais_da_sessao(b, ticks, perfil=WIN)}
    assert linhas["plano_sem_gestao"]["motivo"] == "tempo" and linhas["plano_sem_gestao"]["y_alvo"] == 0
    assert linhas["plano_ia"]["motivo"] == "tempo"
    prev = decisao_ia.ler(tmp_path / "lat.db", decisao_ia.PREVISOES)[0]
    assert prev["y_alvo"] == 0 and prev["t_entrega_ns"] == T0 + 10 * NS


def test_contrafactual_executada_entra_no_disparo(tmp_path):
    pytest.importorskip("robowin.decisao.gestao")
    # entrega em T0+10 s, disparo real em T0+20 s; entre os dois o preco sobe 40 ticks
    b = _banco_latencia(tmp_path, executada=True, t_exec=T0 + 20 * NS)
    ticks = _caminho([0] * 11 + [4] * 10 + [0] * 200, 100_000 * TK)
    linhas = {ln["politica"]: ln for ln in cf.contrafactuais_da_sessao(b, ticks, perfil=WIN, gravar=False)}
    assert linhas["plano_sem_gestao"]["motivo"] == "alvo"  # da entrega: pegou a alta
    assert linhas["sem_gestao"]["motivo"] == "tempo" and linhas["trailing_fixo"]["motivo"] == "tempo"
    assert linhas["sem_gestao"]["t_fim_ns"] >= T0 + 80 * NS
