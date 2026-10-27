import numpy as np
import pytest

from robowin.core.tipos import Lado
from robowin.dados.sintetico import ConfigSintetico, gerar
from robowin.mm.avellaneda_stoikov import (
    ParamsAS,
    cotacoes_continuas,
    discretizar,
    preco_reserva,
    spread_otimo,
)
from robowin.mm.config import ConfigMM, exigir_simulador_apenas
from robowin.mm.cotacao import CotadorMM
from robowin.mm.markouts import Preenchimento, markouts
from robowin.mm.simulacao import simular_mm

NS = 1_000_000_000
P = ParamsAS()


def estado(mp=130002.5, q=0, sigma=3.0, bb=130000.0, ba=130005.0, **kw):
    return {"microprice": mp, "melhor_bid": bb, "melhor_ask": ba, "estoque": q, "sigma": sigma, **kw}


# ---------------------------------------------------------------- Avellaneda-Stoikov -----------------------


def test_as_formulas_e_vies_de_estoque():
    assert spread_otimo(3.0, P) == pytest.approx(P.gamma * 9 * P.tau_s + 2 / P.gamma * np.log(1 + P.gamma / P.k))
    assert preco_reserva(100.0, 1, 3.0, P) < 100.0 < preco_reserva(100.0, -1, 3.0, P)
    b, a = cotacoes_continuas(100.0, 0, 3.0, P)
    assert a - b == pytest.approx(spread_otimo(3.0, P))


def test_as_estoque_limite_remove_o_lado_que_aumenta_posicao():
    assert cotacoes_continuas(100.0, 1, 3.0, P)[0] is None
    assert cotacoes_continuas(100.0, -1, 3.0, P)[1] is None
    b, a = cotacoes_continuas(100.0, 0, 3.0, P)
    assert b is not None and a is not None


def test_as_discretiza_no_tick_sem_cruzar_nem_ser_isca():
    for mp in np.linspace(129990, 130020, 31):
        for sg in (0.5, 3.0, 10.0):
            b, a = discretizar(*cotacoes_continuas(float(mp), 0, sg, P), 130000.0, 130005.0, P)
            assert b % 5 == 0 and a % 5 == 0
            assert b <= 130000.0 and a >= 130005.0          # nunca cruza o livro
            assert b >= 130000.0 - 2 * 5 and a <= 130005.0 + 2 * 5  # nunca longe do topo (isca)
    # deriva prevista positiva empurra as cotacoes para cima
    b0, a0 = cotacoes_continuas(100.0, 0, 3.0, P)
    b1, a1 = cotacoes_continuas(100.0 + 7, 0, 3.0, P)
    assert b1 > b0 and a1 > a0


# ---------------------------------------------------------------- Cotador ---------------------------------


def _roteiro(cot, n=400, passo_s=0.5, tox=None):
    """Estado que oscila de forma agressiva para forcar o cotador a querer mudar a cotacao a toda hora."""
    rng = np.random.default_rng(0)
    t0 = 1_700_000_000 * NS
    livre: list[dict] = []
    for i in range(n):
        t = t0 + int(i * passo_s * NS)
        mp = 130000.0 + 5.0 * rng.integers(-6, 7)
        bb = float(np.floor(mp / 5) * 5)
        e = estado(mp=mp, bb=bb, ba=bb + 5, sigma=float(rng.uniform(1, 8)),
                   toxicidade=0.0 if tox is None else tox(i))
        livre.append({"t": t, "ret": cot.cotar(t, e), "msgs": list(cot.ultimas_mensagens)})
    return livre


def test_cotador_respeita_tempo_minimo_de_2s_no_livro():
    cot = CotadorMM(cfg=ConfigMM(mensagens_min_max=20, mensagens_sessao_max=10_000))
    _roteiro(cot)
    ult: dict[str, int] = {}
    for m in cot.historico:
        if m.tipo == "nova":
            ult[m.ordem_id] = m.t_ns
        else:
            assert m.t_ns - ult[m.ordem_id] >= 2 * NS, m
            if m.tipo == "modificacao":
                ult[m.ordem_id] = m.t_ns
    assert not [al for al in cot.monitor.alertas if al.tipo == "tempo_minimo_no_livro"]


def test_cotador_respeita_orcamento_de_mensagens_por_minuto_e_por_sessao():
    cot = CotadorMM(cfg=ConfigMM(mensagens_min_max=6, mensagens_sessao_max=10_000))
    _roteiro(cot, n=600)
    ts = np.array([m.t_ns for m in cot.historico])
    assert len(ts) > 6
    for t in ts:  # nenhuma janela de 60 s tem mais que 6 mensagens
        assert int(((ts >= t) & (ts < t + 60 * NS)).sum()) <= 6
    cot2 = CotadorMM(cfg=ConfigMM(mensagens_min_max=20, mensagens_sessao_max=15))
    _roteiro(cot2, n=600)
    assert len(cot2.historico) <= 15


def test_cotador_estoque_um_uma_ordem_por_lado_e_sem_cruzar():
    cot = CotadorMM(cfg=ConfigMM(mensagens_min_max=30, mensagens_sessao_max=10_000))
    rng = np.random.default_rng(1)
    t0 = 1_700_000_000 * NS
    for i in range(300):
        q = int(rng.integers(-1, 2))
        mp = 130000.0 + 5.0 * rng.integers(-3, 4)
        bb = float(np.floor(mp / 5) * 5)
        ret = cot.cotar(t0 + i * 3 * NS, estado(mp=mp, q=q, bb=bb, ba=bb + 5))
        assert len(cot.vivas) <= 2
        if ret["bid"] is not None:
            assert ret["bid"] <= bb
        if ret["ask"] is not None:
            assert ret["ask"] >= bb + 5
    # a politica de alvo nunca pede bid com q=1 nem ask com q=-1
    assert cot.alvo(estado(q=1))["bid"] is None and cot.alvo(estado(q=-1))["ask"] is None


def test_cotador_cancela_por_toxicidade_so_apos_tempo_minimo():
    cot = CotadorMM(cfg=ConfigMM(mensagens_min_max=30))
    t0 = 1_700_000_000 * NS
    r = cot.cotar(t0, estado())
    assert r["bid"] is not None and r["ask"] is not None
    r = cot.cotar(t0 + 1 * NS, estado(toxicidade=0.95))  # < 2 s: nao pode cancelar
    assert r["bid"] is not None and not cot.ultimas_mensagens
    r = cot.cotar(t0 + 2 * NS, estado(toxicidade=0.95))
    assert r == {"bid": None, "ask": None}
    assert {m.tipo for m in cot.ultimas_mensagens} == {"cancelamento"}
    r = cot.cotar(t0 + 10 * NS, estado(toxicidade=0.95))  # tóxico: nao recota
    assert r == {"bid": None, "ask": None} and not cot.ultimas_mensagens


def test_cotador_execucao_remove_ordem_do_lado():
    cot = CotadorMM()
    t0 = 1_700_000_000 * NS
    cot.cotar(t0, estado())
    cot.registrar_execucao("bid", t0 + NS)
    assert "bid" not in cot.vivas and "ask" in cot.vivas


def test_flag_demo_mm_desligada_por_padrao_e_nao_implementada():
    assert ConfigMM().demo_mm is False
    exigir_simulador_apenas(ConfigMM())
    with pytest.raises(NotImplementedError):
        exigir_simulador_apenas(ConfigMM(demo_mm=True))


def test_simulacao_mm_respeita_estoque_unitario_e_orcamento():
    d = gerar(ConfigSintetico(seed=5, n_ticks=12_000))
    cot = CotadorMM(cfg=ConfigMM(mensagens_min_max=10, mensagens_sessao_max=250))
    r = simular_mm(d.ticks, d.livros, cot, seed=1)
    assert r.max_abs_estoque <= 1
    assert len(cot.historico) <= 250
    assert len(r.fills) > 0
    ts = np.array([m.t_ns for m in cot.historico])
    for t in ts[:: max(1, len(ts) // 50)]:
        assert int(((ts >= t) & (ts < t + 60 * NS)).sum()) <= 10
    assert not [al for al in cot.monitor.alertas if al.tipo == "tempo_minimo_no_livro"]
    r2 = simular_mm(d.ticks, d.livros, CotadorMM(cfg=ConfigMM(mensagens_min_max=10, mensagens_sessao_max=250)), seed=1)
    assert [(f.t_ns, f.preco) for f in r.fills] == [(f.t_ns, f.preco) for f in r2.fills]  # determinismo


# ---------------------------------------------------------------- Markouts --------------------------------


def test_markouts_em_caso_construido():
    t = np.array([0, 1, 2, 3, 4, 5, 6], dtype=np.int64) * NS
    meio = np.array([100.0, 100.0, 99.0, 98.0, 98.0, 101.0, 102.0])
    fills = [Preenchimento(1 * NS, Lado.COMPRA, 100.0),   # comprou a 100; meio cai: selecao adversa
             Preenchimento(1 * NS, Lado.VENDA, 100.0),    # vendeu a 100; meio cai: a favor
             Preenchimento(6 * NS, Lado.COMPRA, 102.0)]   # sem dados a 1 s em diante
    mat, res, capt = markouts(fills, t, meio, horizontes_s=(1.0, 3.0))
    assert mat[0, 0] == pytest.approx(-1.0) and mat[0, 1] == pytest.approx(-2.0)  # meio(2)=99 ; meio(4)=98
    assert mat[1, 0] == pytest.approx(1.0) and mat[1, 1] == pytest.approx(2.0)
    assert np.isnan(mat[2]).all()
    assert res[0].n == 2 and res[0].n_sem_dados == 1 and res[0].media_pts == pytest.approx(0.0)
    assert capt == pytest.approx(0.0)  # meio no instante do fill = preco nos dois primeiros; 3o: 0 tambem
    # selecao adversa pura: so compras que o mercado derrubou => markout medio negativo
    _, res2, _ = markouts(fills[:1], t, meio, horizontes_s=(1.0, 3.0))
    assert res2[0].media_pts < 0 and res2[1].media_pts < 0
