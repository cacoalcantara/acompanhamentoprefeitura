"""Redacao de segredos: chaves de API nunca vao para logs, erros ou auditoria."""
from __future__ import annotations

import os
import re

_PADROES = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|x-api-key|authorization)(\s*[=:]\s*)\S+"),
]
_VARIAVEIS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "OPENAI_COMPATIVEL_API_KEY", "OLLAMA_API_KEY",
)
_registrados: set[str] = set()


def registrar_segredo(valor: str | None) -> None:
    if valor and len(valor) >= 6:
        _registrados.add(valor)


def redigir(texto: str) -> str:
    """Substitui qualquer segredo conhecido (registrado, em variavel de ambiente ou por padrao) por ***."""
    segredos = set(_registrados)
    for v in _VARIAVEIS:
        val = os.environ.get(v)
        if val and len(val) >= 6:
            segredos.add(val)
    for s in sorted(segredos, key=len, reverse=True):
        texto = texto.replace(s, "***")
    for p in _PADROES:
        if p.groups >= 2:
            texto = p.sub(lambda m: f"{m.group(1)}{m.group(2)}***", texto)
        else:
            texto = p.sub("***", texto)
    return texto
