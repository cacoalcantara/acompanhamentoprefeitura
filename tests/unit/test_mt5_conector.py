"""ConectorMT5: import tardio e adaptacao ao Protocolo TerminalMT5 (com um modulo MetaTrader5 FALSO)."""
from __future__ import annotations

import sys
import types

import pytest

from robowin.core.protocolos import TerminalMT5
from robowin.mt5.conector import ConectorMT5, MT5IndisponivelErro
from robowin.mt5.duble import DubleMT5


def falso_mt5() -> types.ModuleType:
    m = types.ModuleType("MetaTrader5")
    m.chamadas = []  # type: ignore[attr-defined]
    for nome in ["initialize", "shutdown", "last_error", "account_info", "terminal_info", "symbols_get", "symbol_info",
                 "symbol_info_tick", "symbol_select", "copy_ticks_from", "copy_ticks_range", "market_book_add",
                 "market_book_get", "market_book_release", "order_check", "order_send", "orders_get", "positions_get",
                 "history_orders_get", "history_deals_get"]:
        setattr(m, nome, lambda *a, _n=nome, **k: m.chamadas.append((_n, a, k)) or True)  # type: ignore[attr-defined]
    return m


def test_import_tardio_sem_o_pacote_levanta_erro_claro(monkeypatch):
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)  # simula ausencia (Linux)
    with pytest.raises(MT5IndisponivelErro):
        ConectorMT5()


def test_conector_adapta_o_protocolo_e_nao_vaza_senha(monkeypatch):
    fake = falso_mt5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    c = ConectorMT5(caminho_terminal="C:/mt5/terminal64.exe", login=1, servidor="XPMT5-DEMO", senha="segredo")
    assert isinstance(c, TerminalMT5)
    assert "segredo" not in repr(c)
    assert c.initialize() is True
    nome, _, kw = fake.chamadas[-1]
    assert nome == "initialize" and kw["path"] == "C:/mt5/terminal64.exe" and kw["portable"] is True
    assert kw["login"] == 1 and kw["server"] == "XPMT5-DEMO" and kw["password"] == "segredo"
    c.symbols_get("WIN*")
    c.copy_ticks_from("WINZ26", 1, 10, -1)
    c.positions_get(symbol="WINZ26")
    assert [x[0] for x in fake.chamadas[-3:]] == ["symbols_get", "copy_ticks_from", "positions_get"]
    assert c.order_send is fake.order_send  # encaminhado sem chamada no conector


def test_duble_tambem_satisfaz_o_protocolo():
    assert isinstance(DubleMT5(), TerminalMT5)
