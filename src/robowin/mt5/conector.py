"""Conector real: adapta o pacote oficial MetaTrader5 ao Protocolo `TerminalMT5`.

O pacote so existe no Windows, entao o `import MetaTrader5` e TARDIO: so ocorre quando o conector e
instanciado. Em Linux/CI use `DubleMT5` (mt5/duble.py).

A senha nunca e registrada em log; vem do argumento ou da variavel de ambiente `ROBOWIN_MT5_SENHA`.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any


class MT5IndisponivelErro(RuntimeError):
    """O pacote MetaTrader5 nao esta instalado (ou nao e Windows)."""


class ConectorMT5:
    def __init__(
        self,
        caminho_terminal: str | None = None,
        login: int | None = None,
        servidor: str | None = None,
        senha: str | None = None,
        portatil: bool = True,
        timeout_ms: int = 30_000,
    ) -> None:
        try:
            import MetaTrader5 as mt5  # import tardio: so existe no Windows
        except ImportError as e:  # pragma: no cover - depende da plataforma
            raise MT5IndisponivelErro(
                "pacote MetaTrader5 indisponivel (instale o extra 'mt5' no Windows)"
            ) from e
        self._mt5 = mt5
        self._params: dict[str, Any] = {"timeout": timeout_ms, "portable": portatil}
        if caminho_terminal:
            self._params["path"] = caminho_terminal
        if login is not None:
            self._params["login"] = int(login)
        if servidor:
            self._params["server"] = servidor
        senha = senha or os.environ.get("ROBOWIN_MT5_SENHA")
        if senha:
            self._params["password"] = senha
        # Encaminhamento SEM chamada: o unico CHAMADOR de order_send no projeto e execucao_demo.py
        # (garantido por teste estatico por AST).
        self.order_send = mt5.order_send

    def __repr__(self) -> str:  # nunca expor a senha
        p = {k: v for k, v in self._params.items() if k != "password"}
        return f"ConectorMT5({p})"

    # --- ciclo de vida -------------------------------------------------------------------------
    def initialize(self, **kw: Any) -> bool:
        return bool(self._mt5.initialize(**{**self._params, **kw}))

    def shutdown(self) -> None:
        self._mt5.shutdown()

    def last_error(self) -> tuple[int, str]:
        return self._mt5.last_error()

    # --- conta e terminal ----------------------------------------------------------------------
    def account_info(self) -> Any:
        return self._mt5.account_info()

    def terminal_info(self) -> Any:
        return self._mt5.terminal_info()

    # --- simbolos ------------------------------------------------------------------------------
    def symbols_get(self, group: str | None = None) -> Sequence[Any] | None:
        return self._mt5.symbols_get(group=group) if group else self._mt5.symbols_get()

    def symbol_info(self, symbol: str) -> Any:
        return self._mt5.symbol_info(symbol)

    def symbol_info_tick(self, symbol: str) -> Any:
        return self._mt5.symbol_info_tick(symbol)

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        return bool(self._mt5.symbol_select(symbol, enable))

    # --- dados de mercado ----------------------------------------------------------------------
    def copy_ticks_from(self, symbol: str, date_from: Any, count: int, flags: int) -> Any:
        return self._mt5.copy_ticks_from(symbol, date_from, count, flags)

    def copy_ticks_range(self, symbol: str, date_from: Any, date_to: Any, flags: int) -> Any:
        return self._mt5.copy_ticks_range(symbol, date_from, date_to, flags)

    def market_book_add(self, symbol: str) -> bool:
        return bool(self._mt5.market_book_add(symbol))

    def market_book_get(self, symbol: str) -> Any:
        return self._mt5.market_book_get(symbol)

    def market_book_release(self, symbol: str) -> bool:
        return bool(self._mt5.market_book_release(symbol))

    # --- ordens (leitura; o envio e o atributo `order_send` definido no __init__) ---------------
    def order_check(self, request: dict[str, Any]) -> Any:
        return self._mt5.order_check(request)

    def orders_get(self, **kw: Any) -> Any:
        return self._mt5.orders_get(**kw)

    def positions_get(self, **kw: Any) -> Any:
        return self._mt5.positions_get(**kw)

    def history_orders_get(self, *a: Any, **kw: Any) -> Any:
        return self._mt5.history_orders_get(*a, **kw)

    def history_deals_get(self, *a: Any, **kw: Any) -> Any:
        return self._mt5.history_deals_get(*a, **kw)
