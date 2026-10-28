from __future__ import annotations

import json
import re

import pytest
from perfis_teste import WIN

from robowin.ia.dossie import (
    ErroDossie,
    caminhos_validos,
    canonico,
    hash_dossie,
    montar_dossie,
    verificar_anonimizacao,
)

S = 1_000_000_000
T0 = 5_000 * S
BASE = 131_245.0


def _ticks(n=1000, drift=0.1):
    return [(T0 - (n - 1 - i) * S, BASE + drift * i + ((i * 7) % 5)) for i in range(n)]


def _dossie(**kw):
    args = dict(t0_ns=T0, ticks=_ticks(), spread_pontos=5.0, tick_pontos=5.0, regime="tendencia",
                limites={"contratos_restantes": 1, "perda_restante_dia_brl": 180.0},
                minutos_desde_abertura=90, minutos_para_zeragem=300)
    args.update(kw)
    return montar_dossie(**args)


def _numeros(d):
    if isinstance(d, dict):
        for v in d.values():
            yield from _numeros(v)
    elif isinstance(d, (list, tuple)):
        for v in d:
            yield from _numeros(v)
    elif isinstance(d, (int, float)) and not isinstance(d, bool):
        yield d


def _textos(d):
    if isinstance(d, dict):
        for k, v in d.items():
            yield str(k)
            yield from _textos(v)
    elif isinstance(d, (list, tuple)):
        for v in d:
            yield from _textos(v)
    elif isinstance(d, str):
        yield d


def test_nenhum_nivel_absoluto_nem_data_no_dossie():
    ticks = _ticks()
    niveis = [p for _, p in ticks]
    d = _dossie(
        noticias=[{"categoria": "juros", "direcao": "baixa", "relevancia": 0.8, "intensidade": 0.5,
                   "horizonte_min": 30, "t_disponivel_ns": T0 - 120 * S, "titulo": "Copom corta juros em 04/11/2026"}],
        posicao={"lado": "compra", "quantidade": 1, "preco_medio": BASE + 10, "aberta_ns": T0 - 90 * S,
                 "stop_pontos": 100.0, "alvo_pontos": 150.0},
        memoria=[{"acao": "COMPRAR", "resultado_pontos": 35.0, "duracao_s": 240, "motivo_saida": "alvo"}],
        modelos={"lgbm": {"status": "nao_treinado", "saida": {"p_alta": 0.55}}},
        statarb={"z_win_wdo": 1.2}, features={"desbalanco_book": 0.3})
    assert verificar_anonimizacao(d, niveis) == []
    for x in _numeros(d):
        assert abs(x) < 50_000
        assert all(abs(x - n) > 1 for n in niveis)
    texto = canonico(d)
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|20\d{2}", texto)
    assert "WDO" not in texto and "WIN" not in texto and "Copom" not in texto and "04/11" not in texto
    assert "titulo" not in texto


def test_noticia_entra_so_como_codigo_de_categoria():
    d = _dossie(noticias=[{"categoria": "cambio", "direcao": "alta", "relevancia": 0.7, "intensidade": 0.4,
                           "horizonte_min": 15, "t_disponivel_ns": T0 - 300 * S, "titulo": "Dolar sobe"},
                          {"categoria": "algo_novo", "t_disponivel_ns": T0 - 60 * S}])
    assert d["nlp"][0]["categoria"] == "C02" and d["nlp"][0]["idade_min"] == 5
    assert d["nlp"][1]["categoria"] == "C99"
    assert "Dolar" not in canonico(d)


def test_noticia_ainda_indisponivel_em_t0_nao_entra():
    d = _dossie(noticias=[{"categoria": "juros", "t_disponivel_ns": T0 + 10 * S}])
    assert d["nlp"] == []


def test_ticks_do_futuro_nao_vazam():
    ticks = _ticks() + [(T0 + 5 * S, BASE + 9_999.0)]
    d1 = _dossie(ticks=ticks)
    d2 = _dossie(ticks=_ticks())
    assert d1 == d2
    with pytest.raises(ErroDossie):
        montar_dossie(t0_ns=T0, ticks=[(T0 + S, BASE)], tick_pontos=WIN.tick_pontos)


def test_pontos_sao_relativos_ao_ultimo_preco():
    d = _dossie()
    ult = _ticks()[-1][1]
    ref60 = [p for t, p in _ticks() if t <= T0 - 60 * S][-1]
    assert d["movimento"]["ret_60s_pts"] == pytest.approx(ult - ref60, abs=0.01)
    assert d["microestrutura"]["spread_ticks"] == 1.0
    assert d["movimento"]["vol_norm"] is not None


def test_deslocar_todos_os_precos_nao_muda_o_dossie():
    d1 = _dossie()
    d2 = _dossie(ticks=[(t, p + 20_000) for t, p in _ticks()])
    assert d1 == d2 and hash_dossie(d1) == hash_dossie(d2)  # so a forma importa, nunca o nivel


def test_hash_estavel_e_sensivel():
    assert hash_dossie(_dossie()) == hash_dossie(_dossie())
    assert hash_dossie(_dossie()) != hash_dossie(_dossie(regime="lateral"))
    assert len(hash_dossie(_dossie())) == 64


def test_feature_fora_da_lista_ou_indicador_e_recusada():
    with pytest.raises(ErroDossie, match="lista permitida"):
        _dossie(features={"preco_ultimo": 131000.0})
    with pytest.raises(ErroDossie, match="indicador"):
        _dossie(features={"rsi_14": 55.0}, features_permitidas={"rsi_14"})


def test_verificador_detecta_violacoes():
    assert verificar_anonimizacao({"a": "2026-10-27"})
    assert verificar_anonimizacao({"a": "contrato WDOX26"})
    assert verificar_anonimizacao({"a": "quarta-feira passada", "b": "quarta"})
    assert verificar_anonimizacao({"data": "x"})
    assert verificar_anonimizacao({"x": 131245.0})
    assert verificar_anonimizacao({"x": 1_700_000_000_000_000_000})
    assert verificar_anonimizacao({"x": 131_100.5}, niveis_absolutos=[131_100.5])
    assert verificar_anonimizacao({"x": 4_750.0}, niveis_absolutos=[4_750.2]) != []
    assert verificar_anonimizacao({"movimento": {"ret_60s_pts": -35.0}, "regime": "lateral"}) == []


def test_caminhos_validos_para_evidencias():
    c = caminhos_validos(_dossie(memoria=[{"acao": "VENDER", "resultado_pontos": -10.0, "duracao_s": 30, "motivo_saida": "stop"}]))
    assert "movimento.ret_60s_pts" in c and "movimento" in c and "memoria.0.acao" in c
    json.dumps(_dossie())  # serializavel
