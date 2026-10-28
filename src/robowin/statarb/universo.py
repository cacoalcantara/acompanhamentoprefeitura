"""Universo configuravel de simbolos, validado contra `symbols_get()` do terminal."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


def padrao(alvo: str | None = None) -> tuple[str, ...]:
    """Universo candidato: todos os ativos de config/instrumentos.toml mais o indice de referencia."""
    from robowin.core.instrumentos import ativos_disponiveis
    return (*(a for a in ativos_disponiveis() if a != alvo), "IBOV")


def _nome(s: Any) -> str:
    return s if isinstance(s, str) else str(getattr(s, "name", ""))


@dataclass(frozen=True)
class Universo:
    """Simbolos pedidos, validos (existentes no terminal) e descartados (com aviso)."""
    pedidos: tuple[str, ...]
    validos: tuple[str, ...] = ()
    descartados: tuple[str, ...] = ()
    avisos: tuple[str, ...] = field(default=())

    @classmethod
    def criar(cls, simbolos: Iterable[str] | None = None, acoes: Iterable[str] = (),
              alvo: str | None = None) -> Universo:
        vistos: dict[str, None] = {}
        for s in (*(padrao(alvo) if simbolos is None else simbolos), *acoes):
            s = s.strip().upper()
            if s:
                vistos.setdefault(s, None)
        return cls(pedidos=tuple(vistos))

    def validar(self, symbols_get: Sequence[Any] | None) -> Universo:
        """Valida contra a lista do terminal (objetos com `.name` ou strings). Nunca inventa simbolos."""
        disponiveis = {_nome(s).upper() for s in (symbols_get or ())}
        validos = tuple(s for s in self.pedidos if s in disponiveis)
        descartados = tuple(s for s in self.pedidos if s not in disponiveis)
        avisos = tuple(f"simbolo ausente no terminal, descartado: {s}" for s in descartados)
        for a in avisos:
            log.warning(a)
        return Universo(self.pedidos, validos, descartados, avisos)

    def __contains__(self, simbolo: object) -> bool:
        return simbolo in self.validos
