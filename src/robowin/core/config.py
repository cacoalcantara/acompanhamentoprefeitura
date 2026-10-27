"""Configuracao TOML com hash; politica de risco travada por aprovacao."""
from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from .tipos import ConfigRisco, Papel


class ErroConfig(Exception):
    pass


def carregar_toml(caminho: str | Path) -> dict[str, Any]:
    with open(caminho, "rb") as f:
        return tomllib.load(f)


def sha256_arquivo(caminho: str | Path) -> str:
    return hashlib.sha256(Path(caminho).read_bytes()).hexdigest()


def carregar_risco(caminho: str | Path, aprovacao: str | Path | None) -> ConfigRisco:
    """Recusa iniciar se o hash do arquivo de risco diferir do ultimo registro de aprovacao."""
    cfg = ConfigRisco(**carregar_toml(caminho))
    if aprovacao is None:
        raise ErroConfig("sem registro de aprovacao da politica de risco")
    reg = carregar_toml(aprovacao)
    if reg.get("sha256") != sha256_arquivo(caminho):
        raise ErroConfig("hash da politica de risco difere do registro de aprovacao")
    return cfg


def papeis(cfg: dict[str, Any]) -> dict[str, Papel]:
    out: dict[str, Papel] = {}
    for nome, p in cfg.get("ia", {}).get("papeis", {}).items():
        out[nome] = Papel(nome=nome, modelo=p["modelo"], effort=p.get("effort"), parametros=p.get("parametros", {}))
    return out
