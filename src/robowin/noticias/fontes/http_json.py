"""Fonte HTTP/JSON generica: mapeia campos por caminho pontuado."""
from __future__ import annotations

import json
import logging
from typing import Any

from robowin.core.tipos import EventoNoticia
from robowin.noticias.fontes import FonteBase, Transporte, transporte_http
from robowin.noticias.tempo import FUSO_BRASILIA, para_ns_utc

log = logging.getLogger("robowin.noticias")


def _pegar(d: Any, caminho: str) -> Any:
    for parte in caminho.split(".") if caminho else []:
        if isinstance(d, dict):
            d = d.get(parte)
        elif isinstance(d, list) and parte.isdigit() and int(parte) < len(d):
            d = d[int(parte)]
        else:
            return None
    return d


class FonteHttpJson(FonteBase):
    def __init__(self, nome: str, url: str, *, campo_lista: str = "", campo_titulo: str = "titulo",
                 campo_data: str = "data", campo_id: str = "id", formato_data: str | None = None,
                 fuso_origem: str = FUSO_BRASILIA, transporte: Transporte | None = None, **kw: Any):
        super().__init__(nome, **kw)
        self.url, self.campo_lista, self.campo_titulo = url, campo_lista, campo_titulo
        self.campo_data, self.campo_id, self.formato_data = campo_data, campo_id, formato_data
        self.fuso_origem = fuso_origem
        self._transporte = transporte

    def _coletar(self) -> list[EventoNoticia]:
        return self.interpretar((self._transporte or transporte_http())(self.url))

    def interpretar(self, texto: str) -> list[EventoNoticia]:
        t_rec = self.relogio.agora_ns()
        try:
            dados = json.loads(texto)
        except ValueError as e:
            log.warning("JSON invalido em %s: %s", self.nome, e)
            return []
        itens = _pegar(dados, self.campo_lista) if self.campo_lista else dados
        if not isinstance(itens, list):
            return []
        out: list[EventoNoticia] = []
        for it in itens:
            titulo, data = _pegar(it, self.campo_titulo), _pegar(it, self.campo_data)
            if not titulo or data is None:
                continue
            try:
                t_pub = para_ns_utc(data, self.fuso_origem, self.formato_data)
            except (ValueError, TypeError):
                log.warning("data invalida em %s: %r", self.nome, data)
                continue
            chave = str(_pegar(it, self.campo_id) or f"{titulo}|{t_pub}")
            out.append(self._evento(chave, str(titulo), t_pub, t_rec))
        return out
