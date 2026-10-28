"""Edicao TEXTUAL cuidadosa de chaves de UMA secao de um TOML, preservando todo o resto (comentarios, ordem, espacos).

Sem dependencia nova: as linhas da secao `[NOME]` (ate o proximo cabecalho de tabela) sao varridas com controle de
strings e colchetes; cada chave pedida e substituida (todas as suas linhas, se o valor for multilinha) ou inserida
depois da ultima atribuicao da secao. O resultado e CONFERIDO: relido com tomllib, tem de ser identico ao original
exceto pelas chaves alteradas; se nao for, nada e gravado (`ErroEdicaoToml`).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any


class ErroEdicaoToml(Exception):
    pass


RE_CHAVE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def valor_toml(v: Any) -> str:
    """Serializa bool, int, float, str e lista simples no formato TOML."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # string basica TOML (escapes compativeis)
    if isinstance(v, list | tuple):
        return "[" + ", ".join(valor_toml(x) for x in v) + "]"
    raise ErroEdicaoToml(f"tipo nao suportado na edicao textual: {type(v).__name__}")


def _profundidade_apos(linha: str, prof: int) -> int:
    """Profundidade de colchetes/chaves ao fim de `linha`, ignorando strings e comentarios. Strings multilinha
    (triplas) nao sao suportadas: levanta para nao arriscar uma edicao errada."""
    if '"""' in linha or "'''" in linha:
        raise ErroEdicaoToml("strings multilinha nao sao suportadas pela edicao textual")
    i, n = 0, len(linha)
    while i < n:
        c = linha[i]
        if c == "#":
            break
        if c == '"':
            i += 1
            while i < n and linha[i] != '"':
                i += 2 if linha[i] == "\\" else 1
        elif c == "'":
            i += 1
            while i < n and linha[i] != "'":
                i += 1
        elif c in "[{":
            prof += 1
        elif c in "]}":
            prof -= 1
        i += 1
    return prof


def _cabecalho(linha: str) -> str | None:
    """Nome da tabela se a linha for cabecalho `[x]`/`[[x.y]]`, senao None."""
    m = re.match(r"^\s*(\[\[?)\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", linha.rstrip("\r\n"))
    return m.group(2) if m else None


def _mapear(linhas: list[str], secao: str) -> tuple[int, int, dict[str, tuple[int, int]]]:
    """(inicio, fim, chaves) da secao `[secao]`: linhas [inicio, fim) e o intervalo [a, b) de cada chave."""
    prof, ini, fim = 0, -1, len(linhas)
    chaves: dict[str, tuple[int, int]] = {}
    atual: tuple[str, int] | None = None
    for i, ln in enumerate(linhas):
        if prof == 0:
            nome = _cabecalho(ln) if ln.lstrip().startswith("[") else None
            if nome is not None:
                if ini >= 0:
                    fim = i
                    break
                if nome == secao and not ln.lstrip().startswith("[["):
                    ini = i + 1
                continue
            if ini >= 0:
                m = RE_CHAVE.match(ln)
                if m:
                    atual = (m.group(1), i)
        prof = _profundidade_apos(ln, prof)
        if ini >= 0 and prof == 0 and atual is not None:
            chaves[atual[0]] = (atual[1], i + 1)
            atual = None
    if ini < 0:
        raise ErroEdicaoToml(f"secao [{secao}] nao encontrada")
    if prof != 0:
        raise ErroEdicaoToml("colchetes desbalanceados: edicao textual recusada")
    return ini, fim, chaves


def atualizar_secao(texto: str, secao: str, novos: dict[str, Any]) -> str:
    """Texto com as chaves `novos` da tabela `[secao]` trocadas/inseridas; o resto do arquivo fica intacto."""
    original = tomllib.loads(texto)
    nl = "\r\n" if "\r\n" in texto else "\n"
    linhas = texto.splitlines(keepends=True)
    if linhas and not linhas[-1].endswith(("\n", "\r")):
        linhas[-1] += nl
    ini, _fim, chaves = _mapear(linhas, secao)
    inserir: list[str] = []
    substituir: dict[int, tuple[int, str]] = {}
    for k, v in novos.items():
        linha_nova = f"{k} = {valor_toml(v)}{nl}"
        if k in chaves:
            a, b = chaves[k]
            substituir[a] = (b, linha_nova)
        else:
            inserir.append(linha_nova)
    pos_insercao = max((b for _a, b in chaves.values()), default=ini)
    saida: list[str] = []
    i = 0
    while i < len(linhas):
        if i == pos_insercao:
            saida.extend(inserir)
            inserir = []
        if i in substituir:
            b, nova = substituir[i]
            saida.append(nova)
            i = b
            continue
        saida.append(linhas[i])
        i += 1
    saida.extend(inserir)
    resultado = "".join(saida)
    # conferencia: so as chaves pedidas mudaram
    esperado = {**original, secao: {**original[secao], **{k: list(v) if isinstance(v, tuple) else v
                                                         for k, v in novos.items()}}}
    try:
        obtido = tomllib.loads(resultado)
    except tomllib.TOMLDecodeError as e:
        raise ErroEdicaoToml(f"edicao produziria TOML invalido: {e}") from e
    if obtido != esperado:
        raise ErroEdicaoToml("edicao alteraria algo alem das chaves pedidas: recusada (nada gravado)")
    return resultado


def gravar_atomico(caminho: str | Path, texto: str) -> None:
    """Grava `texto` em `caminho` via arquivo temporario + os.replace (nunca deixa o arquivo pela metade)."""
    p = Path(caminho)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(texto)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
