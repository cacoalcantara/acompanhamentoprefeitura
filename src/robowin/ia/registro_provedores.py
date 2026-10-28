"""Registro de provedores (config/provedores.toml): NOME do provedor -> adaptador, base_url e variavel da chave.

Um provedor novo e so uma entrada no TOML (em geral ``adaptador = "openai_compativel"`` + ``base_url``). O nome e o
prefixo de ``provedor:modelo`` no catalogo, nos papeis e nos bracos; o adaptador e o codigo que fala com a API.

Chaves: so os NOMES das variaveis ficam no registro; o valor e lido do ambiente na hora de criar o cliente e nunca
e impresso (``chave_presente`` diz apenas presente/ausente). Criar o cliente nao faz chamada de rede.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robowin.ia.catalogo import carregar_toml_tolerante

ADAPTADORES = ("anthropic", "anthropic_bedrock", "anthropic_vertex", "openai", "azure_openai", "google",
               "gemini_vertex", "openai_compativel", "ollama", "falso")
SAIDAS_COMPATIVEL = ("json_schema", "json", "texto")
_RE_NOME = re.compile(r"^[a-z0-9][a-z0-9_\-.]*$")
CAMINHO_PADRAO = "config/provedores.toml"
_CREDENCIAL_ARQUIVO = "GOOGLE_APPLICATION_CREDENTIALS"


class ErroRegistro(Exception):
    pass


@dataclass(frozen=True)
class DefProvedor:
    nome: str
    adaptador: str
    base_url: str = ""
    base_url_env: str = ""
    chave_env: tuple[str, ...] = ()
    chave_obrigatoria: bool = True
    exige_env: tuple[str, ...] = ()
    local: bool = False
    saida_estruturada: str = ""
    envia_effort: bool = False
    regiao: str = ""
    observacoes: str = ""
    verificado: bool = False
    fonte_verificacao: str = ""
    parametros: Mapping[str, Any] = field(default_factory=dict)

    # --- ambiente (nunca devolve o VALOR de uma chave) --------------------------------------------
    def _chave(self, env: Mapping[str, str]) -> str | None:
        for v in self.chave_env:
            if env.get(v):
                return env[v]
        return None

    def variaveis_ausentes(self, env: Mapping[str, str] | None = None) -> list[str]:
        """Variaveis que faltam para o modo real (a chave conta como UMA: qualquer das de ``chave_env``)."""
        env = os.environ if env is None else env
        faltam = [v for v in self.exige_env if not env.get(v)]
        if self.chave_obrigatoria and self.chave_env and self._chave(env) is None:
            faltam.insert(0, self.chave_env[0] if len(self.chave_env) == 1 else " ou ".join(self.chave_env))
        return faltam

    def chave_presente(self, env: Mapping[str, str] | None = None) -> bool:
        """True se o modo real tem o que precisa (chave e variaveis exigidas). Local sem chave: True."""
        return not self.variaveis_ausentes(env)

    def estado_chave(self, env: Mapping[str, str] | None = None) -> str:
        env = os.environ if env is None else env
        if not self.chave_env:
            return "nao_exigida"
        if self._chave(env) is not None:
            return "presente"
        return "ausente" if self.chave_obrigatoria else "ausente_opcional"

    def url_efetiva(self, env: Mapping[str, str] | None = None) -> str:
        env = os.environ if env is None else env
        return (env.get(self.base_url_env) if self.base_url_env else None) or self.base_url

    def kwargs_adaptador(self, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Argumentos do construtor do adaptador (le o ambiente; nao abre conexao)."""
        env = os.environ if env is None else env
        kw: dict[str, Any] = {"nome": self.nome}
        url = self.url_efetiva(env)
        chave = self._chave(env)
        if self.adaptador in ("anthropic", "openai", "openai_compativel", "anthropic_bedrock"):
            if url:
                kw["base_url"] = url
            if chave is not None:
                kw["api_key"] = chave
        if self.adaptador == "openai_compativel":
            kw["envia_effort"] = self.envia_effort
            if self.saida_estruturada:
                kw["saida_padrao"] = self.saida_estruturada
            if chave is None:
                kw["api_key"] = "sem-chave"  # servidor local sem autenticacao (o SDK exige algum valor)
        elif self.adaptador == "azure_openai":
            kw["azure_endpoint"] = url or None
            kw["api_version"] = env.get("OPENAI_API_VERSION") or self.parametros.get("api_version")
            if chave is not None:
                kw["api_key"] = chave
        elif self.adaptador == "ollama":
            if url:
                kw["host"] = url
        elif self.adaptador in ("google", "gemini_vertex"):
            # GOOGLE_APPLICATION_CREDENTIALS e um ARQUIVO de credencial lido pelo proprio SDK, nao uma chave
            token = next((env[v] for v in self.chave_env if v != _CREDENCIAL_ARQUIVO and env.get(v)), None)
            if token is not None:
                kw["api_key"] = token
        elif self.adaptador == "anthropic_vertex":
            token = next((env[v] for v in self.chave_env if v != _CREDENCIAL_ARQUIVO and env.get(v)), None)
            if token is not None:
                kw["access_token"] = token
            if url:
                kw["base_url"] = url
        for k, v in self.parametros.items():
            if k.endswith("_env"):
                valor = env.get(str(v))
                if valor:
                    kw[k[:-4]] = valor
            elif k not in kw or kw[k] is None:
                kw[k] = v
        if self.adaptador == "gemini_vertex":
            kw.setdefault("projeto", env.get("GOOGLE_CLOUD_PROJECT"))
            kw.setdefault("regiao", env.get("GOOGLE_CLOUD_LOCATION"))
        return kw

    def resumo(self, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Para ``robowin ia listar``: nunca inclui o valor da chave."""
        return {"nome": self.nome, "adaptador": self.adaptador, "base_url": self.url_efetiva(env),
                "chave_env": list(self.chave_env), "chave": self.estado_chave(env),
                "faltam": self.variaveis_ausentes(env), "local": self.local, "regiao": self.regiao,
                "verificado": self.verificado}


def _tupla(v: Any, campo: str, nome: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,) if v else ()
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return tuple(v)
    raise ErroRegistro(f"provedor {nome}: '{campo}' deve ser texto ou lista de textos")


def validar_definicao(nome: str, d: Mapping[str, Any]) -> DefProvedor:
    if not _RE_NOME.match(nome) or ":" in nome:
        raise ErroRegistro(f"nome de provedor invalido: {nome!r} (minusculas, digitos, '_', '-', '.'; sem ':')")
    ad = d.get("adaptador")
    if ad not in ADAPTADORES or ad == "falso":
        raise ErroRegistro(f"provedor {nome}: adaptador invalido {ad!r} (validos: {', '.join(ADAPTADORES[:-1])})")
    conhecidos = {"adaptador", "base_url", "base_url_env", "chave_env", "chave_obrigatoria", "exige_env", "local",
                  "saida_estruturada", "envia_effort", "regiao", "observacoes", "verificado", "fonte_verificacao",
                  "parametros"}
    extras = set(d) - conhecidos
    if extras:
        raise ErroRegistro(f"provedor {nome}: campos desconhecidos {sorted(extras)}")
    saida = str(d.get("saida_estruturada", "") or "")
    if saida and saida not in SAIDAS_COMPATIVEL:
        raise ErroRegistro(f"provedor {nome}: saida_estruturada invalida {saida!r} (use {SAIDAS_COMPATIVEL})")
    url = str(d.get("base_url", "") or "")
    if url and not url.startswith(("https://", "http://")):
        raise ErroRegistro(f"provedor {nome}: base_url deve comecar com http:// ou https://")
    if url.startswith("http://") and not d.get("local", False):
        raise ErroRegistro(f"provedor {nome}: http:// sem TLS so para servidor local (local = true)")
    if ad == "openai_compativel" and not url and not d.get("base_url_env"):
        raise ErroRegistro(f"provedor {nome}: openai_compativel exige base_url (ou base_url_env)")
    params = d.get("parametros", {}) or {}
    if not isinstance(params, dict):
        raise ErroRegistro(f"provedor {nome}: 'parametros' deve ser uma tabela")
    return DefProvedor(
        nome=nome, adaptador=str(ad), base_url=url, base_url_env=str(d.get("base_url_env", "") or ""),
        chave_env=_tupla(d.get("chave_env"), "chave_env", nome),
        chave_obrigatoria=bool(d.get("chave_obrigatoria", True)), exige_env=_tupla(d.get("exige_env"), "exige_env", nome),
        local=bool(d.get("local", False)), saida_estruturada=saida, envia_effort=bool(d.get("envia_effort", False)),
        regiao=str(d.get("regiao", "") or ""), observacoes=str(d.get("observacoes", "") or ""),
        verificado=bool(d.get("verificado", False)), fonte_verificacao=str(d.get("fonte_verificacao", "") or ""),
        parametros=dict(params))


# Registro minimo embutido (pacote instalado sem a pasta config/): os provedores historicos do projeto.
_EMBUTIDO: dict[str, dict[str, Any]] = {
    "anthropic": {"adaptador": "anthropic", "chave_env": ["ANTHROPIC_API_KEY"]},
    "openai": {"adaptador": "openai", "chave_env": ["OPENAI_API_KEY"]},
    "google": {"adaptador": "google", "chave_env": ["GOOGLE_API_KEY", "GEMINI_API_KEY"]},
    "openai_compativel": {"adaptador": "openai_compativel", "base_url_env": "OPENAI_COMPATIVEL_BASE_URL",
                          "chave_env": ["OPENAI_COMPATIVEL_API_KEY"], "chave_obrigatoria": False,
                          "exige_env": ["OPENAI_COMPATIVEL_BASE_URL"], "envia_effort": True},
    "ollama": {"adaptador": "ollama", "base_url": "http://localhost:11434", "base_url_env": "OLLAMA_HOST",
               "chave_obrigatoria": False, "local": True},
}


class RegistroProvedores:
    def __init__(self, defs: Mapping[str, DefProvedor], origem: str = "") -> None:
        self._defs = dict(defs)
        self.origem = origem

    @classmethod
    def de_dict(cls, cfg: Mapping[str, Any], origem: str = "") -> RegistroProvedores:
        provs = cfg.get("provedor", {}) or {}
        if not isinstance(provs, dict):
            raise ErroRegistro("use tabelas [provedor.<nome>] no registro de provedores")
        return cls({n: validar_definicao(n, d) for n, d in provs.items()}, origem)

    @classmethod
    def carregar(cls, caminho: str | Path | None = None) -> RegistroProvedores:
        """Le ``config/provedores.toml`` (CWD, depois a raiz do repositorio); sem arquivo, o registro embutido."""
        for c in (caminho, CAMINHO_PADRAO, Path(__file__).resolve().parents[3] / CAMINHO_PADRAO):
            if c and Path(c).is_file():
                return cls.de_dict(carregar_toml_tolerante(c), str(c))
        if caminho:
            raise ErroRegistro(f"registro de provedores nao encontrado: {caminho}")
        return cls.de_dict({"provedor": _EMBUTIDO}, "embutido")

    def __contains__(self, nome: object) -> bool:
        return nome in self._defs

    def __iter__(self) -> Iterator[DefProvedor]:
        return iter(self._defs.values())

    def nomes(self) -> list[str]:
        return list(self._defs)

    def obter(self, nome: str) -> DefProvedor:
        try:
            return self._defs[nome]
        except KeyError:
            raise ErroRegistro(
                f"provedor nao registrado: {nome!r}. Adicione [provedor.{nome}] em config/provedores.toml "
                f"(registrados: {', '.join(self._defs)})") from None

    def chave_presente(self, nome: str, env: Mapping[str, str] | None = None) -> bool:
        return nome in self._defs and self._defs[nome].chave_presente(env)

    def mensagem_sem_chave(self, nome: str, env: Mapping[str, str] | None = None) -> str:
        """Texto claro (sem valor de chave) do que falta e de como definir no Windows."""
        d = self.obter(nome)
        faltam = d.variaveis_ausentes(env)
        exemplo = (d.chave_env[0] if d.chave_env else (faltam[0] if faltam else "VARIAVEL"))
        return (f"sem chave/configuracao para o provedor '{nome}': falta {', '.join(faltam) or '?'}. "
                f"No Windows: setx {exemplo} \"<valor>\" e abra um NOVO terminal (o valor nunca e exibido).")

    def criar(self, nome: str, *, catalogo: Any = None, env: Mapping[str, str] | None = None, **kw: Any) -> Any:
        """Instancia o adaptador do provedor ``nome`` (sem rede). ``kw`` (ex.: ``cliente``, ``http_client``)
        sobrepoe o que vem do registro: e assim que os testes injetam um transporte falso."""
        from robowin.ia.provedores import criar_provedor
        d = self.obter(nome)
        args = {**d.kwargs_adaptador(env), **kw}
        return criar_provedor(d.adaptador, catalogo=catalogo, **args)


_cache: dict[str, RegistroProvedores] = {}


def registro_padrao(caminho: str | Path | None = None) -> RegistroProvedores:
    """Registro carregado (uma vez por caminho/CWD)."""
    chave = f"{caminho}|{Path.cwd()}"
    if chave not in _cache:
        _cache[chave] = RegistroProvedores.carregar(caminho)
    return _cache[chave]
