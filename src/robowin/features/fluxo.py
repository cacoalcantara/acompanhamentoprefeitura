"""Fluxo agressor por flags BUY/SELL dos ticks do MT5 e desequilibrio de fluxo."""
from __future__ import annotations

from robowin.core.tipos import Tick

from ._janela import JanelaSoma

NS = 1_000_000_000
TICK_FLAG_LAST = 8
TICK_FLAG_BUY = 32
TICK_FLAG_SELL = 64


def sinal_agressor(flags: int) -> int:
    """+1 compra agressora, -1 venda agressora, 0 desconhecido (sem flag ou ambas)."""
    b = bool(flags & TICK_FLAG_BUY)
    s = bool(flags & TICK_FLAG_SELL)
    if b and not s:
        return 1
    if s and not b:
        return -1
    return 0


def eh_negocio(tick: Tick) -> bool:
    return bool(tick.flags & (TICK_FLAG_BUY | TICK_FLAG_SELL)) or (
        bool(tick.flags & TICK_FLAG_LAST) and tick.volume > 0
    )


def desequilibrio(compra: float, venda: float) -> float:
    tot = compra + venda
    return (compra - venda) / tot if tot > 0 else 0.0


class FluxoAgressor:
    """Desequilibrio do fluxo agressor em janelas (5s, 30s, 120s) + intensidade/volume em 30s."""

    def __init__(self, janelas_s: tuple[int, ...] = (5, 30, 120)):
        self.janelas_s = janelas_s
        # por janela: soma compra agressora, soma venda agressora
        self._imb = {w: JanelaSoma(w * NS, 2) for w in janelas_s}
        self._int = JanelaSoma(30 * NS, 2)  # (negocios, volume)

    def atualizar(self, t_ns: int, tick: Tick) -> None:
        if not eh_negocio(tick):
            return
        vol = tick.volume if tick.volume > 0 else 1.0
        sg = sinal_agressor(tick.flags)
        for j in self._imb.values():
            j.adicionar(t_ns, vol if sg > 0 else 0.0, vol if sg < 0 else 0.0)
        self._int.adicionar(t_ns, 1.0, vol)

    def valores(self, t_ns: int) -> dict[str, float]:
        out: dict[str, float] = {}
        for w, j in self._imb.items():
            c, v = j.somas(t_ns)
            out[f"fluxo_imb_{w}s"] = desequilibrio(c, v)
        n, vol = self._int.somas(t_ns)
        out["fluxo_intensidade_30s"] = n / 30.0
        out["fluxo_volume_30s"] = vol
        return out
