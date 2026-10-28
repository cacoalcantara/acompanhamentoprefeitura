"""Consultas SOMENTE LEITURA do terminal, serializadas para o nucleo (o gateway e o unico que toca o MT5).

* ``posicoes_publicas``: posicoes do estado publicado pelo gateway. O ``volume`` do MT5 vem em unidades do
  ativo (acoes: 100 por lote); ``contratos`` = volume / lote do perfil (limite conhecido: o resto da base
  trabalha em contratos/lotes do robo).
* ``snapshot_terminal``: positions_get + orders_get + history_deals_get lidos NA MESMA passada do gateway
  (visao atomica para a reconciliacao). Todos os instantes saem em UTC (ms), ja descontado o offset do servidor,
  e os volumes saem em contratos. Os nomes dos campos sao os do MT5 (``time_msc``, ``reason``, ``entry``...).
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from . import constantes as C
from .tempo_servidor import RelogioServidor


def _lado(tipo: int) -> str:
    return "compra" if int(tipo) == C.POS_COMPRA else "venda"


def _unidade(lote: float) -> float:
    """Volume do terminal por unidade do robo: lote inteiro (B3) ou lote minimo fracionario (forex/CFD: 0.01)."""
    v = float(lote)
    if v >= 1:
        return float(max(int(round(v)), 1)) if abs(v - round(v)) < 1e-9 else v
    return v if v > 0 else 1.0


def posicoes_publicas(posicoes: Iterable[Any], lote: float) -> list[dict[str, Any]]:
    lote = _unidade(lote)
    return [{"ticket": int(p.ticket), "simbolo": str(p.symbol), "lado": _lado(p.type), "volume": float(p.volume),
             "contratos": round(float(p.volume) / lote, 6), "sl": float(p.sl), "tp": float(p.tp), "magic": int(getattr(p, "magic", 0)),
             "preco_abertura": float(getattr(p, "price_open", 0.0)), "lucro": float(getattr(p, "profit", 0.0))}
            for p in posicoes]


def snapshot_terminal(terminal: Any, rs: RelogioServidor, lote: float, desde_ns: int, ate_ns: int) -> dict[str, Any]:
    """Lista posicoes e ordens da conta e os deals de ``desde_ns`` a ``ate_ns`` (UTC). ``ok=False`` se o terminal calar."""
    lote = _unidade(lote)
    ps = terminal.positions_get()
    os_ = terminal.orders_get()
    ds = terminal.history_deals_get(rs.utc_ns_para_dt_servidor(max(int(desde_ns), 0)), rs.utc_ns_para_dt_servidor(int(ate_ns)))
    if ps is None or os_ is None or ds is None:
        return {"ok": False, "positions": [], "orders": [], "deals": []}

    def utc_ms(ms: int) -> int:
        return int(ms) - rs.offset_ms

    positions = [{"ticket": int(p.ticket), "symbol": str(p.symbol), "type": int(p.type), "magic": int(p.magic),
                  "volume": round(float(p.volume) / lote, 6), "volume_bruto": float(p.volume), "price_open": float(p.price_open),
                  "sl": float(p.sl), "tp": float(p.tp), "profit": float(p.profit), "time_msc": utc_ms(p.time_msc),
                  "reason": int(getattr(p, "reason", C.RAZAO_EXPERT)), "identifier": int(getattr(p, "identifier", p.ticket))}
                 for p in ps]
    orders = [{"ticket": int(o.ticket), "symbol": str(o.symbol), "type": int(o.type), "magic": int(o.magic),
               "volume_current": round(float(o.volume_current) / lote, 6), "price_open": float(o.price_open),
               "sl": float(o.sl), "tp": float(o.tp), "time_setup_msc": utc_ms(o.time_setup_msc)} for o in os_]
    deals = [{"ticket": int(d.ticket), "order": int(d.order), "position_id": int(d.position_id), "time_msc": utc_ms(d.time_msc),
              "type": int(d.type), "entry": int(d.entry), "magic": int(d.magic), "reason": int(d.reason),
              "volume": round(float(d.volume) / lote, 6), "price": float(d.price), "profit": float(d.profit),
              "commission": float(d.commission), "swap": float(d.swap), "fee": float(getattr(d, "fee", 0.0)),
              "symbol": str(d.symbol)} for d in ds]
    return {"ok": True, "positions": positions, "orders": orders, "deals": deals, "lote": lote,
            "offset_ms": rs.offset_ms}
