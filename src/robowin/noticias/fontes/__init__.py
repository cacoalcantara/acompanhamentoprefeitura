"""Fontes de noticias. Todas implementam ``FonteNoticias`` (core.protocolos).

Regras comuns:
- ``verificado=False`` por padrao: fonte NAO verificada fica DESLIGADA (``coletar()`` devolve []) ate que
  o usuario a verifique e marque ``verificado = true`` (ou ligue explicitamente ``permitir_nao_verificada``);
- cada evento carrega ``t_publicado_ns`` (da fonte, convertido do fuso de origem), ``t_recebido_ns`` (relogio
  local) e ``t_disponivel_ns = max(publicado, recebido)``: so ha informacao quando ela chegou;
- nenhuma fonte e chamada por rede nos testes: o transporte e injetado.
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable

from robowin.core.relogio import Relogio, RelogioReal
from robowin.core.tipos import EventoNoticia

log = logging.getLogger("robowin.noticias")

Transporte = Callable[[str], str]  # url -> corpo (texto)


def id_evento(fonte: str, chave: str) -> str:
    return hashlib.sha256(f"{fonte}|{chave}".encode()).hexdigest()[:16]


class FonteBase:
    nome: str
    verificado: bool

    def __init__(self, nome: str, *, verificado: bool = False, permitir_nao_verificada: bool = False,
                 relogio: Relogio | None = None):
        self.nome = nome
        self.verificado = verificado
        self.permitir_nao_verificada = permitir_nao_verificada
        self.relogio = relogio or RelogioReal()

    @property
    def ativa(self) -> bool:
        return self.verificado or self.permitir_nao_verificada

    def _evento(self, chave: str, titulo: str, t_publicado_ns: int, t_recebido_ns: int) -> EventoNoticia:
        return EventoNoticia(
            id=id_evento(self.nome, chave), fonte=self.nome, titulo=titulo.strip(),
            t_publicado_ns=t_publicado_ns, t_recebido_ns=t_recebido_ns,
            t_disponivel_ns=max(t_publicado_ns, t_recebido_ns), verificado=self.verificado)

    def coletar(self) -> list[EventoNoticia]:
        if not self.ativa:
            log.info("fonte %s nao verificada: desligada", self.nome)
            return []
        return self._coletar()

    def _coletar(self) -> list[EventoNoticia]:  # pragma: no cover - abstrato
        raise NotImplementedError


def transporte_http(timeout_s: float = 10.0) -> Transporte:
    """Transporte padrao (httpx). Importado sob demanda; nunca usado nos testes."""
    def get(url: str) -> str:
        import httpx

        r = httpx.get(url, timeout=timeout_s, follow_redirects=True)
        r.raise_for_status()
        return r.text
    return get
