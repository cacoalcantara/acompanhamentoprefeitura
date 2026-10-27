"""Papeis (proponente, revisor, classificador_nlp, ...) ligados a 'provedor:modelo' pela configuracao."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from robowin.core.config import carregar_toml
from robowin.core.config import papeis as _papeis_cfg
from robowin.core.tipos import Papel
from robowin.ia.catalogo import Catalogo, ErroCatalogo, dividir_modelo

PAPEIS_PADRAO = ("proponente", "revisor", "classificador_nlp")


def carregar_papeis(cfg: dict[str, Any] | str | Path = "config/robowin.toml") -> dict[str, Papel]:
    if not isinstance(cfg, dict):
        cfg = carregar_toml(cfg)
    out = _papeis_cfg(cfg)
    for p in out.values():
        dividir_modelo(p.modelo)  # valida o formato
    return out


def resolver(papeis: dict[str, Papel], nome: str) -> tuple[str, str]:
    """Papel -> (provedor, id_do_modelo)."""
    if nome not in papeis:
        raise ErroCatalogo(f"papel nao configurado: {nome}")
    return dividir_modelo(papeis[nome].modelo)


def validar_papeis(papeis: dict[str, Papel], catalogo: Catalogo) -> None:
    """Falha segura: todo papel precisa apontar para modelo do catalogo (com preco e corte)."""
    catalogo.exigir(p.modelo for p in papeis.values())


def orcamento_mensal_usd(cfg: dict[str, Any]) -> float:
    return float(cfg.get("ia", {}).get("orcamento_mensal_usd", 0.0))
