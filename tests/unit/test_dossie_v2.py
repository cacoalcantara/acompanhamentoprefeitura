"""Dossie v2 do planejador (docs/16 WP4): secoes extras, sem limites nem dinheiro, vol em pontos, sem features de
unidade log/relativa (C30), `_perto` restrito por caminho (C29) e o v1 identico ao de antes."""
from __future__ import annotations

import math

import numpy as np
import pytest

from robowin.core.tipos import ConfigRisco
from robowin.features.registro import REGISTRO_PERMITIDO, nomes_permitidos
from robowin.ia.dossie import (
    CHAVES_LIMITE_V2,
    FEATURES_PERMITIDAS_PADRAO,
    SECOES_V2,
    UNIDADES_PROIBIDAS_V2,
    ErroDossie,
    chaves_proibidas_v2,
    hash_dossie,
    montar_dossie,
    unidade_feature,
    verificar_anonimizacao,
)

S = 1_000_000_000
T0 = 5_000 * S
# FEATURES_DOSSIE do orquestrador (v1) menos o retorno logaritmico: o FEATURES_DOSSIE_V2 esperado (C30)
FEATURES_V1 = ("fluxo_imb_5s", "fluxo_imb_30s", "fluxo_imb_120s", "fluxo_intensidade_30s", "livro_imb_1",
               "livro_imb_5", "microprice_desvio_pts", "ofi_30s_norm", "ret_5s", "ret_30s", "ret_120s", "rv_1m",
               "rv_razao_1m_30m", "ctx_bucket_30m")
FEATURES_V2 = tuple(f for f in FEATURES_V1 if f != "rv_1m")


def _ticks(base, tick, n=600):
    return [(T0 - (n - 1 - i) * S, base + tick * ((i * 7) % 5) + tick * (i // 50)) for i in range(n)]


def _v2(base=131_245.0, tick=5.0, extras=None, **kw):
    args = dict(t0_ns=T0, ticks=_ticks(base, tick), spread_pontos=tick, tick_pontos=tick, regime="normal",
                minutos_desde_abertura=60, minutos_para_zeragem=200, versao=2, extras=extras or {})
    args.update(kw)
    return montar_dossie(**args)


# ---------------------------------------------------------------------------------------------------- v1 intacto
def test_v1_identico_ao_de_antes_hash_dourado():
    """Hash capturado com o codigo anterior ao WP4 (mesma entrada, sem features)."""
    ticks = [(T0 - (999 - i) * S, 131_245.0 + 5.0 * ((i * 7) % 13) + i // 20 * 5.0) for i in range(1000)]
    d = montar_dossie(
        t0_ns=T0, ticks=ticks, spread_pontos=5.0, tick_pontos=5.0, regime="tendencia",
        limites={"contratos_restantes": 1, "perda_restante_dia_brl": 180.0}, minutos_desde_abertura=90,
        minutos_para_zeragem=300,
        posicao={"lado": "compra", "quantidade": 1, "preco_medio": 131_300.0, "aberta_ns": T0 - 90 * S,
                 "stop_pontos": 100.0, "alvo_pontos": 150.0},
        memoria=[{"acao": "COMPRAR", "resultado_pontos": 35.0, "duracao_s": 240, "motivo_saida": "alvo"}],
        statarb={"z": 1.2}, modelos={"m": {"status": "nao_treinado", "saida": {"p": 0.55}}})
    assert hash_dossie(d) == "f211a0f01cafe58d1477c4f5a18a9b924823611f4d5e589e1274fe8feecbc462"
    assert d["meta"]["versao_dossie"] == 1 and "limites" in d and "vol_pts_60s" not in d["movimento"]


def test_v1_recusa_extras_e_versao_desconhecida():
    with pytest.raises(ErroDossie):
        montar_dossie(t0_ns=T0, ticks=_ticks(100.0, 1.0), tick_pontos=1.0, extras={"custos": {}})
    with pytest.raises(ErroDossie):
        _v2(versao=3)


def test_padrao_de_features_e_o_registro():
    assert FEATURES_PERMITIDAS_PADRAO == frozenset(nomes_permitidos())
    with pytest.raises(ErroDossie):
        montar_dossie(t0_ns=T0, ticks=_ticks(100.0, 1.0), tick_pontos=1.0, features={"desbalanco_book": 0.1})


# ---------------------------------------------------------------------------------------------------- estrutura
def test_v2_secoes_vol_em_pontos_e_sem_limites():
    extras = {"instrumento": {"tick_pontos": 5.0, "valor_ponto_conta": 1.0, "stop_dispara_por": "ultimo"},
              "custos": {"ida_volta_pontos_base": 3.0, "formula_equilibrio": "(stop + custo) / (stop + alvo)"},
              "envelope": {"unidades_max": 1, "stop_max_pontos_por_q": [250.0], "tempo_max_s": 300,
                           "entradas_possiveis": True, "motivos_sem_entrada": []},
              "sessao": {"min_para_fim_entradas": 150, "min_para_evento": None},
              "mercado": {"feed_ok": True, "leilao": False, "vol_extrema": False},
              "plano_vigente": None, "retorno": {"ultima": "vetada:stop_acima_do_envelope"},
              "latencia_ia": {"p50_s": 8.0, "p95_s": 14.5}}
    d = _v2(extras=extras)
    assert d["meta"]["versao_dossie"] == 2
    assert "limites" not in d
    assert set(SECOES_V2) - {"posicao"} <= set(d)
    assert d["sessao"] == {"min_desde_abertura": 60, "min_para_zeragem": 200, "min_para_fim_entradas": 150,
                           "min_para_evento": None}  # sessao ampliada (mesclada)
    precos = np.array([p for t, p in _ticks(131_245.0, 5.0) if t >= T0 - 60 * S])
    assert d["movimento"]["vol_pts_60s"] == pytest.approx(float(np.std(np.diff(precos))), abs=1e-4)
    assert d["movimento"]["vol_pts_300s"] is not None
    assert d["posicao"] == {"lado": None, "quantidade": 0}


def test_v2_posicao_extra_substitui_a_basica():
    pos = {"lado": "compra", "quantidade": 2, "quantidade_inicial": 3, "resultado_pontos": 15.0,
           "stop_resultado_pontos": 0.0, "alvo_resultado_pontos": 60.0, "tempo_aberta_s": 90, "regras": []}
    d = _v2(extras={"posicao": pos},
            posicao={"lado": "compra", "quantidade": 2, "preco_medio": 131_250.0, "aberta_ns": T0 - 90 * S})
    assert d["posicao"] == pos


def test_v2_nao_finito_vira_null_e_secao_desconhecida_recusada():
    d = _v2(extras={"latencia_ia": {"p50_s": math.nan, "p95_s": math.inf}})
    assert d["latencia_ia"] == {"p50_s": None, "p95_s": None}
    with pytest.raises(ErroDossie, match="secao extra desconhecida"):
        _v2(extras={"limites_usuario": {}})
    with pytest.raises(ErroDossie, match="nao serializavel"):
        _v2(extras={"custos": {"x": object()}})


def test_v2_retorno_so_codigos():
    assert _v2(extras={"retorno": {"ultima": "executada", "ok": True}})
    with pytest.raises(ErroDossie, match="retorno"):
        _v2(extras={"retorno": {"ultima": "executada", "liquido_pontos": 12.0}})


# ---------------------------------------------------------------------------------------------------- C30
def test_v2_sem_rv_1m_nem_feature_log_ou_relativa():
    assert unidade_feature("rv_1m") == "log" and unidade_feature("har_rv_dia") == "log"
    with pytest.raises(ErroDossie, match="unidade log"):
        _v2(features={"rv_1m": 0.001}, features_permitidas=nomes_permitidos())
    with pytest.raises(ErroDossie):
        _v2(features={"rv_30m": 0.002})
    d = _v2(features={f: 0.1 for f in FEATURES_V2})
    assert "rv_1m" not in d["features"] and set(d["features"]) == set(FEATURES_V2)
    for f in FEATURES_V2:
        assert unidade_feature(f) not in UNIDADES_PROIBIDAS_V2


def test_unidade_de_todas_as_features_do_registro():
    for nome in REGISTRO_PERMITIDO:
        u = unidade_feature(nome)
        assert u in ("pontos", "log", "relativa", "adimensional"), nome
        if "logaritm" in REGISTRO_PERMITIDO[nome].descricao.lower():
            assert u == "log"
    assert unidade_feature("nao_existe") == "desconhecida"


# ---------------------------------------------------------------------------------------------------- limites
def test_v2_recusa_limites_e_qualquer_chave_de_dinheiro():
    with pytest.raises(ErroDossie, match="limites"):
        _v2(limites={"perda_restante_dia_brl": 100.0})
    for chave in ("perda_max_dia_brl", "tempo_max_posicao_s", "contratos_max", "stop_max_pontos", "saldo_brl",
                  "usuario", "hash"):
        with pytest.raises(ErroDossie, match="limite/dinheiro"):
            _v2(extras={"envelope": {chave: 1}})


def test_chaves_de_limite_cobrem_todo_config_risco():
    """Nenhum nome de campo de ConfigRisco pode aparecer no dossie do planejador (lista mantida no ia/dossie, que
    nao pode importar o ConfigRisco)."""
    assert set(ConfigRisco.model_fields) <= CHAVES_LIMITE_V2


def test_v2_completo_nao_tem_chave_proibida():
    from robowin.core.instrumentos import carregar_perfil
    from robowin.ia.regressao import bateria_planejador
    for nome in ("WIN", "EURUSD", "PETR4"):
        for caso in bateria_planejador(carregar_perfil(nome, permitir_a_confirmar=True)):
            assert chaves_proibidas_v2(caso.dossie) == [], caso.id
            assert caso.dossie["meta"]["versao_dossie"] == 2


# ---------------------------------------------------------------------------------------------------- C29
def test_perto_isenta_dinheiro_e_segundos_com_ativo_barato():
    # ativo perto de 200: 200 na moeda da conta (P&L realizado) e tempo de 200 s nao sao "nivel absoluto"
    d = _v2(base=200.0, tick=0.01, extras={
        "posicao": {"lado": "compra", "quantidade": 1, "resultado_pontos": 0.03, "liquido_parcial_conta": 200.0,
                    "tempo_aberta_s": 200, "tempo_max_min": 200},
        "custos": {"ida_volta_pontos_base": 200.0}})
    assert d["posicao"]["liquido_parcial_conta"] == 200.0
    # o v1 (verificador padrao) continua acusando
    assert verificar_anonimizacao({"posicao": {"tempo_aberta_s": 200}}, [200.0])


def test_perto_isenta_tempo_de_5000_s_com_ativo_perto_de_5000():
    d = _v2(base=5_000.0, tick=0.25, extras={"posicao": {"lado": "venda", "quantidade": 1, "tempo_aberta_s": 5000,
                                                         "resultado_pontos": 1.25},
                                             "envelope": {"tempo_max_s": 5000}})
    assert d["posicao"]["tempo_aberta_s"] == 5000


@pytest.mark.parametrize("extras", [
    {"posicao": {"lado": "compra", "quantidade": 1, "nivel_pontos": 200.0}},
    {"plano_vigente": {"gatilho_pontos": 200.004}},
])
def test_perto_continua_valendo_nas_secoes_de_preco(extras):
    with pytest.raises(ErroDossie, match="nivel absoluto"):
        _v2(base=200.0, tick=0.01, extras=extras)


def test_v2_anonimizacao_de_texto_e_timestamp():
    with pytest.raises(ErroDossie):
        _v2(extras={"retorno": {"ultima": "WINZ26"}})
    with pytest.raises(ErroDossie):
        _v2(extras={"latencia_ia": {"desde": T0}})
    with pytest.raises(ErroDossie):
        _v2(extras={"mercado": {"bid": 1.0}})


def test_v2_ignora_o_verificador_rapido_do_backtest():
    from robowin.backtest.anonimizacao import anonimizacao_rapida
    with anonimizacao_rapida():
        d = _v2(base=200.0, tick=0.01, extras={"posicao": {"lado": "compra", "quantidade": 1, "tempo_aberta_s": 200}})
    assert d["meta"]["versao_dossie"] == 2


def test_niveis_extras_absolutos_entram_na_checagem():
    with pytest.raises(ErroDossie):
        _v2(statarb={"spread_aux_pontos": 4321.0}, niveis_extras_absolutos=[4321.2])
