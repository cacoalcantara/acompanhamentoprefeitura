"""Leitura normalizada do estado da conta MT5."""
from __future__ import annotations

from typing import Any

from robowin.core.tipos import EstadoConta

from . import constantes as C


def ler_estado_conta(terminal: Any) -> EstadoConta | None:
    """Converte account_info/terminal_info em EstadoConta. None se o terminal nao responde."""
    ai = terminal.account_info()
    if ai is None:
        return None
    ti = terminal.terminal_info()
    # modo desconhecido e tratado como real (falha segura)
    modo = C.MODO_CONTA.get(int(getattr(ai, "trade_mode", -1)), "real")
    perm_conta = bool(getattr(ai, "trade_allowed", False))
    perm_term = bool(getattr(ti, "trade_allowed", False)) if ti is not None else False
    margem = int(getattr(ai, "margin_mode", -1))
    return EstadoConta(
        login=int(ai.login),
        servidor=str(ai.server),
        trade_mode=modo,  # type: ignore[arg-type]
        margin_mode=C.NOMES_MARGEM.get(margem, str(margem)),
        trade_allowed=perm_conta and perm_term,
        trade_expert=bool(getattr(ai, "trade_expert", False)),
    )
