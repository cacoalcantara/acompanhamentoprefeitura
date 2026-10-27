"""Latencia injetada: amostragem empirica deterministica, carimbos injetado=True, origem em banco."""
from __future__ import annotations

import pytest

from robowin.backtest import DistribuicaoLatencia, carimbo_injetado
from robowin.backtest.latencia_injetada import ErroLatencia, linha_latencia
from robowin.dados.banco import BancoSQLite


def test_amostrador_so_devolve_valores_da_distribuicao_e_e_deterministico():
    amostras = [10.0, 20.0, 35.0, 90.0, 400.0]
    dist = DistribuicaoLatencia.de_amostras(amostras)
    a, b = dist.amostrador(7), dist.amostrador(7)
    xs = [a.amostrar_ms() for _ in range(200)]
    assert xs == [b.amostrar_ms() for _ in range(200)]
    assert set(xs) <= set(amostras) and len(set(xs)) == len(amostras)
    assert [x for x in dist.amostrador(8).drenar()] == []
    assert a.drenar() == xs and a.drenar() == []


def test_amostrador_em_ns_e_suave_dentro_do_intervalo():
    dist = DistribuicaoLatencia.de_amostras([10.0, 100.0], suave=True)
    am = dist.amostrador(1)
    v = [am() for _ in range(300)]
    assert all(10_000_000 <= x <= 100_000_000 for x in v) and len(set(v)) > 50


def test_distribuicao_invalida_e_recusada():
    for ruim in ([], [-1.0], [float("nan")], [float("inf")]):
        with pytest.raises(ErroLatencia):
            DistribuicaoLatencia.de_amostras(ruim)


def test_carimbo_injetado_e_linha_do_banco():
    c = carimbo_injetado("execucao_ordem", "b1", 1_000, 12.5)
    assert c.injetado is True and c.dur_ms == pytest.approx(12.5)
    assert linha_latencia(c)["injetado"] == 1


def test_distribuicao_medida_vem_do_banco_ignorando_injetadas():
    banco = BancoSQLite(":memory:")
    for ms, inj in [(30.0, 0), (45.0, 0), (999.0, 1)]:
        banco.inserir("latencias", {"t_ns": 1, "braco": "b", "etapa": "order_send", "dur_ms": ms, "injetado": inj,
                                    "modelo": "", "effort": ""})
    dist = DistribuicaoLatencia.de_banco(banco, "order_send")
    assert sorted(dist.amostras_ms) == [30.0, 45.0] and dist.origem == "banco:order_send"
    assert dist.quantis()["p50"] == pytest.approx(37.5)
    with pytest.raises(ErroLatencia):
        DistribuicaoLatencia.de_banco(banco, "inexistente")
