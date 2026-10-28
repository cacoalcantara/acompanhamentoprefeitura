"""Auxiliares dos testes do MT4 (ponte EA): DubleMT4 + ConectorMT4 sobre transporte local. Dados 100% SINTETICOS."""
from __future__ import annotations

from types import SimpleNamespace

from perfis_teste import WIN
from test_mt5_comum import LOGIN, SIMB, T0_MS, T0_NS, cmd, ticks_planos

from robowin.core.relogio import RelogioSimulado
from robowin.mt5.conector_mt4 import ConectorMT4, TransporteLocal
from robowin.mt5.duble import especificacao
from robowin.mt5.duble_mt4 import DubleMT4, _espec_mt4
from robowin.mt5.execucao_demo import ConfigDemo, ExecucaoDemo
from robowin.mt5.tempo_servidor import calibrar

SERVIDOR4 = "BROKERMT4-Demo"

__all__ = ["LOGIN", "SERVIDOR4", "SIMB", "T0_MS", "T0_NS", "cmd", "montar_mt4", "montar_exec4", "ticks_planos"]


def montar_mt4(ticks=None, relogio=None, **kw):
    """(duble_mt4, conector_mt4 ja inicializado, relogio) sem rede (TransporteLocal: mesmo JSON do protocolo)."""
    rel = relogio or RelogioSimulado(T0_NS)
    kw.setdefault("login", LOGIN)
    kw.setdefault("servidor", SERVIDOR4)
    d = DubleMT4(rel, **kw)
    d.adicionar_simbolo(_espec_mt4(especificacao(WIN, SIMB, 0)), ticks if ticks is not None else ticks_planos(3000))
    con = ConectorMT4(transporte=TransporteLocal(d))
    assert con.initialize()
    return d, con, rel


def montar_exec4(ticks=None, cfg_extra=None, **kw):
    d, con, rel = montar_mt4(ticks, **kw)
    rs = calibrar(con, SIMB, rel)
    cfg = ConfigDemo(login=LOGIN, servidor=SERVIDOR4, simbolo=SIMB, conta="hedging", **(cfg_extra or {}))
    alertas: list[tuple[str, dict]] = []
    ex = ExecucaoDemo(con, cfg, rel, alerta=lambda t, x: alertas.append((t, x)), relogio_srv=rs)
    return SimpleNamespace(d=d, con=con, rel=rel, ex=ex, rs=rs, alertas=alertas)
