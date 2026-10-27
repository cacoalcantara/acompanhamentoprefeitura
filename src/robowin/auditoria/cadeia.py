"""Auditoria JSONL so de acrescimo, com cadeia de hash."""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _h(prev: str, corpo: dict[str, Any]) -> str:
    return hashlib.sha256((prev + json.dumps(corpo, sort_keys=True, ensure_ascii=False, default=str)).encode()).hexdigest()


class RegistradorAuditoria:
    def __init__(self, diretorio: str | Path, relogio: Any = None) -> None:
        self.dir = Path(diretorio)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._rel = relogio
        self._ultimo: dict[str, str] = {}
        self._n: dict[str, int] = {}

    def _arquivo(self, dia: str) -> Path:
        return self.dir / f"auditoria-{dia}.jsonl"

    def registrar(self, tipo: str, dados: dict[str, Any], dia: str | None = None) -> str:
        import datetime as dt
        import time
        t = self._rel.agora_ns() if self._rel else time.time_ns()
        dia = dia or dt.datetime.fromtimestamp(t / 1e9, dt.UTC).strftime("%Y-%m-%d")
        with self._lock:
            arq = self._arquivo(dia)
            prev = self._ultimo.get(dia) or self._ler_ultimo(arq)
            n = self._n.get(dia)
            if n is None:
                n = self._contar(arq)
            corpo = {"n": n, "t_ns": t, "tipo": tipo, "dados": dados, "prev_hash": prev}
            h = _h(prev, corpo)
            with open(arq, "a", encoding="utf-8") as f:
                f.write(json.dumps({**corpo, "hash": h}, ensure_ascii=False, default=str) + "\n")
            self._ultimo[dia] = h
            self._n[dia] = n + 1
            return h

    def _ler_ultimo(self, arq: Path) -> str:
        if not arq.exists():
            return GENESIS
        ult = GENESIS
        with open(arq, encoding="utf-8") as f:
            for linha in f:
                if linha.strip():
                    ult = json.loads(linha)["hash"]
        return ult

    def _contar(self, arq: Path) -> int:
        if not arq.exists():
            return 0
        with open(arq, encoding="utf-8") as f:
            return sum(1 for ln in f if ln.strip())


def verificar_arquivo(arq: str | Path) -> tuple[bool, str]:
    prev = GENESIS
    esperado = 0
    with open(arq, encoding="utf-8") as f:
        for i, linha in enumerate(f):
            if not linha.strip():
                continue
            reg = json.loads(linha)
            h = reg.pop("hash")
            if reg.get("prev_hash") != prev:
                return False, f"linha {i}: prev_hash invalido"
            if reg.get("n") != esperado:
                return False, f"linha {i}: sequencia {reg.get('n')} != {esperado}"
            if _h(prev, reg) != h:
                return False, f"linha {i}: hash invalido"
            prev, esperado = h, esperado + 1
    return True, f"{esperado} registros integros"


def verificar_diretorio(dirpath: str | Path) -> tuple[bool, list[str]]:
    msgs, ok = [], True
    for arq in sorted(Path(dirpath).glob("auditoria-*.jsonl")):
        o, m = verificar_arquivo(arq)
        ok &= o
        msgs.append(f"{arq.name}: {m}")
    return ok, msgs
