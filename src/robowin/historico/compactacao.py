"""Compactacao CAUSAL de negocios por janela (para milhoes de negocios por dia caberem em minutos no replay).

A grade de janelas e FIXA (``t_disponivel // janela``, definida antes dos dados). Dentro de cada janela:

* sai intacto, no proprio instante, o 1o negocio da janela e todo negocio que faz NOVA MAXIMA ou NOVA MINIMA da
  janela ate ali (decisao que so usa o passado da janela): o caminho de extremos chega na hora certa, e stops/alvos
  veem os mesmos topos e fundos;
* os demais negocios da janela (os que nao sairam) sao somados e entregues no FIM da janela, separados por lado
  inferido (um tick de compra e um de venda, no maximo; o desconhecido vai num terceiro), com o preco e a cotacao
  do ULTIMO deles. O fim da janela e anterior ao proximo negocio, logo nada chega antes de existir e nada revela o
  futuro: perturbar negocios a partir de T nao muda nenhum evento anterior a T (testado).

O que se perde: o instante exato (no maximo ``janela_ms`` de atraso) do volume e dos precos intermediarios que nao
sao extremos da janela. O desequilibrio de fluxo agressor por lado e o volume total ficam exatos. Custo: no maximo
(extremos + 3) eventos por janela. ``janela_ms <= 0`` desliga (replay negocio a negocio).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from robowin.core.tipos import Tick
from robowin.dados.ticks import COLS_TICK
from robowin.features.fluxo import TICK_FLAG_BUY, TICK_FLAG_SELL, sinal_agressor


@dataclass(frozen=True)
class EstatisticaCompactacao:
    entrada: int
    saida: int
    janela_ms: float

    @property
    def fator(self) -> float:
        return self.entrada / self.saida if self.saida else 1.0


def disponibilidade_ns(tab: pd.DataFrame) -> np.ndarray:
    """Instante em que cada tick fica disponivel: max(t_utc_ns, t_recebido_ns) (mesma regra do backtest)."""
    return np.maximum(tab["t_utc_ns"].to_numpy(np.int64), tab["t_recebido_ns"].to_numpy(np.int64))


def ordenar_por_disponibilidade(tab: pd.DataFrame) -> pd.DataFrame:
    disp = disponibilidade_ns(tab)
    ordem = np.lexsort((np.arange(len(tab)), disp))
    return tab.iloc[ordem].reset_index(drop=True)


def _sinais(flags: np.ndarray) -> np.ndarray:
    b = (flags & TICK_FLAG_BUY) != 0
    s = (flags & TICK_FLAG_SELL) != 0
    return np.where(b & ~s, 1, np.where(s & ~b, -1, 0)).astype(np.int8)


def compactar_janelas(tab: pd.DataFrame, janela_ms: float) -> tuple[pd.DataFrame, EstatisticaCompactacao]:
    """Tabela de ticks (colunas de COLS_TICK) -> tabela compactada, ordenada por disponibilidade."""
    tab = ordenar_por_disponibilidade(tab[COLS_TICK])
    n = len(tab)
    if janela_ms <= 0 or n < 2:
        return tab, EstatisticaCompactacao(n, n, janela_ms)
    W = int(janela_ms * 1_000_000)
    t = disponibilidade_ns(tab)
    p = tab["last"].to_numpy(float)
    w = t // W
    primeiro = np.empty(n, dtype=bool)
    primeiro[0] = True
    primeiro[1:] = w[1:] != w[:-1]
    s = pd.Series(p)
    grp = pd.Series(w)
    max_ant = s.groupby(grp).cummax().shift(1).to_numpy()
    min_ant = s.groupby(grp).cummin().shift(1).to_numpy()
    sai = primeiro.copy()
    sai[1:] |= (p[1:] > max_ant[1:]) | (p[1:] < min_ant[1:])
    emitidos = tab[sai].copy()
    emitidos["_t"], emitidos["_o"] = t[sai], 1  # no mesmo instante, o resumo da janela anterior vem antes
    resto = ~sai
    partes = [emitidos]
    if resto.any():
        r = tab[resto].copy()
        r["_w"], r["_lado"] = w[resto], _sinais(r["flags"].to_numpy(np.int64))
        g = r.groupby(["_w", "_lado"], sort=True)
        ult = g.tail(1).set_index(["_w", "_lado"])
        vol = g["volume"].sum()
        resumo = ult.loc[vol.index].copy()
        resumo["volume"] = vol.to_numpy()
        lados = resumo.index.get_level_values("_lado").to_numpy()
        base = resumo["flags"].to_numpy(np.int64) & ~(TICK_FLAG_BUY | TICK_FLAG_SELL)
        resumo["flags"] = base | np.where(lados > 0, TICK_FLAG_BUY, np.where(lados < 0, TICK_FLAG_SELL, 0))
        fim = (resumo.index.get_level_values("_w").to_numpy(np.int64) + 1) * W
        resumo["t_utc_ns"] = fim
        resumo["t_recebido_ns"] = fim
        resumo = resumo.reset_index(drop=True)
        resumo["_t"], resumo["_o"] = fim, 0
        partes.append(resumo[[*COLS_TICK, "_t", "_o"]])
    out = pd.concat(partes, ignore_index=True)
    seq = np.arange(len(out))
    out = out.iloc[np.lexsort((seq, out["_o"].to_numpy(), out["_t"].to_numpy()))]
    out = out.drop(columns=["_t", "_o"]).reset_index(drop=True)
    return out, EstatisticaCompactacao(n, len(out), janela_ms)


def ticks_no_instante(tab: pd.DataFrame) -> list[Tick]:
    """Tabela -> Ticks com os dois relogios iguais ao instante de DISPONIBILIDADE (como `replay.tick_no_instante`):
    o orquestrador, o pipeline de features e o simulador so andam para frente e so veem o que ja chegou."""
    disp = disponibilidade_ns(tab).tolist()
    ms = tab["t_servidor_ms"].to_numpy(np.int64).tolist()
    bid, ask, last = (tab[c].to_numpy(float).tolist() for c in ("bid", "ask", "last"))
    vol = tab["volume"].to_numpy(float).tolist()
    flags = tab["flags"].to_numpy(np.int64).tolist()
    mk = Tick.model_construct
    return [mk(t_servidor_ms=a, t_utc_ns=b, t_recebido_ns=b, bid=c, ask=d, last=e, volume=f, flags=g)
            for a, b, c, d, e, f, g in zip(ms, disp, bid, ask, last, vol, flags, strict=True)]


__all__ = ["EstatisticaCompactacao", "compactar_janelas", "disponibilidade_ns", "ordenar_por_disponibilidade",
           "sinal_agressor", "ticks_no_instante"]
