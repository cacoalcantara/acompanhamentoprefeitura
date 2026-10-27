"""Gerador DETERMINISTICO de ticks e livros SINTETICOS (para testes e desenvolvimento de infraestrutura).

Nada aqui e dado de mercado. Todo resultado carrega `sintetico=True` e `rotulo=ROTULO_SINTETICO`.

Sinal fraco injetado (opcional, conhecido): a direcao do agressor segue uma cadeia de Markov com
P(direcao_t == direcao_{t-1}) = 0.5 + sinal_forca; logo a correlacao serial das direcoes e 2*sinal_forca e
o melhor preditor da proxima direcao e a direcao anterior. Com sinal_forca=0 o passeio e sem estrutura.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field

import numpy as np

from robowin.core.calendario_b3 import eh_pregao
from robowin.core.tipos import LivroSnapshot, NivelBook, Tick

from .ticks import ArmazenamentoTicks, dia_ns

ROTULO_SINTETICO = "SINTETICO - dados gerados por simulacao; NAO sao dados de mercado"
FLAG_BID, FLAG_ASK, FLAG_LAST, FLAG_VOLUME, FLAG_BUY, FLAG_SELL = 2, 4, 8, 16, 32, 64  # constantes do MT5
TICK_PTS = 5.0
_SESSAO_S = 9 * 3600 + 25 * 60  # 09:00 -> 18:25


@dataclass(frozen=True)
class ConfigSintetico:
    seed: int = 42
    data: dt.date = dt.date(2026, 3, 10)
    simbolo: str = "SINT_WIN"
    n_ticks: int = 20_000
    preco_inicial: float = 130_000.0
    passo_medio_ticks: float = 0.6        # media da quantidade de ticks (5 pts) por negocio, antes da sazonalidade
    sinal_forca: float = 0.0              # 0 = sem sinal; ex. 0.03 => P(continuacao) = 0.53
    gap_abertura_pts: float = 0.0
    n_saltos: int = 0                     # "limit moves" instantaneos
    salto_pts: float = 1_500.0
    n_lacunas: int = 0                    # janelas sem ticks (feed parado)
    lacuna_s: float = 120.0
    offset_servidor_ms: int = 0
    livro_a_cada_ticks: int = 20          # 0 = sem livro
    niveis_livro: int = 5


@dataclass(frozen=True)
class DadosSinteticos:
    config: ConfigSintetico
    ticks: tuple[Tick, ...]
    livros: tuple[LivroSnapshot, ...]
    sintetico: bool = True
    rotulo: str = ROTULO_SINTETICO
    metadados: dict[str, object] = field(default_factory=dict)

    def hash(self) -> str:
        h = hashlib.sha256()
        for t in self.ticks:
            h.update(repr((t.t_servidor_ms, t.t_utc_ns, t.t_recebido_ns, t.bid, t.ask, t.last, t.volume,
                           t.flags)).encode())
        for lv in self.livros:
            h.update(repr((lv.t_utc_ns, [(n.preco, n.volume, n.lado) for n in lv.niveis])).encode())
        return h.hexdigest()

    def relatorio(self) -> dict[str, object]:
        return {"sintetico": True, "rotulo": self.rotulo, "seed": self.config.seed, "n_ticks": len(self.ticks),
                "n_livros": len(self.livros), "hash": self.hash(), **self.metadados}

    def gravar(self, armazem: ArmazenamentoTicks) -> int:
        """Grava no armazem e o marca como sintetico (CLI/painel exibem o rotulo)."""
        armazem.marcar_sintetico()
        armazem.gravar_livros(self.config.simbolo, self.livros)
        armazem.gravar_offset_servidor(self.config.simbolo, self.config.offset_servidor_ms)
        return armazem.gravar(self.config.simbolo, self.ticks)


def _arred(x: np.ndarray) -> np.ndarray:
    return np.round(x / TICK_PTS) * TICK_PTS


def gerar(cfg: ConfigSintetico | None = None) -> DadosSinteticos:
    cfg = cfg or ConfigSintetico()
    rng = np.random.default_rng(cfg.seed)
    n = cfg.n_ticks
    # --- instantes: processo de Poisson nao homogeneo com intensidade em U (abertura/fechamento mais ativos)
    seg = np.arange(_SESSAO_S + 1, dtype=float)
    vol_s = 1.0 + 1.2 * np.exp(-seg / 2400.0) + 0.8 * np.exp(-(_SESSAO_S - seg) / 1800.0)
    inten = vol_s ** 1.5
    cdf = np.cumsum(inten)
    cdf = (cdf - cdf[0]) / (cdf[-1] - cdf[0])
    sec = np.interp(np.sort(rng.random(n)), cdf, seg)
    ms = np.floor(sec * 1000).astype(np.int64)
    idx = np.arange(n, dtype=np.int64)
    ms = np.maximum.accumulate(ms - idx) + idx  # estritamente crescente
    vol_t = np.interp(ms / 1000.0, seg, vol_s)
    # --- direcao do agressor e tamanho do passo
    u = rng.random(n)
    p_cont = 0.5 + cfg.sinal_forca
    s0 = 1.0 if rng.random() < 0.5 else -1.0
    sinal = s0 * np.cumprod(np.where(u < p_cont, 1.0, -1.0))
    passo = rng.poisson(cfg.passo_medio_ticks * vol_t)
    delta = sinal * passo * TICK_PTS
    # --- saltos (limit moves) e gap de abertura
    if cfg.n_saltos > 0 and n > 1:
        pos = rng.choice(np.arange(1, n), size=min(cfg.n_saltos, n - 1), replace=False)
        sgn = rng.choice([-1.0, 1.0], size=pos.size)
        delta[pos] += sgn * cfg.salto_pts
    bid = _arred(cfg.preco_inicial + cfg.gap_abertura_pts + np.cumsum(delta))
    bid = np.maximum(bid, TICK_PTS)
    spread = np.where(rng.random(n) < 0.1, 2 * TICK_PTS, TICK_PTS)
    ask = bid + spread
    last = np.where(sinal > 0, ask, bid)
    volume = np.clip(np.round(rng.lognormal(1.2, 0.8, n)), 1, 500)
    flags = FLAG_LAST | FLAG_VOLUME | np.where(sinal > 0, FLAG_BUY, FLAG_SELL)
    lat_ms = rng.integers(1, 30, n)
    # --- instantes absolutos
    base_ms = (dia_ns(cfg.data) // 1_000_000) + 9 * 3_600_000
    utc_ms = base_ms + ms
    keep = np.ones(n, dtype=bool)
    lacunas: list[tuple[int, int]] = []
    for _ in range(cfg.n_lacunas):
        ini = int(rng.integers(600_000, _SESSAO_S * 1000 - int(cfg.lacuna_s * 1000) - 1))
        fim = ini + int(cfg.lacuna_s * 1000)
        keep &= ~((ms >= ini) & (ms < fim))
        lacunas.append((int(base_ms + ini), int(base_ms + fim)))
    ticks = tuple(
        Tick.model_construct(t_servidor_ms=int(utc_ms[i] + cfg.offset_servidor_ms), t_utc_ns=int(utc_ms[i]) * 1_000_000,
                             t_recebido_ns=int(utc_ms[i] + lat_ms[i]) * 1_000_000, bid=float(bid[i]),
                             ask=float(ask[i]), last=float(last[i]), volume=float(volume[i]), flags=int(flags[i]))
        for i in np.flatnonzero(keep))
    livros: list[LivroSnapshot] = []
    if cfg.livro_a_cada_ticks > 0:
        for i in np.flatnonzero(keep)[:: cfg.livro_a_cada_ticks]:
            vols = rng.integers(5, 200, size=2 * cfg.niveis_livro)
            niv = [NivelBook(preco=float(bid[i] - TICK_PTS * k), volume=float(vols[k]), lado="compra")
                   for k in range(cfg.niveis_livro)]
            niv += [NivelBook(preco=float(ask[i] + TICK_PTS * k), volume=float(vols[cfg.niveis_livro + k]),
                              lado="venda") for k in range(cfg.niveis_livro)]
            livros.append(LivroSnapshot(t_utc_ns=int(utc_ms[i]) * 1_000_000, niveis=tuple(niv)))
    meta = {"sinal_forca": cfg.sinal_forca, "p_continuacao": p_cont,
            "n_saltos": cfg.n_saltos, "lacunas_ms_utc": lacunas, "gap_abertura_pts": cfg.gap_abertura_pts,
            "ultimo_bid": float(bid[-1]) if n else cfg.preco_inicial}
    return DadosSinteticos(config=cfg, ticks=ticks, livros=tuple(livros), metadados=meta)


def gerar_dias(cfg: ConfigSintetico, n_dias: int) -> list[DadosSinteticos]:
    """Gera `n_dias` pregoes consecutivos (pula fins de semana/feriados); seed derivada por dia, preco encadeado."""
    out: list[DadosSinteticos] = []
    d, preco = cfg.data, cfg.preco_inicial
    while len(out) < n_dias:
        if eh_pregao(d):
            c = ConfigSintetico(**{**cfg.__dict__, "data": d, "seed": cfg.seed + len(out), "preco_inicial": preco})
            r = gerar(c)
            out.append(r)
            preco = float(r.metadados["ultimo_bid"])  # type: ignore[arg-type]
        d += dt.timedelta(days=1)
    return out
