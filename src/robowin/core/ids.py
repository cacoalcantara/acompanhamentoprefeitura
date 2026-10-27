import hashlib
import itertools
import json
from typing import Any

_c = itertools.count(1)


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{next(_c):08d}"


def hash_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def hash_arquivo(caminho: str) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()
