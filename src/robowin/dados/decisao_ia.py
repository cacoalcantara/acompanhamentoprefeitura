"""Tabelas da decisao completa pela IA (braco ``planejador``; docs/16), migracao registrada por este modulo.

* ``posicoes_episodio``: um episodio = uma posicao, da abertura ao fechamento (parciais e ajustes somados).
* ``gestao_eventos``: cada ajuste de protecao (da IA ou automatico), aplicado ou nao, com SL/TP antes e depois.
* ``previsoes_ia``: ``p_alvo``/``incerteza`` declarados, resolvidos depois (Brier, BSS, calibracao).
* ``contrafactuais``: politicas alternativas sobre a mesma decisao (so diagnostico, nunca metrica primaria).

Mesmo padrao de ``dados/features_ciclo.py``: tabelas FORA do esquema congelado M0 (``core/esquema_banco.py``),
criadas por ``BancoSQLite.registrar_migracao`` com o modulo ``MODULO``. Banco antigo sem as tabelas continua
funcionando (a leitura devolve vazio); duble sem suporte a migracao => ``garantir_tabelas`` devolve False e nada e
gravado. Nada aqui entra no dossie, na auditoria ou em ``hash_resultado``: os hashes existentes nao mudam.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MODULO = "decisao_ia"

EPISODIOS = "posicoes_episodio"
GESTAO = "gestao_eventos"
PREVISOES = "previsoes_ia"
CONTRAFACTUAIS = "contrafactuais"

# coluna -> tipo SQLite; a ordem e a do DDL (a primeira coluna e a chave primaria). CONGELADO: o CREATE TABLE
# de cada tabela entra no hash da migracao; coluna nova vai em ``COLUNAS_ACRESCIDAS`` (ALTER TABLE no fim de DDL).
_COLUNAS_V1: dict[str, dict[str, str]] = {
    EPISODIOS: {
        "id": "TEXT PRIMARY KEY", "braco": "TEXT", "ticket": "INTEGER", "decisao_id": "TEXT",
        "intencao_id": "TEXT", "lado": "TEXT", "q_inicial": "INTEGER", "ancora_t0": "REAL",
        "stop_inicial_pts": "REAL", "alvo_inicial_pts": "REAL", "tempo_max_s": "INTEGER", "t_aberta_ns": "INTEGER",
        "t_fechada_ns": "INTEGER", "liquido_total": "REAL", "n_parciais": "INTEGER", "n_ajustes_ia": "INTEGER",
        "n_ajustes_auto": "INTEGER", "mfe_pts": "REAL", "mae_pts": "REAL", "motivo_final": "TEXT", "p_alvo": "REAL",
        "sombra_de": "TEXT", "trades_json": "TEXT"},
    GESTAO: {
        "id": "TEXT PRIMARY KEY", "t_ns": "INTEGER", "braco": "TEXT", "ticket": "INTEGER", "origem": "TEXT",
        "acao": "TEXT", "regra": "TEXT", "sl_antes": "REAL", "sl_depois": "REAL", "tp_antes": "REAL",
        "tp_depois": "REAL", "prazo_ns": "INTEGER", "aplicado": "INTEGER", "motivo": "TEXT"},
    PREVISOES: {
        "id": "TEXT PRIMARY KEY", "decisao_id": "TEXT", "t0_ns": "INTEGER", "braco": "TEXT", "lado": "TEXT",
        "p_alvo": "REAL", "incerteza": "REAL", "stop_pts": "REAL", "alvo_pts": "REAL", "tempo_s": "INTEGER",
        "ancora": "REAL", "executada": "INTEGER", "y_alvo": "INTEGER", "fonte_y": "TEXT", "resolvido_ns": "INTEGER"},
    CONTRAFACTUAIS: {
        "id": "TEXT PRIMARY KEY", "decisao_id": "TEXT", "braco_ref": "TEXT", "politica": "TEXT", "liquido": "REAL",
        "motivo": "TEXT", "t_fim_ns": "INTEGER", "y_alvo": "INTEGER", "seed": "INTEGER"},
}
# Colunas acrescentadas depois (ALTER TABLE ... ADD COLUMN, so no fim de ``DDL``):
# * previsoes_ia.t_entrega_ns: instante em que a decisao foi ENTREGUE (t0 do dossie + latencia da IA). Os
#   contrafactuais partem dai, nunca de t0 (causalidade: o preco andou durante a latencia).
# * previsoes_ia.t_execucao_ns: instante do disparo/execucao real da entrada (alinha sem_gestao/trailing_fixo com o
#   trade real e as sombras).
COLUNAS_ACRESCIDAS: dict[str, dict[str, str]] = {
    PREVISOES: {"t_entrega_ns": "INTEGER", "t_execucao_ns": "INTEGER"},
}
COLUNAS: dict[str, dict[str, str]] = {t: {**cols, **COLUNAS_ACRESCIDAS.get(t, {})} for t, cols in _COLUNAS_V1.items()}
TABELAS = tuple(COLUNAS)

_INDICES = {EPISODIOS: ("braco", "t_aberta_ns"), GESTAO: ("braco", "ticket", "t_ns"), PREVISOES: ("braco", "t0_ns"),
            CONTRAFACTUAIS: ("decisao_id",)}

# A ordem desta lista e o contrato da migracao (indice + hash): so ACRESCENTAR DDLs novos no fim.
DDL: list[str] = (
    [f"CREATE TABLE IF NOT EXISTS {t}({', '.join(f'{c} {tp}' for c, tp in cols.items())})"
     for t, cols in _COLUNAS_V1.items()]
    + [f"CREATE INDEX IF NOT EXISTS idx_{t}_{'_'.join(cs)} ON {t}({', '.join(cs)})" for t, cs in _INDICES.items()]
    + [f"ALTER TABLE {t} ADD COLUMN {c} {tp}" for t, cols in COLUNAS_ACRESCIDAS.items() for c, tp in cols.items()]
)


def garantir_tabelas(banco: Any) -> bool:
    """Aplica a migracao (idempotente). False se o banco nao suporta migracoes (duble de teste): nao grava."""
    reg = getattr(banco, "registrar_migracao", None)
    if reg is None:
        return False
    reg(MODULO, DDL)
    return True


def linha(tabela: str, campos: Mapping[str, Any]) -> dict[str, Any]:
    """Linha completa da ``tabela``: colunas ausentes ficam NULL; booleanos viram 0/1; coluna desconhecida e erro
    (o contrato das tabelas e fixo: um campo novo exige um DDL novo no fim de ``DDL``)."""
    if tabela not in COLUNAS:
        raise ValueError(f"tabela desconhecida: {tabela}")
    cols = COLUNAS[tabela]
    extras = sorted(set(campos) - set(cols))
    if extras:
        raise ValueError(f"{tabela}: colunas desconhecidas: {', '.join(extras)}")
    if campos.get("id") in (None, ""):
        raise ValueError(f"{tabela}: id obrigatorio")
    out: dict[str, Any] = {}
    for c in cols:
        v = campos.get(c)
        out[c] = int(v) if isinstance(v, bool) else v
    return out


def ler(banco: str | Path, tabela: str, *, onde: str = "", parametros: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    """Linhas da ``tabela`` (somente leitura, ordenadas pela chave). Banco inexistente, sem a tabela ou de outra
    versao => lista vazia (nunca quebra). ``onde`` e um filtro SQL com ``?`` (nunca valores interpolados)."""
    if tabela not in COLUNAS:
        raise ValueError(f"tabela desconhecida: {tabela}")
    p = Path(banco)
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        sql = f"SELECT * FROM {tabela}" + (f" WHERE {onde}" if onde else "") + " ORDER BY id"
        return [dict(r) for r in con.execute(sql, parametros)]
    except sqlite3.Error:
        return []
    finally:
        con.close()
