"""Auxiliares compartilhados pelos testes do modulo mt5 (sem testes proprios). Dados 100% SINTETICOS."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import ComandoOrdem, Lado
from robowin.mt5 import constantes as C
from robowin.mt5.duble import (
    DubleMT5,
    especificacao_win,
    gerar_livros_sinteticos,
    gerar_ticks_sinteticos,
)
from robowin.mt5.execucao_demo import ConfigDemo, ExecucaoDemo
from robowin.mt5.tempo_servidor import calibrar

SP = dt.timezone(dt.timedelta(hours=-3))
# terca-feira 27/10/2026, 12:00 em Sao Paulo (dentro da janela de entradas)
T0_NS = int(dt.datetime(2026, 10, 27, 12, 0, tzinfo=SP).timestamp() * 1e9)
T0_MS = T0_NS // 1_000_000
LOGIN, SERVIDOR = 900001, "XPMT5-DEMO"
OFFSET_S = 10800
SIMB = "WINZ26"


def ticks_planos(n: int = 400, preco: float = 100000.0, passo_ms: int = 100, spread: float = 5.0,
                 inicio_ms: int | None = None) -> list[tuple]:
    """Serie constante (bid/ask fixos) para testes deterministicos."""
    ini = (inicio_ms if inicio_ms is not None else T0_MS - 1000)
    return [(ini + i * passo_ms, preco, preco + spread, preco, 0.0, C.FLAG_BID | C.FLAG_ASK) for i in range(n)]


def montar_duble(ticks=None, livros=None, relogio=None, **kw) -> tuple[DubleMT5, RelogioSimulado]:
    rel = relogio or RelogioSimulado(T0_NS)
    kw.setdefault("login", LOGIN)
    kw.setdefault("servidor", SERVIDOR)
    kw.setdefault("offset_servidor_s", OFFSET_S)
    d = DubleMT5(rel, **kw)
    venc = int(dt.datetime(2026, 12, 16, 21, tzinfo=dt.UTC).timestamp())
    tk = ticks if ticks is not None else gerar_ticks_sinteticos(T0_MS - 1000, 3000, 100, semente=3)
    d.adicionar_simbolo(especificacao_win(SIMB, venc), tk, livros if livros is not None else gerar_livros_sinteticos(tk, 10, 5))
    d.adicionar_simbolo(especificacao_win("WING27", int(dt.datetime(2027, 2, 17, 21, tzinfo=dt.UTC).timestamp())),
                        gerar_ticks_sinteticos(T0_MS - 1000, 20, 1000, 100500.0, 9))
    d.initialize()
    return d, rel


def montar_exec(ticks=None, relogio=None, cfg_extra=None, **kw):
    d, rel = montar_duble(ticks, relogio=relogio, **kw)
    rs = calibrar(d, SIMB, rel)
    cfg = ConfigDemo(login=LOGIN, servidor=SERVIDOR, simbolo=SIMB, **(cfg_extra or {}))
    alertas: list[tuple[str, dict]] = []
    ex = ExecucaoDemo(d, cfg, rel, alerta=lambda t, x: alertas.append((t, x)), relogio_srv=rs)
    return SimpleNamespace(d=d, rel=rel, ex=ex, rs=rs, alertas=alertas)


_n = [0]


def cmd(lado: Lado, tipo: str = "mercado", ticket: int | None = None, sl: float | None = None, tp: float | None = None,
        preco: float | None = None, magic: int = 7, iid: str | None = None, qtd: int = 1) -> ComandoOrdem:
    _n[0] += 1
    return ComandoOrdem(intencao_id=iid or f"i{_n[0]:06d}", magic=magic, lado=lado, tipo=tipo, quantidade=qtd,
                        preco=preco, sl=sl, tp=tp, position_ticket=ticket)

