from __future__ import annotations

import pytest

from robowin.core.tipos import ChamadaLLM
from robowin.ia.catalogo import Catalogo, ErroCatalogo, dividir_modelo
from robowin.ia.papeis import carregar_papeis, orcamento_mensal_usd, resolver, validar_papeis

BASE = {"provedor": "anthropic", "id": "x", "fixavel": True, "corte_conhecimento": "2026-06",
        "preco_entrada_mtok": 4.0, "preco_saida_mtok": 20.0, "preco_cache_mtok": 0.2}


def test_carrega_catalogo_real():
    cat = Catalogo.carregar("config/modelos.toml")
    e = cat.obter("anthropic:claude-opus-5-5")
    assert e.preco_saida_mtok == 20.0 and e.fixavel
    assert not cat.obter("openai:gpt-6.1-sol").fixavel


@pytest.mark.parametrize("campo", ["preco_entrada_mtok", "preco_saida_mtok", "corte_conhecimento"])
def test_recusa_modelo_sem_preco_ou_corte(campo):
    ruim = {k: v for k, v in BASE.items() if k != campo}
    with pytest.raises(ErroCatalogo, match="recusado"):
        Catalogo.de_dict({"modelo": [ruim]})


def test_recusa_corte_invalido_preco_negativo_e_duplicado():
    with pytest.raises(ErroCatalogo):
        Catalogo.de_dict({"modelo": [{**BASE, "corte_conhecimento": "junho de 2026"}]})
    with pytest.raises(ErroCatalogo):
        Catalogo.de_dict({"modelo": [{**BASE, "preco_saida_mtok": -1.0}]})
    with pytest.raises(ErroCatalogo, match="duplicado"):
        Catalogo.de_dict({"modelo": [BASE, BASE]})


def test_modelo_fora_do_catalogo_e_recusado():
    cat = Catalogo.carregar("config/modelos.toml")
    with pytest.raises(ErroCatalogo, match="fora do catalogo"):
        cat.obter("anthropic:modelo-inexistente")
    with pytest.raises(ErroCatalogo):
        cat.obter("semprovedor")


def test_custo_da_chamada():
    cat = Catalogo.carregar("config/modelos.toml")
    ch = ChamadaLLM(papel="proponente", modelo_pedido="anthropic:claude-opus-5-5", tokens_entrada=1000,
                    tokens_cache=5000, tokens_saida=2000)
    esperado = (1000 * 4.0 + 5000 * 0.2 + 2000 * 20.0) / 1e6
    assert cat.custo(ch) == pytest.approx(esperado)
    assert cat.custo(ch) == pytest.approx(0.045)  # premissa do plano (~US$ 0,045 por chamada)


def test_fronteira_de_contaminacao_e_o_corte_mais_recente():
    cat = Catalogo.carregar("config/modelos.toml")
    assert cat.fronteira_contaminacao(["openai:gpt-6.1-sol"]) == "2026-04"
    assert cat.fronteira_contaminacao(["anthropic:claude-opus-5-5", "openai:gpt-6.1-sol"]) == "2026-06"
    cat2 = Catalogo.de_dict({"modelo": [{**BASE, "id": "a", "corte_conhecimento": "2025-12"},
                                        {**BASE, "id": "b", "corte_conhecimento": "2026-02-15"}]})
    assert cat2.fronteira_contaminacao(["anthropic:a", "anthropic:b"]) == "2026-02-15"
    with pytest.raises(ErroCatalogo):
        cat2.fronteira_contaminacao([])


def test_dividir_modelo_so_no_primeiro_dois_pontos():
    assert dividir_modelo("ollama:llama3:8b") == ("ollama", "llama3:8b")
    with pytest.raises(ErroCatalogo):
        dividir_modelo("ollama:")


def test_papeis_da_config_real():
    papeis = carregar_papeis("config/robowin.toml")
    assert resolver(papeis, "proponente") == ("anthropic", "claude-opus-5-5")
    assert resolver(papeis, "revisor") == ("openai", "gpt-6.1-sol")
    assert resolver(papeis, "classificador_nlp") == ("anthropic", "claude-haiku-5-5")
    assert papeis["proponente"].effort == "low"
    validar_papeis(papeis, Catalogo.carregar("config/modelos.toml"))
    with pytest.raises(ErroCatalogo):
        resolver(papeis, "analista_pre_mercado")


def test_trocar_modelo_so_na_config_e_validado_contra_o_catalogo():
    cfg = {"ia": {"orcamento_mensal_usd": 100, "papeis": {"proponente": {"modelo": "google:gemini-x"}}}}
    papeis = carregar_papeis(cfg)
    assert resolver(papeis, "proponente") == ("google", "gemini-x")
    with pytest.raises(ErroCatalogo):  # sem entrada no catalogo => sem preco/corte => falha segura
        validar_papeis(papeis, Catalogo.carregar("config/modelos.toml"))
    assert orcamento_mensal_usd(cfg) == 100
