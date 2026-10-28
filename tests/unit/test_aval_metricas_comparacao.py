import datetime as dt
import math

import numpy as np
import pytest
from perfis_teste import WIN
from scipy import stats

from robowin.avaliacao import comparacao as comp
from robowin.avaliacao import metricas
from robowin.core.tipos import Lado, Trade

SP = dt.timezone(dt.timedelta(hours=-3))


def mk(i, liquido, dia="2026-11-03", hora=10, braco="A", slip=0.5, tarifa=0.5, corr=0.0):
    t = dt.datetime.fromisoformat(f"{dia}T{hora:02d}:{i % 60:02d}:00").replace(tzinfo=SP)
    ns = int(t.timestamp() * 1e9)
    return Trade(id=f"{braco}-{dia}-{i}", braco=braco, lado=Lado.COMPRA, entrada=100000, saida=100010,
                 t_entrada_ns=ns - 1_000_000_000, t_saida_ns=ns, bruto_brl=liquido + tarifa + corr + slip,
                 tarifas_brl=tarifa, corretagem_brl=corr, slippage_brl=slip, motivo_saida="alvo")


def test_resumo_basico():
    ts = [mk(1, 20), mk(2, -60), mk(3, 20), mk(4, 20), mk(5, -10, dia="2026-11-04")]
    r = metricas.resumo(ts)
    assert r.n == 5 and r.dias == 2 and r.acertos == 3
    assert r.taxa_acerto == pytest.approx(0.6)
    assert r.lucro_liquido_brl == pytest.approx(-10)
    assert r.ganho_medio_brl == pytest.approx(20) and r.perda_media_brl == pytest.approx(35)
    assert r.expectativa_brl == pytest.approx(-2)
    assert r.fator_lucro == pytest.approx(60 / 70)
    assert r.periodo_ini == "2026-11-03" and r.periodo_fim == "2026-11-04"
    assert r.slippage_total_brl == pytest.approx(2.5)
    assert r.custos_brl == pytest.approx(2.5)


def test_acerto_exige_liquido_positivo():
    # bruto positivo mas liquido <= 0 nao e acerto
    t = mk(1, 0.0)
    assert metricas.resumo([t]).acertos == 0


def test_drawdown_e_curva():
    ts = [mk(1, 10), mk(2, -30), mk(3, -10), mk(4, 25), mk(5, 20)]
    assert list(metricas.curva_capital(ts)) == [10, -20, -30, -5, 15]
    dd = metricas.drawdown(ts)
    assert dd.maximo_brl == pytest.approx(40) and dd.recuperado
    assert dd.duracao_operacoes == 4  # do pico (op 1) ate recuperar (op 5)
    dd2 = metricas.drawdown([mk(1, 10), mk(2, -30)])
    assert dd2.maximo_brl == pytest.approx(30) and not dd2.recuperado
    assert metricas.drawdown([]).maximo_brl == 0
    assert metricas.drawdown([mk(1, -5)]).maximo_brl == pytest.approx(5)


def test_distribuicao_diaria_e_vazio():
    ts = [mk(1, 10), mk(2, 5), mk(3, -3, dia="2026-11-04")]
    assert metricas.pnl_por_dia(ts) == {"2026-11-03": 15.0, "2026-11-04": -3.0}
    d = metricas.distribuicao_diaria(ts)
    assert d["dias"] == 2 and d["dias_positivos"] == 1 and d["dias_negativos"] == 1
    assert metricas.resumo([]).n == 0
    assert metricas.distribuicao_diaria([])["dias"] == 0


def test_fator_lucro_sem_perdas():
    assert math.isinf(metricas.fator_de_lucro(np.array([1.0, 2.0])))
    assert math.isnan(metricas.fator_de_lucro(np.array([0.0])))


# ----------------------------------------------------------- comparacao
def test_holm_contra_implementacao_de_referencia():
    from statsmodels.stats.multitest import multipletests
    p = {"a": 0.001, "b": 0.012, "c": 0.03, "d": 0.04, "e": 0.30}
    h = comp.holm(p, 0.05)
    rej, aj, _, _ = multipletests(list(p.values()), alpha=0.05, method="holm")
    for nome, r, a in zip(p, rej, aj, strict=True):
        assert h[nome]["p_ajustado"] == pytest.approx(a)
        assert h[nome]["rejeita_h0"] == bool(r)
    # Holm para na primeira nao-rejeicao
    h2 = comp.holm({"x": 0.03, "y": 0.04, "z": 0.001}, 0.05)
    assert h2["z"]["rejeita_h0"] and not h2["x"]["rejeita_h0"] and not h2["y"]["rejeita_h0"]


def test_diferencas_pareadas():
    rng = np.random.default_rng(0)
    base = {f"d{i}": float(rng.normal(0, 30)) for i in range(60)}
    a = {k: v + 4 + rng.normal(0, 3) for k, v in base.items()}
    r = comp.diferencas_pareadas(a, base)
    d = np.array([a[k] - base[k] for k in base])
    assert r["n_pares"] == 60 and r["media_dif"] == pytest.approx(d.mean())
    assert r["p_valor_bilateral"] == pytest.approx(stats.ttest_rel([a[k] for k in base], list(base.values())).pvalue)
    assert r["p_valor_unilateral"] < 0.001
    assert r["ic_bootstrap"][0] > 0
    # so chaves comuns entram; n pre-registrado maior => amostra insuficiente
    r2 = comp.diferencas_pareadas({"x": 1, "y": 2, "z": 3}, {"y": 1, "z": 1, "w": 5}, n_preregistrado=10)
    assert r2["n_pares"] == 2 and r2["status"] == "amostra_insuficiente"


def test_valor_do_veto():
    ts = [mk(i, -60, dia="2026-11-03" if i % 2 else "2026-11-04") for i in range(1, 21)]
    ts += [mk(100 + i, 20) for i in range(10)]
    ids = {t.id for t in ts if t.liquido_brl < 0}
    r = comp.valor_do_veto(ts, ids, n_boot=300)
    assert r["n_rejeitadas"] == 20 and r["expectativa_rejeitadas_brl"] == pytest.approx(-60)
    assert r["valor_veto_total_brl"] == pytest.approx(1200) and r["veto_agrega_valor"]
    r2 = comp.valor_do_veto(ts, ids, n_boot=100, n_preregistrado=50)
    assert r2["status"] == "amostra_insuficiente" and not r2["veto_agrega_valor"]
    assert comp.valor_do_veto(ts, set())["n_rejeitadas"] == 0


def test_beneficio_liquido_do_llm():
    rng = np.random.default_rng(1)
    llm, base = [], []
    for d in range(30):
        dia = (dt.date(2026, 11, 3) + dt.timedelta(days=d)).isoformat()
        for i in range(4):
            llm.append(mk(i, 6 + float(rng.normal(0, 2)), dia=dia, braco="B"))
            base.append(mk(i, 1 + float(rng.normal(0, 2)), dia=dia, braco="A"))
    r = comp.beneficio_liquido_llm(llm, base, custo_api_brl=120 * 1.0)  # R$ 1 por operacao
    assert r["expectativa_incremental_brl"] == pytest.approx(5, abs=1.0)
    assert r["custo_api_por_operacao_brl"] == pytest.approx(1.0)
    assert r["beneficio_liquido_brl"] == pytest.approx(r["expectativa_incremental_brl"] - 1.0)
    assert r["llm_se_justifica"]
    caro = comp.beneficio_liquido_llm(llm, base, custo_api_brl=120 * 20.0)
    assert caro["beneficio_liquido_brl"] < 0 and not caro["llm_se_justifica"]
    assert comp.beneficio_liquido_llm(llm, base, 1.0, n_preregistrado=1000)["status"] == "amostra_insuficiente"


def test_controle_aleatorio_e_teste_contra_controle():
    assert comp.acerto_controle_teorico(300, 100) == pytest.approx(0.75)
    x = comp.gerar_controle_aleatorio(20000, 300, 100, custos_brl=2.0, ponto_valor_brl=WIN.ponto_valor_brl, seed=3)
    assert (x > 0).mean() == pytest.approx(0.75, abs=0.01)
    assert set(np.round(np.unique(x), 6)) == {18.0, -62.0}
    # 80% em 300 contra controle de 75%: diferenca significativa
    r = comp.comparar_com_controle(255, 300, 225, 300)
    assert r["p_valor_braco_maior"] < 0.05 and r["status"] == "ok"
    assert r["p_valor_braco_maior"] == pytest.approx(
        stats.fisher_exact([[255, 45], [225, 75]], alternative="greater")[1])
    # braco igual ao controle: nao significativo
    assert comp.comparar_com_controle(225, 300, 225, 300)["p_valor_braco_maior"] > 0.4
    assert comp.comparar_com_controle(8, 10, 7, 10, n_preregistrado=300)["status"] == "amostra_insuficiente"
