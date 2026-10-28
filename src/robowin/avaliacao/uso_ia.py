"""Contabilidade de uso de IA por braco e por papel (custo, tokens, latencia, chamadas invalidas).

Ideia adaptada do ``UsageLedger`` do Aurum (MIT): ver, por agente, quanto cada etapa custou e quanto demorou.
O robowin ja RESERVA e CONFIRMA o custo em ``ia.orcamento``; aqui so se AGREGA o que ficou gravado em
``chamadas_llm`` para o relatorio, o que importa para os bracos de varias etapas (debate: 5 etapas + sintese +
revisor) onde o custo total de um braco nao cabe numa unica linha.

Custo em USD (faturamento da API, sem conversao de moeda). Nada aqui decide ordem nem limite.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

CAMPOS = ("braco", "papel", "total_ms", "custo_usd", "valida", "tokens_entrada", "tokens_saida", "tokens_cache")


def carregar_chamadas(banco: str | Path) -> list[dict[str, Any]]:
    """Linhas de ``chamadas_llm`` (so as colunas usadas aqui). Banco sem a tabela => lista vazia."""
    con = sqlite3.connect(f"file:{banco}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(chamadas_llm)")}
        if not cols:
            return []
        sel = ", ".join(c if c in cols else f"0 AS {c}" for c in CAMPOS)
        return [dict(r) for r in con.execute(f"SELECT {sel} FROM chamadas_llm")]
    finally:
        con.close()


def _quantis(v: list[float]) -> dict[str, float]:
    if not v:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    p = np.percentile(v, [50, 95, 99])
    return {"p50": round(float(p[0]), 1), "p95": round(float(p[1]), 1), "p99": round(float(p[2]), 1)}


def _agregar(linhas: list[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(linhas)
    invalidas = sum(1 for x in linhas if not x.get("valida"))
    return {
        "chamadas": n, "invalidas": invalidas, "taxa_invalidas": (invalidas / n) if n else 0.0,
        "custo_usd": round(sum(float(x.get("custo_usd") or 0.0) for x in linhas), 6),
        "tokens_entrada": int(sum(int(x.get("tokens_entrada") or 0) for x in linhas)),
        "tokens_saida": int(sum(int(x.get("tokens_saida") or 0) for x in linhas)),
        "tokens_cache": int(sum(int(x.get("tokens_cache") or 0) for x in linhas)),
        "latencia_ms": _quantis([float(x.get("total_ms") or 0.0) for x in linhas]),
    }


def resumir_uso(chamadas: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """{'por_braco': {braco: {total.., 'papeis': {papel: {..}}}}, 'total': {..}}."""
    todas = list(chamadas)
    por_braco: dict[str, list[Mapping[str, Any]]] = {}
    for x in todas:
        por_braco.setdefault(str(x.get("braco") or "?"), []).append(x)
    out: dict[str, Any] = {}
    for b in sorted(por_braco):
        papeis: dict[str, list[Mapping[str, Any]]] = {}
        for x in por_braco[b]:
            papeis.setdefault(str(x.get("papel") or "?"), []).append(x)
        out[b] = {**_agregar(por_braco[b]), "papeis": {p: _agregar(v) for p, v in sorted(papeis.items())}}
    return {"por_braco": out, "total": _agregar(todas)}


def renderizar_markdown(uso: Mapping[str, Any]) -> str:
    """Secao de Markdown para anexar ao relatorio de avaliacao (vazia se nao houve chamada de IA)."""
    if not uso["total"]["chamadas"]:
        return ""
    ln = ["", "## Uso de IA por braco e papel (custo em USD; latencia em ms)", "",
          "| braco | papel | chamadas | invalidas | custo (USD) | p50 | p95 | p99 |", "|---|---|---|---|---|---|---|---|"]
    for b, d in uso["por_braco"].items():
        ln.append(f"| **{b}** | (todos) | {d['chamadas']} | {d['invalidas']} | {d['custo_usd']:.4f} | "
                  f"{d['latencia_ms']['p50']} | {d['latencia_ms']['p95']} | {d['latencia_ms']['p99']} |")
        for p, x in d["papeis"].items():
            ln.append(f"| {b} | {p} | {x['chamadas']} | {x['invalidas']} | {x['custo_usd']:.4f} | "
                      f"{x['latencia_ms']['p50']} | {x['latencia_ms']['p95']} | {x['latencia_ms']['p99']} |")
    t = uso["total"]
    ln += ["", f"Total: {t['chamadas']} chamadas, {t['invalidas']} invalidas, USD {t['custo_usd']:.4f}.",
           "Custo de IA e fator de decisao: um braco so se justifica se o ganho liquido superar custo e latencia "
           "(ver `valor_do_veto` e o beneficio liquido de LLM)."]
    return "\n".join(ln) + "\n"
