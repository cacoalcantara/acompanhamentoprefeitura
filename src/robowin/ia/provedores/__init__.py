"""Provedores de LLM. Adaptadores sao importados sob demanda (SDKs opcionais)."""
from __future__ import annotations

import importlib
from typing import Any

from robowin.ia.provedores.base import ErroProvedor, PacoteAusente, ProvedorBase

_CLASSES = {
    "anthropic": ("robowin.ia.provedores.anthropic", "ProvedorAnthropic"),
    "openai": ("robowin.ia.provedores.openai", "ProvedorOpenAI"),
    "google": ("robowin.ia.provedores.google", "ProvedorGoogle"),
    "openai_compativel": ("robowin.ia.provedores.openai_compativel", "ProvedorOpenAICompativel"),
    "ollama": ("robowin.ia.provedores.ollama", "ProvedorOllama"),
    "falso": ("robowin.ia.provedores.falso", "ProvedorFalso"),
}

PROVEDORES = tuple(_CLASSES)


def criar_provedor(nome: str, **kw: Any) -> ProvedorBase:
    if nome not in _CLASSES:
        raise ErroProvedor(f"provedor desconhecido: {nome} (validos: {', '.join(_CLASSES)})")
    modulo, classe = _CLASSES[nome]
    return getattr(importlib.import_module(modulo), classe)(**kw)


__all__ = ["PROVEDORES", "ErroProvedor", "PacoteAusente", "ProvedorBase", "criar_provedor"]
