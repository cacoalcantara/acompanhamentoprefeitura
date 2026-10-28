"""Fixtures globais dos testes.

Nenhum teste depende de chave de API: antes de CADA teste, todas as variaveis ``*_API_KEY`` e as demais
credenciais/enderecos de provedores de IA (as do registro config/provedores.toml e as que os SDKs leem sozinhos)
sao removidas do ambiente. Um teste que precise simular "chave presente" usa ``monkeypatch.setenv`` com um valor
falso; nenhum teste fala com a rede.
"""
from __future__ import annotations

import os
import re

import pytest

_RE_CHAVE = re.compile(r".*_API_KEY$")
# credenciais/enderecos que os SDKs leem do ambiente por conta propria (desviariam o teste para a rede real)
_EXTRAS = (
    "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
    "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_VERTEX_ACCESS_TOKEN", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION",
    "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "GOOGLE_GENAI_USE_VERTEXAI", "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_AD_TOKEN", "OPENAI_API_VERSION", "OPENAI_COMPATIVEL_BASE_URL", "OLLAMA_HOST", "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
)


_do_registro: set[str] | None = None


def _variaveis_do_registro() -> set[str]:
    global _do_registro
    if _do_registro is None:
        _do_registro = set()
        try:
            from robowin.ia.registro_provedores import RegistroProvedores
            for d in RegistroProvedores.carregar():
                _do_registro.update(d.chave_env, d.exige_env)
                if d.base_url_env:
                    _do_registro.add(d.base_url_env)
        except Exception:  # registro invalido: o teste do registro acusa; aqui so limpamos o que der
            pass
    return _do_registro


def variaveis_de_credencial() -> set[str]:
    return {v for v in os.environ if _RE_CHAVE.match(v)} | set(_EXTRAS) | _variaveis_do_registro()


@pytest.fixture(autouse=True)
def _sem_chaves_de_api(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in variaveis_de_credencial():
        monkeypatch.delenv(v, raising=False)
