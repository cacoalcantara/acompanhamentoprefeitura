"""Carrega as fontes de config/fontes.toml. Fonte nao verificada fica desligada (nao e nem instanciada)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from robowin.core.config import carregar_toml
from robowin.core.protocolos import FonteNoticias
from robowin.core.relogio import Relogio
from robowin.noticias.fontes import Transporte
from robowin.noticias.fontes.http_json import FonteHttpJson
from robowin.noticias.fontes.rss import FonteRSS

log = logging.getLogger("robowin.noticias")


def carregar_fontes(cfg: dict[str, Any] | str | Path = "config/fontes.toml", *, relogio: Relogio | None = None,
                    transporte: Transporte | None = None, permitir_nao_verificadas: bool = False) -> list[FonteNoticias]:
    if not isinstance(cfg, dict):
        cfg = carregar_toml(cfg)
    out: list[FonteNoticias] = []
    for f in cfg.get("fonte", []):
        verificado = bool(f.get("verificado", False))
        if not verificado and not permitir_nao_verificadas:
            log.info("fonte %s nao verificada: ignorada", f.get("nome"))
            continue
        comum = {"verificado": verificado, "permitir_nao_verificada": permitir_nao_verificadas and not verificado, "relogio": relogio, "transporte": transporte,
                 "fuso_origem": f.get("fuso_origem", "America/Sao_Paulo")}
        if f["tipo"] == "rss":
            out.append(FonteRSS(f["nome"], f["url"], **comum))
        elif f["tipo"] == "http_json":
            campos = {k: f[k] for k in ("campo_lista", "campo_titulo", "campo_data", "campo_id", "formato_data") if k in f}
            out.append(FonteHttpJson(f["nome"], f["url"], **campos, **comum))
        else:
            raise ValueError(f"tipo de fonte desconhecido: {f['tipo']}")
    return out
