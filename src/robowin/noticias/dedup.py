"""Deduplicacao de noticias: id igual, titulo normalizado igual ou quase igual (Jaccard de palavras)."""
from __future__ import annotations

import re
import unicodedata
from collections import deque

from robowin.core.tipos import EventoNoticia

_STOP = {"de", "da", "do", "das", "dos", "a", "o", "as", "os", "e", "em", "no", "na", "nos", "nas", "para", "por",
         "com", "um", "uma", "the", "of", "to", "in", "and", "on", "for", "says", "diz"}


def normalizar(titulo: str) -> str:
    t = unicodedata.normalize("NFKD", titulo.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^a-z0-9% ]+", " ", t)
    return " ".join(w for w in t.split() if w not in _STOP)


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class Deduplicador:
    """Mantem o primeiro evento (menor t_disponivel) e descarta repeticoes dentro da janela.

    Estado interno limitado pela janela: chamar ``filtrar`` em ordem aproximadamente cronologica.
    """

    def __init__(self, janela_s: int = 6 * 3600, limiar: float = 0.8):
        self.janela_ns = janela_s * 1_000_000_000
        self.limiar = limiar
        self._vistos: deque[tuple[int, str, str, frozenset[str]]] = deque()  # (t_disp, id, norm, palavras)

    def _expirar(self, t_ns: int) -> None:
        while self._vistos and self._vistos[0][0] < t_ns - self.janela_ns:
            self._vistos.popleft()

    def duplicado(self, ev: EventoNoticia) -> bool:
        self._expirar(ev.t_disponivel_ns)
        norm = normalizar(ev.titulo)
        pal = set(norm.split())
        for _, vid, vnorm, vpal in self._vistos:
            if vid == ev.id or vnorm == norm or _jaccard(pal, set(vpal)) >= self.limiar:
                return True
        return False

    def filtrar(self, eventos: list[EventoNoticia]) -> list[EventoNoticia]:
        out: list[EventoNoticia] = []
        for ev in sorted(eventos, key=lambda e: (e.t_disponivel_ns, e.t_publicado_ns, e.id)):
            if self.duplicado(ev):
                continue
            norm = normalizar(ev.titulo)
            self._vistos.append((ev.t_disponivel_ns, ev.id, norm, frozenset(norm.split())))
            out.append(ev)
        return out
