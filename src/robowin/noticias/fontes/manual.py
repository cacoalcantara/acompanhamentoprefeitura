"""Fonte manual: o usuario insere noticias (ou historico para backtest). Sempre ativa (sem rede)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from robowin.core.tipos import EventoNoticia
from robowin.noticias.fontes import FonteBase, id_evento
from robowin.noticias.tempo import FUSO_BRASILIA, para_ns_utc


class FonteManual(FonteBase):
    def __init__(self, nome: str = "manual", *, fuso_origem: str = FUSO_BRASILIA, **kw: Any):
        super().__init__(nome, **kw)
        self.fuso_origem = fuso_origem
        self._fila: list[EventoNoticia] = []

    @property
    def ativa(self) -> bool:  # entrada manual nao usa rede
        return True

    def adicionar(self, titulo: str, publicado: str | int | float | datetime, *,
                  disponivel: str | int | float | datetime | None = None, verificado: bool | None = None) -> EventoNoticia:
        """``publicado``/``disponivel`` sem fuso explicito sao lidos em ``fuso_origem``.
        ``disponivel`` padrao = max(publicado, agora)."""
        t_pub = para_ns_utc(publicado, self.fuso_origem)
        t_rec = self.relogio.agora_ns()
        t_disp = para_ns_utc(disponivel, self.fuso_origem) if disponivel is not None else max(t_pub, t_rec)
        ev = EventoNoticia(
            id=id_evento(self.nome, f"{titulo}|{t_pub}"), fonte=self.nome, titulo=titulo.strip(),
            t_publicado_ns=t_pub, t_disponivel_ns=max(t_disp, t_pub), t_recebido_ns=t_rec,
            verificado=self.verificado if verificado is None else verificado)
        self._fila.append(ev)
        return ev

    def _coletar(self) -> list[EventoNoticia]:
        out, self._fila = self._fila, []
        return out
