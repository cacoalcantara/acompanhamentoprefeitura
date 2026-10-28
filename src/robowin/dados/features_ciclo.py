"""Vetor de features por ciclo de decisao (tabela ``features_ciclo``, migracao registrada por este modulo).

O Orquestrador ja calcula, a cada ciclo, o vetor do ``PipelineFeatures`` (so features da lista PERMITIDA de
``features/registro.py``). Este modulo grava esse vetor, uma linha por ciclo (grupo de bracos), para que o PSI do
``robowin avaliar``/painel (``avaliacao/monitoramento_banco.py``) compare as features REAIS e nao proxies.

* Tabela FORA do esquema congelado (``core/esquema_banco.py``): criada por ``BancoSQLite.registrar_migracao`` com o
  modulo ``MODULO``. Banco antigo sem a tabela continua funcionando (a leitura devolve vazio e o PSI cai nos proxies).
* Compacta: ``valores_json`` e um objeto JSON ``{nome: valor}`` com chaves ordenadas, sem espacos, valores com
  ``CASAS_SIGNIFICATIVAS`` algarismos significativos; nao finitos (aquecimento) ficam de fora.
* Nada aqui entra no dossie, na auditoria ou em ``hash_resultado``: os hashes existentes nao mudam.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

MODULO = "features_ciclo"
TABELA = "features_ciclo"
DDL = [
    f"""CREATE TABLE IF NOT EXISTS {TABELA}(id TEXT PRIMARY KEY, t_ns INTEGER, sessao TEXT, ciclo_id TEXT,
       bracos TEXT, gatilhos TEXT, n INTEGER, valores_json TEXT)""",
    f"CREATE INDEX IF NOT EXISTS idx_{TABELA}_t ON {TABELA}(t_ns)",
]
CASAS_SIGNIFICATIVAS = 7


def garantir_tabela(banco: Any) -> bool:
    """Aplica a migracao (idempotente). False se o banco nao suporta migracoes (duble de teste): nao grava."""
    reg = getattr(banco, "registrar_migracao", None)
    if reg is None:
        return False
    reg(MODULO, DDL)
    return True


def compactar(valores: Mapping[str, Any], permitidas: Iterable[str]) -> dict[str, float]:
    """So features permitidas, finitas, com ``CASAS_SIGNIFICATIVAS`` algarismos significativos."""
    ok = set(permitidas)
    out: dict[str, float] = {}
    for k in sorted(valores):
        v = valores[k]
        if k not in ok or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        f = float(v)
        if math.isfinite(f):
            out[k] = float(f"{f:.{CASAS_SIGNIFICATIVAS}g}")
    return out


def linha(*, id: str, t_ns: int, sessao: str, ciclo_id: str, bracos: Iterable[str], gatilhos: Iterable[str],
          valores: Mapping[str, float]) -> dict[str, Any]:
    return {"id": id, "t_ns": int(t_ns), "sessao": sessao, "ciclo_id": ciclo_id, "bracos": ",".join(bracos),
            "gatilhos": ",".join(gatilhos), "n": len(valores),
            "valores_json": json.dumps(dict(valores), sort_keys=True, separators=(",", ":"))}


def ler_vetores(banco: str | Path) -> list[tuple[int, dict[str, float]]]:
    """``(t_ns, valores)`` em ordem cronologica, um por instante (grupos no mesmo ``t`` compartilham o vetor).

    Somente leitura; banco inexistente, sem a tabela ou de outra versao => lista vazia (nunca quebra)."""
    p = Path(banco)
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        linhas = list(con.execute(f"SELECT t_ns, valores_json FROM {TABELA} ORDER BY t_ns, id"))
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out: list[tuple[int, dict[str, float]]] = []
    vistos: set[int] = set()
    for t, js in linhas:
        if t is None or int(t) in vistos:
            continue
        try:
            d = json.loads(js) if js else None
        except (TypeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        vals = {str(k): float(v) for k, v in d.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))}
        if vals:
            vistos.add(int(t))
            out.append((int(t), vals))
    return out
