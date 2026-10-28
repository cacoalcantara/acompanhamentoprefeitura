"""Provedores de LLM. Adaptadores sao importados sob demanda (SDKs opcionais)."""
from __future__ import annotations

import importlib
from typing import Any

from robowin.ia.provedores.base import ErroProvedor, PacoteAusente, ProvedorBase

_CLASSES = {
    "anthropic": ("robowin.ia.provedores.anthropic", "ProvedorAnthropic"),
    "anthropic_bedrock": ("robowin.ia.provedores.anthropic", "ProvedorAnthropicBedrock"),
    "anthropic_vertex": ("robowin.ia.provedores.anthropic", "ProvedorAnthropicVertex"),
    "openai": ("robowin.ia.provedores.openai", "ProvedorOpenAI"),
    "azure_openai": ("robowin.ia.provedores.openai", "ProvedorAzureOpenAI"),
    "google": ("robowin.ia.provedores.google", "ProvedorGoogle"),
    "gemini_vertex": ("robowin.ia.provedores.google", "ProvedorGeminiVertex"),
    "openai_compativel": ("robowin.ia.provedores.openai_compativel", "ProvedorOpenAICompativel"),
    "ollama": ("robowin.ia.provedores.ollama", "ProvedorOllama"),
    "falso": ("robowin.ia.provedores.falso", "ProvedorFalso"),
}

PROVEDORES = tuple(_CLASSES)


def criar_provedor(adaptador: str, /, **kw: Any) -> ProvedorBase:
    """Instancia o ADAPTADOR ``nome`` (anthropic, openai_compativel...). Para um provedor do registro
    (deepseek, groq, lmstudio...), use ``criar_provedor_registrado``."""
    if adaptador not in _CLASSES:
        raise ErroProvedor(f"provedor desconhecido: {adaptador} (validos: {', '.join(_CLASSES)})")
    modulo, classe = _CLASSES[adaptador]
    return getattr(importlib.import_module(modulo), classe)(**kw)


def criar_provedor_registrado(nome: str, /, *, registro: Any = None, **kw: Any) -> ProvedorBase:
    """Provedor pelo NOME do registro (config/provedores.toml): adaptador, base_url e chave do ambiente.
    ``falso`` continua disponivel. Nao abre conexao."""
    if nome == "falso":
        return criar_provedor("falso", **kw)
    from robowin.ia.registro_provedores import registro_padrao
    reg = registro if registro is not None else registro_padrao()
    return reg.criar(nome, **kw)


__all__ = ["PROVEDORES", "ErroProvedor", "PacoteAusente", "ProvedorBase", "criar_provedor",
           "criar_provedor_registrado"]
