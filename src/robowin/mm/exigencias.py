"""Exigencias de capacidade do terminal para o market making (verificador MINIMO, por duck-typing).

TODO(capacidades): quando existir ``robowin.mt5.capacidades.CapacidadesTerminal`` (item 4b, outro agente), trocar
este verificador por ele. Enquanto isso, ``tem_capacidade_livro`` aceita:

* um objeto/dict com atributo/chave ``livro`` (bool), ou com ``capacidades`` que a contenha
  (formato esperado de ``CapacidadesTerminal``);
* o ESTADO publicado pelo gateway: ``capacidades.livro`` quando existir, senao ``livro_profundidade.declarada > 0``
  (o gateway so assina o livro quando o perfil ``aceita_book``).

Sem livro de ofertas (ex.: MT4) NAO ha market making: a ponte recusa ligar.
"""
from __future__ import annotations

from typing import Any


def _ler(obj: Any, nome: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(nome)
    return getattr(obj, nome, None)


def tem_capacidade_livro(fonte: Any) -> bool:
    caps = _ler(fonte, "capacidades")
    for alvo in (caps, fonte):
        if alvo is not None and _ler(alvo, "livro") is not None:
            return bool(_ler(alvo, "livro"))
    prof = _ler(fonte, "livro_profundidade")
    if prof is not None:
        return float(_ler(prof, "declarada") or 0) > 0
    return False  # nao sabemos => nao ha capacidade (falha segura)


def exigir_capacidade_livro(fonte: Any) -> None:
    if not tem_capacidade_livro(fonte):
        raise CapacidadeAusente("o terminal nao oferece livro de ofertas (capacidade 'livro'): market making bloqueado")


class CapacidadeAusente(Exception):
    """Capacidade exigida (ex.: livro) ausente no terminal."""
