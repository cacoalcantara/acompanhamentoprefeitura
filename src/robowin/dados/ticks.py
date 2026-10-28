"""ArmazenamentoTicks: Parquet zstd por simbolo/data (America/Sao_Paulo), leitura por DuckDB."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections.abc import Sequence
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from robowin.core.calendario_b3 import SP
from robowin.core.tipos import LivroSnapshot, NivelBook, Tick

COLS_TICK = ["t_servidor_ms", "t_utc_ns", "t_recebido_ns", "bid", "ask", "last", "volume", "flags"]
CHAVE_DEDUP = ["t_servidor_ms", "bid", "ask", "last", "volume"]
_TIPOS_TICK = dict(zip(COLS_TICK, ["int64"] * 3 + ["float64"] * 4 + ["int64"], strict=True))
_SCHEMA_TICK = pa.schema([
    ("t_servidor_ms", pa.int64()), ("t_utc_ns", pa.int64()), ("t_recebido_ns", pa.int64()),
    ("bid", pa.float64()), ("ask", pa.float64()), ("last", pa.float64()), ("volume", pa.float64()),
    ("flags", pa.int64()),
])
_SCHEMA_LIVRO = pa.schema([("t_utc_ns", pa.int64()), ("niveis", pa.string())])
MARCADOR_SINTETICO = "SINTETICO.txt"
MARCADOR_REAL = "REAL.txt"  # dados de mercado REAIS (ex.: negocios publicos da B3); nunca misturar com sinteticos


class MisturaDeDados(ValueError):
    """Tentativa de misturar dados SINTETICOS e REAIS no mesmo armazem (recusada: separe os diretorios)."""


def data_sp(t_ns: int) -> dt.date:
    return dt.datetime.fromtimestamp(t_ns / 1e9, SP).date()


def dia_ns(d: dt.date) -> int:
    """Meia-noite de `d` em America/Sao_Paulo, em ns UTC."""
    return int(dt.datetime(d.year, d.month, d.day, tzinfo=SP).timestamp()) * 1_000_000_000


class ArmazenamentoTicks:
    """Implementa `ArmazemTicks`. Intervalo de leitura: [de_ns, ate_ns) em t_utc_ns."""

    def __init__(self, raiz: str | Path) -> None:
        self.raiz = Path(raiz)
        self.raiz.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # -- caminhos -----------------------------------------------------------
    def _dir(self, tipo: str, simbolo: str) -> Path:
        if not simbolo or "/" in simbolo or "\\" in simbolo or simbolo.startswith("."):
            raise ValueError(f"simbolo invalido: {simbolo!r}")
        return self.raiz / tipo / simbolo

    def _arq(self, tipo: str, simbolo: str, d: dt.date) -> Path:
        return self._dir(tipo, simbolo) / f"data={d.isoformat()}" / "part.parquet"

    @staticmethod
    def _escrever(df: pd.DataFrame, schema: pa.Schema, destino: Path) -> None:
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(f".tmp{os.getpid()}_{threading.get_ident()}")
        pq.write_table(pa.Table.from_pandas(df, schema=schema, preserve_index=False), tmp, compression="zstd")
        os.replace(tmp, destino)

    # -- ticks --------------------------------------------------------------
    def gravar(self, simbolo: str, ticks: Sequence[Tick]) -> int:
        """Grava ticks (idempotente, dedup por chave). Retorna quantos ticks NOVOS entraram."""
        if not ticks:
            return 0
        novo = pd.DataFrame({c: [getattr(t, c) for t in ticks] for c in COLS_TICK}).astype(_TIPOS_TICK)
        dias = pd.to_datetime(novo["t_utc_ns"], unit="ns", utc=True).dt.tz_convert(SP).dt.date
        total = 0
        with self._lock:
            for d, grupo in novo.groupby(dias, sort=True):
                arq = self._arq("ticks", simbolo, d)
                antes = pd.read_parquet(arq) if arq.exists() else None
                n_antes = 0 if antes is None else len(antes)
                tudo = grupo if antes is None else pd.concat([antes, grupo], ignore_index=True)
                tudo = tudo.astype(_TIPOS_TICK).drop_duplicates(subset=CHAVE_DEDUP, keep="first")
                tudo = tudo.sort_values(["t_utc_ns", "t_servidor_ms", "t_recebido_ns"], kind="mergesort")
                if antes is None or len(tudo) != n_antes:
                    self._escrever(tudo.reset_index(drop=True), _SCHEMA_TICK, arq)
                total += len(tudo) - n_antes
        return total

    def _arquivos(self, tipo: str, simbolo: str, de_ns: int, ate_ns: int) -> list[str]:
        if ate_ns <= de_ns:
            return []
        d, d1 = data_sp(de_ns), data_sp(ate_ns - 1)
        out = []
        while d <= d1:
            a = self._arq(tipo, simbolo, d)
            if a.exists():
                out.append(str(a))
            d += dt.timedelta(days=1)
        return out

    def ler(self, simbolo: str, de_ns: int, ate_ns: int) -> list[Tick]:
        arqs = self._arquivos("ticks", simbolo, de_ns, ate_ns)
        if not arqs:
            return []
        with duckdb.connect() as con:
            linhas = con.execute(
                f"SELECT {','.join(COLS_TICK)} FROM read_parquet(?) WHERE t_utc_ns >= ? AND t_utc_ns < ? "
                "ORDER BY t_utc_ns, t_servidor_ms, t_recebido_ns, bid, ask, last, volume",
                [arqs, de_ns, ate_ns]).fetchall()
        return [Tick.model_construct(**dict(zip(COLS_TICK, ln, strict=True))) for ln in linhas]

    def ler_dia(self, simbolo: str, d: dt.date) -> list[Tick]:
        return self.ler(simbolo, dia_ns(d), dia_ns(d + dt.timedelta(days=1)))

    # -- livro --------------------------------------------------------------
    @staticmethod
    def _ser(liv: LivroSnapshot) -> str:
        return json.dumps([[n.preco, n.volume, n.lado] for n in liv.niveis])

    def gravar_livros(self, simbolo: str, livros: Sequence[LivroSnapshot]) -> int:
        """Grava snapshots do livro (tabela separada; niveis serializados). Idempotente."""
        if not livros:
            return 0
        novo = pd.DataFrame({"t_utc_ns": [x.t_utc_ns for x in livros], "niveis": [self._ser(x) for x in livros]})
        dias = pd.to_datetime(novo["t_utc_ns"], unit="ns", utc=True).dt.tz_convert(SP).dt.date
        total = 0
        with self._lock:
            for d, grupo in novo.groupby(dias, sort=True):
                arq = self._arq("livro", simbolo, d)
                antes = pd.read_parquet(arq) if arq.exists() else None
                n_antes = 0 if antes is None else len(antes)
                tudo = grupo if antes is None else pd.concat([antes, grupo], ignore_index=True)
                tudo = tudo.astype({"t_utc_ns": "int64"}).drop_duplicates(keep="first")
                tudo = tudo.sort_values(["t_utc_ns", "niveis"], kind="mergesort")
                if antes is None or len(tudo) != n_antes:
                    self._escrever(tudo.reset_index(drop=True), _SCHEMA_LIVRO, arq)
                total += len(tudo) - n_antes
        return total

    def ler_livros(self, simbolo: str, de_ns: int, ate_ns: int) -> list[LivroSnapshot]:
        arqs = self._arquivos("livro", simbolo, de_ns, ate_ns)
        if not arqs:
            return []
        with duckdb.connect() as con:
            linhas = con.execute(
                "SELECT t_utc_ns, niveis FROM read_parquet(?) WHERE t_utc_ns >= ? AND t_utc_ns < ? "
                "ORDER BY t_utc_ns, niveis", [arqs, de_ns, ate_ns]).fetchall()
        return [LivroSnapshot(t_utc_ns=t, niveis=tuple(NivelBook(preco=p, volume=v, lado=ld)
                                                        for p, v, ld in json.loads(s))) for t, s in linhas]

    # -- offset do servidor ---------------------------------------------------
    def _arq_offset(self, simbolo: str) -> Path:
        return self._dir("meta", simbolo) / "offset_servidor.json"

    def gravar_offset_servidor(self, simbolo: str, offset_ms: int) -> None:
        """Offset (ms) do relogio do servidor MT5 em relacao ao UTC, calibrado pelo gateway."""
        with self._lock:
            a = self._arq_offset(simbolo)
            a.parent.mkdir(parents=True, exist_ok=True)
            a.write_text(json.dumps({"offset_ms": int(offset_ms)}), encoding="utf-8")

    def ler_offset_servidor(self, simbolo: str) -> int | None:
        a = self._arq_offset(simbolo)
        return int(json.loads(a.read_text(encoding="utf-8"))["offset_ms"]) if a.exists() else None

    # -- inventario ---------------------------------------------------------------
    def simbolos(self, tipo: str = "ticks") -> list[str]:
        base = self.raiz / tipo
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.exists() else []

    def datas(self, simbolo: str, tipo: str = "ticks") -> list[dt.date]:
        base = self._dir(tipo, simbolo)
        if not base.exists():
            return []
        return sorted(dt.date.fromisoformat(p.name.split("=", 1)[1]) for p in base.glob("data=*")
                      if (p / "part.parquet").exists())

    def marcar_sintetico(self) -> None:
        if self.eh_real:
            raise MisturaDeDados(f"{self.raiz} contem dados REAIS ({MARCADOR_REAL}): nao gravo dados sinteticos aqui")
        (self.raiz / MARCADOR_SINTETICO).write_text("DADOS SINTETICOS - nao sao dados de mercado\n", encoding="utf-8")

    @property
    def eh_sintetico(self) -> bool:
        return (self.raiz / MARCADOR_SINTETICO).exists()

    def marcar_real(self, origem: str) -> None:
        """Marca o armazem como de dados REAIS (`origem`, ex. 'real_b3_negocios'). Recusa armazem sintetico."""
        sint = [s for s in self.simbolos() if s.startswith("SINT_")]
        if self.eh_sintetico or sint:
            raise MisturaDeDados(f"{self.raiz} contem dados SINTETICOS ({MARCADOR_SINTETICO} ou simbolos {sint[:3]}): "
                                 "nao misturo dados reais com sinteticos; use outro diretorio")
        a = self.raiz / MARCADOR_REAL
        origens = set(self.origens_reais())
        if origem not in origens:
            origens.add(origem)
            a.write_text("DADOS REAIS DE MERCADO - origens: " + ",".join(sorted(origens)) + "\n", encoding="utf-8")

    @property
    def eh_real(self) -> bool:
        return (self.raiz / MARCADOR_REAL).exists()

    def origens_reais(self) -> list[str]:
        a = self.raiz / MARCADOR_REAL
        if not a.exists():
            return []
        txt = a.read_text(encoding="utf-8").split("origens:", 1)
        return [o.strip() for o in txt[1].split(",") if o.strip()] if len(txt) > 1 else []

    # -- tabela (vetorizado: milhoes de linhas sem criar objetos Tick) -------------------------------
    def gravar_tabela(self, simbolo: str, df: pd.DataFrame) -> int:
        """Grava um DataFrame com as colunas de `COLS_TICK`, SUBSTITUINDO cada dia presente (reimportar e
        idempotente). Sem dedup: negocios distintos com a mesma chave sao legitimos. Retorna as linhas gravadas."""
        falta = [c for c in COLS_TICK if c not in df.columns]
        if falta:
            raise ValueError(f"colunas ausentes: {falta}")
        if df.empty:
            return 0
        tab = df[COLS_TICK].astype(_TIPOS_TICK)
        dias = pd.to_datetime(tab["t_utc_ns"], unit="ns", utc=True).dt.tz_convert(SP).dt.date
        with self._lock:
            for d, grupo in tab.groupby(dias, sort=True):
                g = grupo.sort_values(["t_utc_ns", "t_recebido_ns"], kind="mergesort").reset_index(drop=True)
                self._escrever(g, _SCHEMA_TICK, self._arq("ticks", simbolo, d))
        return len(tab)

    def ler_tabela(self, simbolo: str, d: dt.date) -> pd.DataFrame:
        """Ticks do dia `d` como DataFrame (colunas de `COLS_TICK`, ordem do arquivo); vazio se nao houver."""
        a = self._arq("ticks", simbolo, d)
        if not a.exists():
            return pd.DataFrame({c: pd.Series(dtype=t) for c, t in _TIPOS_TICK.items()})
        return pd.read_parquet(a, columns=COLS_TICK)

    # -- metadados por simbolo (JSON) --------------------------------------------------------------
    def gravar_meta(self, simbolo: str, nome: str, conteudo: dict) -> Path:
        if not nome or "/" in nome or "\\" in nome or nome.startswith("."):
            raise ValueError(f"nome de metadado invalido: {nome!r}")
        with self._lock:
            a = self._dir("meta", simbolo) / f"{nome}.json"
            a.parent.mkdir(parents=True, exist_ok=True)
            a.write_text(json.dumps(conteudo, ensure_ascii=False, indent=2, sort_keys=True, default=str),
                         encoding="utf-8")
            return a

    def ler_meta(self, simbolo: str, nome: str) -> dict | None:
        a = self._dir("meta", simbolo) / f"{nome}.json"
        return json.loads(a.read_text(encoding="utf-8")) if a.exists() else None
