"""Verificador de anonimizacao do dossie em O((folhas + niveis) log niveis), semanticamente IDENTICO a
`robowin.ia.dossie.verificar_anonimizacao` (que e O(folhas x niveis) e inviavel no backtest com ticks reais).

A equivalencia e garantida por teste (tests/unit/test_backtest_vazamento.py). `anonimizacao_rapida()` troca,
somente durante a montagem do dossie, a funcao usada por `montar_dossie`; o modulo `ia.dossie` nao e alterado.
"""
from __future__ import annotations

import bisect
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from robowin.ia import dossie as _d


def verificar_anonimizacao_rapida(d: dict[str, Any], niveis_absolutos: Iterable[float] = ()) -> list[str]:
    erros: list[str] = []
    for k in _d._chaves(d):
        if k.lower() in _d._CHAVES_PROIBIDAS:
            erros.append(f"chave proibida: {k}")
    niveis = sorted({float(n) for n in niveis_absolutos if abs(float(n)) > 100})
    for caminho, v in _d._percorrer(d):
        if isinstance(v, str):
            if _d._RE_STRING_PROIBIDA.search(v):
                erros.append(f"texto identifica periodo/ativo em {caminho}: {v[:40]!r}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            if abs(v) >= _d._LIMITE_TIMESTAMP:
                erros.append(f"valor parece timestamp em {caminho}")
            elif abs(v) >= _d._LIMITE_NIVEL_ABS:
                erros.append(f"valor parece nivel absoluto em {caminho}")
            elif niveis and _tem_proximo(niveis, float(v)):
                erros.append(f"valor coincide com nivel absoluto em {caminho}")
    return erros


def _tem_proximo(ordenados: list[float], v: float) -> bool:
    """Existe n em `ordenados` com |v - n| < 0.5 ? (comparacao estrita, como a original)"""
    i = bisect.bisect_left(ordenados, v)
    for j in (i - 1, i):
        if 0 <= j < len(ordenados) and abs(v - ordenados[j]) < 0.5:
            return True
    return False


@contextmanager
def anonimizacao_rapida() -> Iterator[None]:
    original = _d.verificar_anonimizacao
    _d.verificar_anonimizacao = verificar_anonimizacao_rapida  # type: ignore[assignment]
    try:
        yield
    finally:
        _d.verificar_anonimizacao = original  # type: ignore[assignment]
