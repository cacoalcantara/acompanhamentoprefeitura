"""Fonte RSS 2.0 / Atom."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

from robowin.core.tipos import EventoNoticia
from robowin.noticias.fontes import FonteBase, Transporte, transporte_http
from robowin.noticias.tempo import FUSO_BRASILIA, para_ns_utc

log = logging.getLogger("robowin.noticias")
_ATOM = "{http://www.w3.org/2005/Atom}"


class FonteRSS(FonteBase):
    def __init__(self, nome: str, url: str, *, fuso_origem: str = FUSO_BRASILIA, transporte: Transporte | None = None,
                 **kw: Any):
        super().__init__(nome, **kw)
        self.url = url
        self.fuso_origem = fuso_origem
        self._transporte = transporte

    def _coletar(self) -> list[EventoNoticia]:
        texto = (self._transporte or transporte_http())(self.url)
        return self.interpretar(texto)

    def interpretar(self, texto: str) -> list[EventoNoticia]:
        t_rec = self.relogio.agora_ns()
        try:
            raiz = ET.fromstring(texto)
        except ET.ParseError as e:
            log.warning("RSS invalido em %s: %s", self.nome, e)
            return []
        out: list[EventoNoticia] = []
        for it in list(raiz.iter("item")) + list(raiz.iter(f"{_ATOM}entry")):
            titulo = (it.findtext("title") or it.findtext(f"{_ATOM}title") or "").strip()
            data = (it.findtext("pubDate") or it.findtext(f"{_ATOM}updated") or it.findtext(f"{_ATOM}published")
                    or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
            if not titulo or not data:
                continue  # sem data confiavel nao ha como evitar vazamento: descarta
            try:
                t_pub = para_ns_utc(data, self.fuso_origem)
            except (ValueError, TypeError):
                log.warning("data invalida em %s: %r", self.nome, data)
                continue
            chave = it.findtext("guid") or it.findtext(f"{_ATOM}id") or f"{titulo}|{t_pub}"
            out.append(self._evento(chave, titulo, t_pub, t_rec))
        return out
