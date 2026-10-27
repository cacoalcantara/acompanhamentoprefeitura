"""Catalogo de modelos (config/modelos.toml). Falha segura: sem preco ou sem corte, o modelo e recusado."""
from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from robowin.core.tipos import ChamadaLLM, EntradaCatalogoModelo

_RE_CORTE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?$")


class ErroCatalogo(Exception):
    pass


def _dividir_por_ponto_e_virgula(linha: str) -> list[str]:
    """Divide 'a = 1; b = "x;y"' em linhas, ignorando ';' dentro de aspas e comentarios."""
    partes, atual, aspas = [], [], None
    for ch in linha:
        if aspas:
            atual.append(ch)
            if ch == aspas:
                aspas = None
        elif ch in "\"'":
            aspas = ch
            atual.append(ch)
        elif ch == "#":
            break
        elif ch == ";":
            partes.append("".join(atual))
            atual = []
        else:
            atual.append(ch)
    partes.append("".join(atual))
    return partes


def carregar_toml_tolerante(caminho: str | Path) -> dict[str, Any]:
    """TOML estrito; se falhar, aceita ';' como separador de pares na mesma linha (como em config/modelos.toml)."""
    texto = Path(caminho).read_text(encoding="utf-8")
    try:
        return tomllib.loads(texto)
    except tomllib.TOMLDecodeError:
        linhas: list[str] = []
        for ln in texto.splitlines():
            linhas.extend(_dividir_por_ponto_e_virgula(ln))
        return tomllib.loads("\n".join(linhas))


def dividir_modelo(ref: str) -> tuple[str, str]:
    """'provedor:id' -> (provedor, id). Divide so no primeiro ':' (ids do Ollama contem ':')."""
    if ":" not in ref:
        raise ErroCatalogo(f"modelo deve estar no formato 'provedor:id': {ref!r}")
    prov, _, mid = ref.partition(":")
    if not prov or not mid:
        raise ErroCatalogo(f"modelo deve estar no formato 'provedor:id': {ref!r}")
    return prov, mid


def validar_entrada(bruta: dict[str, Any]) -> EntradaCatalogoModelo:
    ident = f"{bruta.get('provedor', '?')}:{bruta.get('id', '?')}"
    for campo in ("preco_entrada_mtok", "preco_saida_mtok"):
        if bruta.get(campo) is None:
            raise ErroCatalogo(f"modelo {ident} recusado: sem preco ({campo})")
    if not bruta.get("corte_conhecimento"):
        raise ErroCatalogo(f"modelo {ident} recusado: sem data de corte de conhecimento")
    try:
        e = EntradaCatalogoModelo(**bruta)
    except ValidationError as ex:
        raise ErroCatalogo(f"modelo {ident} recusado: {ex.errors()[0].get('loc')} invalido") from ex
    if not _RE_CORTE.match(e.corte_conhecimento):
        raise ErroCatalogo(f"modelo {ident} recusado: corte_conhecimento invalido ({e.corte_conhecimento!r})")
    if min(e.preco_entrada_mtok, e.preco_saida_mtok, e.preco_cache_mtok) < 0:
        raise ErroCatalogo(f"modelo {ident} recusado: preco negativo")
    return e


def _chave_corte(c: str) -> tuple[int, int, int]:
    p = [int(x) for x in c.split("-")]
    return (p[0], p[1], p[2] if len(p) > 2 else 0)


class Catalogo:
    def __init__(self, entradas: Iterable[EntradaCatalogoModelo]):
        self._por_ref: dict[str, EntradaCatalogoModelo] = {}
        for e in entradas:
            ref = f"{e.provedor}:{e.id}"
            if ref in self._por_ref:
                raise ErroCatalogo(f"modelo duplicado no catalogo: {ref}")
            self._por_ref[ref] = e

    @classmethod
    def de_dict(cls, cfg: dict[str, Any]) -> Catalogo:
        return cls(validar_entrada(m) for m in cfg.get("modelo", []))

    @classmethod
    def carregar(cls, caminho: str | Path = "config/modelos.toml") -> Catalogo:
        return cls.de_dict(carregar_toml_tolerante(caminho))

    def __contains__(self, ref: str) -> bool:
        return ref in self._por_ref

    def __iter__(self):
        return iter(self._por_ref.values())

    def obter(self, ref: str) -> EntradaCatalogoModelo:
        dividir_modelo(ref)
        try:
            return self._por_ref[ref]
        except KeyError:
            raise ErroCatalogo(f"modelo fora do catalogo (falha segura): {ref}") from None

    def custo_tokens(self, ref: str, tokens_entrada: int, tokens_cache: int, tokens_saida: int) -> float:
        e = self.obter(ref)
        return (tokens_entrada * e.preco_entrada_mtok + tokens_cache * e.preco_cache_mtok
                + tokens_saida * e.preco_saida_mtok) / 1_000_000

    def custo(self, c: ChamadaLLM) -> float:
        """Custo em US$ de uma chamada, pelo modelo PEDIDO (precos do catalogo)."""
        return self.custo_tokens(c.modelo_pedido, c.tokens_entrada, c.tokens_cache, c.tokens_saida)

    def pior_caso(self, ref: str, tokens_entrada_estimados: int, max_tokens: int) -> float:
        """Custo maximo possivel: entrada sem cache + saida inteira (tokens de raciocinio contam como saida)."""
        e = self.obter(ref)
        return (tokens_entrada_estimados * e.preco_entrada_mtok + max_tokens * e.preco_saida_mtok) / 1_000_000

    def fronteira_contaminacao(self, refs: Iterable[str]) -> str:
        """Corte mais RECENTE entre os modelos usados: dados do modelo ate essa data podem estar no treino;
        so dados posteriores a ela estao livres de contaminacao para todos."""
        cortes = [self.obter(r).corte_conhecimento for r in refs]
        if not cortes:
            raise ErroCatalogo("nenhum modelo informado")
        return max(cortes, key=_chave_corte)

    def exigir(self, refs: Iterable[str]) -> None:
        for r in refs:
            self.obter(r)
