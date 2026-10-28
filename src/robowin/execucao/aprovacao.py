"""Impressao (hash) da ordem APROVADA pelo motor de risco: nenhuma ENTRADA sai sem a aprovacao dessa ordem exata.

Ideia adaptada do ``fingerprint`` do trader-skill (MIT: o broker so mostra como pronta a ordem cujo hash passou
no risco HOJE): o veto final do ``MotorRisco`` e dado a uma INTENCAO; a ordem que sai e montada depois
(``Roteador._montar_entrada``: arredonda stop/alvo, escolhe limitada/mercado). Se alguem (um braco novo, uma
politica de execucao, um defeito) mexer no comando entre o veto e o envio, a impressao deixa de bater e a entrada
NAO e enviada. Fecha uma lacuna de "IA/politica sem veto de risco" por construcao.

* so ENTRADAS dependem da aprovacao; SAIDAS (stop, alvo, tempo, zeragem, kill) nunca esperam por ela;
* nao ha aprovacao humana aqui: o envio a conta DEMO continua exigindo as travas de ``mt5/execucao_demo.py`` e a
  confirmacao explicita da pessoa (``--confirmo-conta-demo``); ordens reais nao existem nesta versao.
"""
from __future__ import annotations

import hashlib
import json

from robowin.core.tipos import ComandoOrdem, DecisaoRisco


class ErroOrdemNaoAprovada(Exception):
    """A ordem de entrada nao tem aprovacao do risco com esta mesma impressao."""


_CAMPOS = ("intencao_id", "magic", "lado", "tipo", "quantidade", "preco", "sl", "tp", "position_ticket")


def impressao_ordem(cmd: ComandoOrdem) -> str:
    """SHA-256 (16 primeiros hex) dos campos que definem a ordem; qualquer mudanca altera a impressao."""
    d = cmd.model_dump(mode="json")
    corpo = {k: d.get(k) for k in _CAMPOS}
    return hashlib.sha256(json.dumps(corpo, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


class RegistroAprovacoes:
    """intencao_id -> impressao da ordem aprovada. Memoria de processo (a auditoria grava o veto/aprovacao)."""

    def __init__(self) -> None:
        self._aprovadas: dict[str, str] = {}

    def aprovar(self, cmd: ComandoOrdem, decisao: DecisaoRisco) -> str:
        if not decisao.aprovada or decisao.intencao_id != cmd.intencao_id:
            raise ErroOrdemNaoAprovada("decisao de risco nao aprovada ou de outra intencao")
        imp = impressao_ordem(cmd)
        self._aprovadas[cmd.intencao_id] = imp
        return imp

    def conferir(self, cmd: ComandoOrdem) -> None:
        esperada = self._aprovadas.get(cmd.intencao_id)
        if esperada is None:
            raise ErroOrdemNaoAprovada(f"entrada {cmd.intencao_id} sem aprovacao do motor de risco")
        if esperada != impressao_ordem(cmd):
            raise ErroOrdemNaoAprovada(f"entrada {cmd.intencao_id} diferente da ordem aprovada pelo risco")

    def __len__(self) -> int:
        return len(self._aprovadas)
