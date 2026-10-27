"""Features: valores conhecidos e CAUSALIDADE (perturbar o futuro nao muda o presente).

Todos os dados aqui sao SINTETICOS.
"""
import datetime as dt
import math
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from robowin.core.tipos import LivroSnapshot, NivelBook, Tick
from robowin.features.contexto import Contexto
from robowin.features.fluxo import FluxoAgressor, sinal_agressor
from robowin.features.livro import contribuicao_ofi, desequilibrio_l2, microprice
from robowin.features.pipeline import PipelineFeatures, calcular_offline
from robowin.features.volatilidade import VolRealizada, rv_diaria
from robowin.modelos.demo_sintetico import gerar

NS = 1_000_000_000
T0 = 1_773_147_600 * NS


def tk(t_s, bid=100.0, ask=105.0, last=0.0, vol=0.0, flags=6):
    t = T0 + int(t_s * NS)
    return Tick(t_servidor_ms=t // 1_000_000, t_utc_ns=t, t_recebido_ns=t + 1_000_000, bid=bid, ask=ask,
                last=last, volume=vol, flags=flags)


def iguais(a, b):
    assert a.keys() == b.keys()
    for k in a:
        x, y = a[k], b[k]
        assert (math.isnan(x) and math.isnan(y)) or x == pytest.approx(y, abs=1e-9), k


# ---- valores conhecidos --------------------------------------------------------------------------
def test_sinal_agressor_por_flags():
    assert sinal_agressor(32 | 8) == 1 and sinal_agressor(64 | 8) == -1
    assert sinal_agressor(8) == 0 and sinal_agressor(32 | 64) == 0 and sinal_agressor(6) == 0


def test_fluxo_imbalance_e_intensidade():
    f = FluxoAgressor()
    for t, fl in [(1, 32 | 8), (2, 32 | 8), (3, 32 | 8), (4, 64 | 8), (4.5, 8)]:  # ultimo: sem agressor
        f.atualizar(T0 + int(t * NS), tk(t, last=105, vol=1, flags=fl))
    v = f.valores(T0 + 5 * NS)
    assert v["fluxo_imb_5s"] == pytest.approx(0.5)  # (3-1)/4
    assert v["fluxo_intensidade_30s"] == pytest.approx(5 / 30)
    v2 = f.valores(T0 + 40 * NS)  # tudo saiu da janela
    assert v2["fluxo_imb_30s"] == 0.0 and v2["fluxo_volume_30s"] == 0.0


def test_microprice_e_desequilibrio():
    assert microprice(100, 30, 105, 10) == pytest.approx(103.75)  # mais fila na compra -> preco tende ao ask
    lv = LivroSnapshot(t_utc_ns=T0, niveis=(
        NivelBook(preco=100, volume=30, lado="compra"), NivelBook(preco=95, volume=10, lado="compra"),
        NivelBook(preco=105, volume=10, lado="venda"), NivelBook(preco=110, volume=10, lado="venda")))
    assert desequilibrio_l2(lv, 1) == pytest.approx(0.5)
    assert desequilibrio_l2(lv, 5) == pytest.approx(20 / 60)


def test_ofi_cont_kukanov_stoikov():
    base = (100.0, 10.0, 105.0, 20.0)
    assert contribuicao_ofi(base, (100.0, 12.0, 105.0, 15.0)) == pytest.approx(2 + 5)  # mesmos precos
    assert contribuicao_ofi(base, (105.0, 7.0, 110.0, 9.0)) == pytest.approx(7 + 20)  # bid e ask sobem
    assert contribuicao_ofi(base, (95.0, 9.0, 100.0, 4.0)) == pytest.approx(-10 - 4)  # bid e ask descem


def test_contexto_sem_data_e_em_horario_local():
    t = int(dt.datetime(2026, 3, 10, 12, 0, tzinfo=ZoneInfo("America/Sao_Paulo")).timestamp() * NS)
    c = Contexto().valores(t)
    assert c["ctx_min_desde_abertura"] == pytest.approx(180.0)
    assert c["ctx_bucket_30m"] == 6.0
    assert set(c) == {"ctx_min_desde_abertura", "ctx_sazonal_sin", "ctx_sazonal_cos", "ctx_bucket_30m"}
    # mesmo horario em outro dia -> mesmos codigos
    t2 = t + 3 * 86_400 * NS
    assert Contexto().valores(t2) == c


def test_volatilidade_realizada():
    v = VolRealizada()
    for s in range(0, 300):  # preco constante por 5 min
        v.atualizar(T0 + s * NS, 100.0)
    assert v.valores(T0 + 300 * NS)["rv_5m"] == 0.0
    for s in range(300, 600, 5):  # alterna
        v.atualizar(T0 + s * NS, 100.0 if (s // 5) % 2 else 101.0)
    out = v.valores(T0 + 600 * NS)
    assert out["rv_1m"] > 0 and out["rv_5m"] >= out["rv_1m"] * 0.99
    # RV de um dia: precos 100 -> 101 em grade de 300s => retorno log unico
    t = np.array([T0, T0 + 300 * NS, T0 + 600 * NS])
    assert rv_diaria(t, np.array([100.0, 101.0, 101.0]), 300) == pytest.approx(math.log(1.01) ** 2)


def test_vetor_antes_do_ultimo_evento_e_recusado():
    p = PipelineFeatures()
    p.on_tick(tk(10, last=105, vol=1, flags=32 | 8))
    with pytest.raises(ValueError):
        p.vetor(T0 + 5 * NS)
    with pytest.raises(ValueError):
        p.on_tick(tk(5, last=105, vol=1, flags=32 | 8))


# ---- causalidade ---------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def dados():
    return gerar(n_ticks=12_000, seed=3, forca_sinal=0.1, n_dias=1)


def _corromper(ticks, livros, t_corte):
    rng = np.random.default_rng(0)
    tk2 = []
    for x in ticks:
        if x.t_utc_ns > t_corte:
            x = x.model_copy(update={"bid": x.bid + 1000.0 * rng.random(), "ask": x.ask + 1500.0,
                                     "volume": 99.0, "flags": 64 | 8, "last": x.last + 777.0})
        tk2.append(x)
    lv2 = []
    for lv in livros:
        if lv.t_utc_ns > t_corte:
            lv = LivroSnapshot(t_utc_ns=lv.t_utc_ns, niveis=tuple(
                NivelBook(preco=n.preco, volume=n.volume * 50 + 1, lado=n.lado) for n in lv.niveis))
        lv2.append(lv)
    return tk2, lv2


def test_causalidade_perturbar_futuro_nao_muda_features(dados):
    ticks, livros = dados
    t_ini, t_fim = ticks[0].t_utc_ns, ticks[-1].t_utc_ns
    instantes = [t_ini + int((t_fim - t_ini) * q) for q in (0.1, 0.25, 0.4, 0.5)]
    t_corte = instantes[-1]
    base = calcular_offline(ticks, livros, instantes)
    tk2, lv2 = _corromper(ticks, livros, t_corte)
    pert = calcular_offline(tk2, lv2, instantes)
    assert any(x.t_utc_ns > t_corte for x in ticks)
    for a, b in zip(base, pert, strict=True):
        iguais(a.valores, b.valores)


def test_causalidade_truncar_dados_equivale_a_ter_o_futuro(dados):
    ticks, livros = dados
    t = ticks[len(ticks) // 2].t_utc_ns
    completo = calcular_offline(ticks, livros, [t])[0]
    cortado = calcular_offline([x for x in ticks if x.t_utc_ns <= t], [x for x in livros if x.t_utc_ns <= t], [t])[0]
    iguais(completo.valores, cortado.valores)


def test_incremental_igual_ao_offline_e_ao_reprocessar(dados):
    ticks, livros = dados
    instantes = [ticks[0].t_utc_ns + k * 30 * NS for k in range(20, 60)]
    a = calcular_offline(ticks, livros, instantes)
    b = calcular_offline(ticks, livros, instantes)  # determinismo
    for x, y in zip(a, b, strict=True):
        iguais(x.valores, y.valores)
    # incremental manual (sem pre-ordenar): ticks e livros intercalados
    p = PipelineFeatures()
    ev = sorted([(x.t_utc_ns, 0, x) for x in ticks] + [(x.t_utc_ns, 1, x) for x in livros], key=lambda e: e[:2])
    i, vs = 0, []
    for t in instantes:
        while i < len(ev) and ev[i][0] <= t:
            (p.on_tick if ev[i][1] == 0 else p.on_livro)(ev[i][2])
            i += 1
        vs.append(p.vetor(t))
    for x, y in zip(a, vs, strict=True):
        iguais(x.valores, y.valores)


def test_replay_por_disponibilidade_ignora_tick_ainda_nao_recebido():
    # tick com t_utc <= t mas recebido depois de t nao pode entrar quando usar_recebido=True
    atrasado = Tick(t_servidor_ms=0, t_utc_ns=T0 + 1 * NS, t_recebido_ns=T0 + 50 * NS, bid=100, ask=105,
                    last=105, volume=1, flags=32 | 8)
    v = calcular_offline([atrasado], [], [T0 + 10 * NS], usar_recebido=True)[0]
    assert v.valores["fluxo_imb_30s"] == 0.0 and v.valores["fluxo_volume_30s"] == 0.0
    v2 = calcular_offline([atrasado], [], [T0 + 10 * NS], usar_recebido=False)[0]
    assert v2.valores["fluxo_volume_30s"] == 1.0


def test_features_recuperam_o_fluxo_latente_do_gerador():
    ticks, livros = gerar(n_ticks=20_000, seed=5, forca_sinal=0.1)
    inst = [ticks[0].t_utc_ns + k * 20 * NS for k in range(30, 240)]
    vs = calcular_offline(ticks, livros, inst)
    fl = np.array([v.valores["fluxo_imb_30s"] for v in vs])
    li = np.array([v.valores["livro_imb_5"] for v in vs])
    # fluxo e livro derivam do mesmo estado latente: devem andar juntos
    assert np.corrcoef(fl, li)[0, 1] > 0.3
