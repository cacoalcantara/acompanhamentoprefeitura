"""Registro permitido de features e teste estatico: nada de indicadores tecnicos tradicionais."""
import pathlib
import re

import pytest

from robowin.features.pipeline import PipelineFeatures
from robowin.features.registro import (
    REGISTRO_PERMITIDO,
    TERMOS_PROIBIDOS,
    FeatureProibida,
    exigir_permitida,
    validar_vetor,
)

RAIZ = pathlib.Path(__file__).resolve().parents[2] / "src" / "robowin"


def test_registro_tem_metadados_completos():
    assert len(REGISTRO_PERMITIDO) >= 25
    for nome, d in REGISTRO_PERMITIDO.items():
        assert d.nome == nome and d.descricao and d.janela and d.causalidade and d.grupo


@pytest.mark.parametrize("nome", ["rsi_14", "macd", "sma_20", "ema_9", "media_movel_50", "bollinger_sup",
                                  "cruzamento_medias", "feature_qualquer"])
def test_recusa_feature_fora_da_lista(nome):
    with pytest.raises(FeatureProibida):
        exigir_permitida(nome)
    with pytest.raises(FeatureProibida):
        validar_vetor({"fluxo_imb_5s": 0.1, nome: 1.0})


def test_pipeline_so_emite_features_permitidas():
    v = PipelineFeatures().vetor(1_773_147_600 * 1_000_000_000)
    assert set(v.valores) <= set(REGISTRO_PERMITIDO)
    assert set(v.valores) == set(REGISTRO_PERMITIDO)  # e emite todas


def test_estatico_sem_indicadores_tecnicos_no_codigo():
    """Falha se 'rsi','macd','sma','ema','media_movel','bollinger','cruzamento' aparecer em src/robowin.

    Isentas apenas as linhas de listas de proibicao (marcadas 'lista-de-proibicao' ou contendo 'proib').
    """
    termos = "|".join(re.escape(t) for t in TERMOS_PROIBIDOS)
    rx = re.compile(rf"(?<![a-z0-9])({termos})(?![a-z0-9])")
    achados = []
    for f in sorted(RAIZ.rglob("*.py")):
        for i, linha in enumerate(f.read_text(encoding="utf-8").lower().splitlines(), 1):
            if "proib" in linha:
                continue
            if rx.search(linha):
                achados.append(f"{f.relative_to(RAIZ)}:{i}: {linha.strip()}")
    assert not achados, "indicador tecnico proibido encontrado:\n" + "\n".join(achados)


def test_o_proprio_detector_funciona():
    rx = re.compile(r"(?<![a-z0-9])(rsi|macd|sma|ema|media_movel|bollinger|cruzamento)(?![a-z0-9])")
    assert rx.search("def calcular_rsi(x):") and rx.search("ema_20 = 1") and rx.search("media_movel")
    assert not rx.search("sistema schema tema semana")
