"""BancoSQLite: SQLite em modo WAL, thread-safe, com migracoes registradas por modulo."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from robowin.core.esquema_banco import DDL

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BancoSQLite:
    """Implementa o Protocolo `Banco`. Valores sempre via parametros; nomes validados contra o esquema."""

    def __init__(self, caminho: str | Path = ":memory:", *, timeout_s: float = 30.0) -> None:
        self.caminho = str(caminho)
        if self.caminho != ":memory:":
            Path(self.caminho).parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(self.caminho, check_same_thread=False, timeout=timeout_s, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._prof = 0
        with self._lock:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
            self._con.execute("PRAGMA foreign_keys=ON")
            self._con.execute(f"PRAGMA busy_timeout={int(timeout_s * 1000)}")
            for ddl in DDL:
                self._con.execute(ddl)
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS _migracoes(modulo TEXT, indice INTEGER, hash TEXT, aplicado_ns INTEGER, "
                "PRIMARY KEY(modulo, indice))")

    # -- util ---------------------------------------------------------------
    @property
    def modo_journal(self) -> str:
        with self._lock:
            return str(self._con.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def tabelas(self) -> list[str]:
        return [r["name"] for r in self.consultar("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def _colunas(self, tabela: str) -> list[str]:
        if not _IDENT.match(tabela):
            raise ValueError(f"nome de tabela invalido: {tabela!r}")
        cols = [r["name"] for r in self._con.execute(f'PRAGMA table_info("{tabela}")')]
        if not cols:
            raise ValueError(f"tabela inexistente: {tabela!r}")
        return cols

    @staticmethod
    def _valor(v: Any) -> Any:
        if isinstance(v, bool):
            return int(v)
        if hasattr(v, "model_dump_json"):
            return v.model_dump_json()
        if isinstance(v, (dict, list, tuple)):
            return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
        return v

    # -- API ----------------------------------------------------------------
    def inserir(self, tabela: str, linha: dict[str, Any]) -> None:
        if not linha:
            raise ValueError("linha vazia")
        with self._lock:
            validas = set(self._colunas(tabela))
            desconhecidas = [c for c in linha if c not in validas]
            if desconhecidas:
                raise ValueError(f"colunas inexistentes em {tabela}: {desconhecidas}")
            cols = list(linha)
            sql = (f'INSERT OR REPLACE INTO "{tabela}"(' + ",".join(f'"{c}"' for c in cols) + ") VALUES ("
                   + ",".join("?" * len(cols)) + ")")
            self._con.execute(sql, tuple(self._valor(linha[c]) for c in cols))

    def inserir_muitos(self, tabela: str, linhas: Sequence[dict[str, Any]]) -> None:
        with self.transacao():
            for ln in linhas:
                self.inserir(tabela, ln)

    def consultar(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._con.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def executar(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            return self._con.execute(sql, tuple(params)).rowcount

    @contextmanager
    def transacao(self) -> Iterator[BancoSQLite]:
        """Transacao atomica (reentrante: aninhadas participam da externa)."""
        with self._lock:
            externa = self._prof == 0
            if externa:
                self._con.execute("BEGIN IMMEDIATE")
            self._prof += 1
            try:
                yield self
            except BaseException:
                self._prof -= 1
                if externa:
                    self._con.execute("ROLLBACK")
                raise
            else:
                self._prof -= 1
                if externa:
                    self._con.execute("COMMIT")

    def registrar_migracao(self, modulo: str, ddl_list: Sequence[str]) -> int:
        """Aplica DDLs do `modulo` ainda nao aplicados (idempotente, por indice+hash). Retorna quantos rodaram."""
        aplicadas = 0
        with self.transacao():
            for i, ddl in enumerate(ddl_list):
                h = hashlib.sha256(ddl.encode()).hexdigest()
                ja = self.consultar("SELECT hash FROM _migracoes WHERE modulo=? AND indice=?", (modulo, i))
                if ja:
                    if ja[0]["hash"] != h:
                        raise ValueError(f"migracao {modulo}#{i} alterada apos aplicada")
                    continue
                self._con.execute(ddl)
                self._con.execute("INSERT INTO _migracoes VALUES(?,?,?,?)", (modulo, i, h, time.time_ns()))
                aplicadas += 1
        return aplicadas

    def fechar(self) -> None:
        with self._lock:
            self._con.close()

    def __enter__(self) -> BancoSQLite:
        return self

    def __exit__(self, *a: object) -> None:
        self.fechar()
