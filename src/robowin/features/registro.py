"""Registro PERMITIDO de features. Toda feature precisa estar aqui; o resto e recusado.

Regra do projeto: nada de indicadores tecnicos tradicionais. Apenas microestrutura e estatistica
causal. Cada entrada declara a janela e a garantia de causalidade (usa so dados com t_evento <= t).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Termos que NUNCA podem aparecer no codigo de src/robowin (o teste estatico varre o repositorio).
# Esta e uma lista de proibicao: as linhas que a declaram ficam isentas.
TERMOS_PROIBIDOS = (  # lista-de-proibicao
    "rsi", "macd", "sma", "ema", "media_movel", "bollinger", "cruzamento",  # lista-de-proibicao
)


class FeatureProibida(ValueError):
    """Feature fora do registro permitido."""


@dataclass(frozen=True)
class DefFeature:
    nome: str
    descricao: str
    janela: str  # ex.: "30s", "instantanea", "dia"
    causalidade: str  # o que a feature consome (sempre dados com t_evento <= t)
    grupo: str


_C_TICKS = "ticks com t_evento <= t"
_C_LIVRO = "snapshots do livro com t_evento <= t"
_C_RELOGIO = "relogio do proprio instante t"


def _def(nome: str, grupo: str, janela: str, desc: str, causal: str) -> DefFeature:
    return DefFeature(nome=nome, descricao=desc, janela=janela, causalidade=causal, grupo=grupo)


def _construir() -> dict[str, DefFeature]:
    d: list[DefFeature] = []
    for w in ("5s", "30s", "120s"):
        d.append(_def(f"fluxo_imb_{w}", "fluxo", w,
                      "Desequilibrio do fluxo agressor (compra-venda)/(compra+venda) por flags BUY/SELL", _C_TICKS))
    d += [
        _def("fluxo_intensidade_30s", "fluxo", "30s", "Negocios por segundo", _C_TICKS),
        _def("fluxo_volume_30s", "fluxo", "30s", "Volume negociado", _C_TICKS),
        _def("livro_imb_1", "livro", "instantanea", "Desequilibrio do melhor nivel do livro L2", _C_LIVRO),
        _def("livro_imb_5", "livro", "instantanea", "Desequilibrio dos 5 melhores niveis do livro L2", _C_LIVRO),
        _def("microprice_desvio_pts", "livro", "instantanea", "Microprice menos ponto medio, em pontos", _C_LIVRO),
        _def("livro_spread_pts", "livro", "instantanea", "Spread do livro em pontos", _C_LIVRO),
        _def("spread_pts", "livro", "instantanea", "Spread da ultima cotacao dos ticks, em pontos", _C_TICKS),
        _def("ofi_5s", "livro", "5s", "Order flow imbalance de Cont-Kukanov-Stoikov no topo do livro", _C_LIVRO),
        _def("ofi_30s", "livro", "30s", "Order flow imbalance de Cont-Kukanov-Stoikov no topo do livro", _C_LIVRO),
        _def("ofi_30s_norm", "livro", "30s", "OFI de 30s dividido pela profundidade do topo no periodo", _C_LIVRO),
    ]
    for w in ("5s", "30s", "120s", "600s"):
        d.append(_def(f"ret_{w}", "retornos", w, "Variacao do ponto medio na janela, em pontos", _C_TICKS))
    for w in ("1m", "5m", "30m"):
        d.append(_def(f"rv_{w}", "volatilidade", w,
                      "Volatilidade realizada (raiz da soma de retornos logaritmicos quadraticos, grade de 5s)",
                      _C_TICKS))
    d += [
        _def("rv_razao_1m_30m", "volatilidade", "30m", "Razao entre a volatilidade realizada curta e a longa", _C_TICKS),
        _def("har_rv_dia", "volatilidade", "dia", "Previsao HAR-RV (Corsi) para o dia, fixada antes da abertura",
             "RV de pregoes anteriores"),
        _def("ctx_min_desde_abertura", "contexto", "dia", "Minutos desde a abertura (codigo nao identificavel)", _C_RELOGIO),
        _def("ctx_sazonal_sin", "contexto", "dia", "Seno do ciclo intradiario", _C_RELOGIO),
        _def("ctx_sazonal_cos", "contexto", "dia", "Cosseno do ciclo intradiario", _C_RELOGIO),
        _def("ctx_bucket_30m", "contexto", "dia", "Indice do bloco de 30 minutos do pregao", _C_RELOGIO),
    ]
    return {x.nome: x for x in d}


REGISTRO_PERMITIDO: dict[str, DefFeature] = _construir()


def eh_permitida(nome: str) -> bool:
    return nome in REGISTRO_PERMITIDO


def exigir_permitida(nome: str) -> DefFeature:
    """Devolve a definicao ou levanta FeatureProibida."""
    try:
        return REGISTRO_PERMITIDO[nome]
    except KeyError:
        raise FeatureProibida(f"feature fora do registro permitido: {nome!r}") from None


def validar_nomes(nomes: Iterable[str]) -> None:
    for n in nomes:
        exigir_permitida(n)


def validar_vetor(valores: Mapping[str, float]) -> None:
    """Recusa vetor com qualquer chave fora do registro."""
    validar_nomes(valores.keys())


def nomes_permitidos() -> tuple[str, ...]:
    return tuple(REGISTRO_PERMITIDO)
