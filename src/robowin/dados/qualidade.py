"""Verificadores de qualidade de ticks e livro. Achado critico bloqueia entradas."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from robowin.core.calendario_b3 import SP
from robowin.core.tipos import LivroSnapshot, Tick

Severidade = Literal["critico", "aviso"]


@dataclass(frozen=True)
class ConfigQualidade:
    lacuna_aviso_s: float = 60.0        # lacuna entre ticks consecutivos (dentro do pregao)
    lacuna_critica_s: float = 300.0
    feed_parado_s: float = 30.0         # idade maxima do ultimo tick (so se `agora_ns` informado)
    tunel_pontos: float = 100.0         # salto maximo aceito entre ticks consecutivos (aviso)
    tunel_critico_pontos: float = 500.0  # acima disto: critico
    sessao_inicio_min: int | None = 9 * 60       # lacunas so contam dentro da sessao; None = sempre
    sessao_fim_min: int | None = 18 * 60 + 25


@dataclass(frozen=True)
class Achado:
    tipo: str
    severidade: Severidade
    detalhe: str
    contagem: int = 1
    t_ns: int | None = None  # primeiro instante afetado


@dataclass
class RelatorioQualidade:
    n_ticks: int = 0
    n_livros: int = 0
    achados: list[Achado] = field(default_factory=list)
    sintetico: bool = False

    @property
    def criticos(self) -> list[Achado]:
        return [a for a in self.achados if a.severidade == "critico"]

    @property
    def avisos(self) -> list[Achado]:
        return [a for a in self.achados if a.severidade == "aviso"]

    @property
    def bloqueia_entradas(self) -> bool:
        return bool(self.criticos)

    @property
    def ok(self) -> bool:
        return not self.achados

    def tipos(self) -> set[str]:
        return {a.tipo for a in self.achados}

    def unir(self, outro: RelatorioQualidade) -> RelatorioQualidade:
        return RelatorioQualidade(self.n_ticks + outro.n_ticks, self.n_livros + outro.n_livros,
                                  self.achados + outro.achados, self.sintetico or outro.sintetico)

    def texto(self) -> str:
        rot = "[SINTETICO - nao e dado de mercado] " if self.sintetico else ""
        linhas = [f"{rot}ticks={self.n_ticks} livros={self.n_livros} criticos={len(self.criticos)} "
                  f"avisos={len(self.avisos)}"]
        linhas += [f"  [{a.severidade.upper()}] {a.tipo} (x{a.contagem}): {a.detalhe}" for a in self.achados]
        return "\n".join(linhas)


def _arr(ticks: Sequence[Tick], campo: str, dtype: type) -> np.ndarray:
    return np.fromiter((getattr(t, campo) for t in ticks), dtype=dtype, count=len(ticks))


def verificar_feed_parado(ultimo_t_ns: int | None, agora_ns: int, cfg: ConfigQualidade | None = None) -> list[Achado]:
    cfg = cfg or ConfigQualidade()
    if ultimo_t_ns is None:
        return [Achado("feed_parado", "critico", "nenhum tick recebido")]
    idade = (agora_ns - ultimo_t_ns) / 1e9
    if idade > cfg.feed_parado_s:
        return [Achado("feed_parado", "critico", f"ultimo tick ha {idade:.1f}s (limite {cfg.feed_parado_s}s)",
                       t_ns=ultimo_t_ns)]
    return []


def verificar_ticks(ticks: Sequence[Tick], cfg: ConfigQualidade | None = None, *, agora_ns: int | None = None,
                    sintetico: bool = False) -> RelatorioQualidade:
    """Monotonicidade, duplicatas, lacunas, feed parado, book cruzado e saltos. `ticks` na ordem de chegada."""
    cfg = cfg or ConfigQualidade()
    rel = RelatorioQualidade(n_ticks=len(ticks), sintetico=sintetico)
    a = rel.achados
    if agora_ns is not None:
        a += verificar_feed_parado(ticks[-1].t_utc_ns if ticks else None, agora_ns, cfg)
    if not ticks:
        return rel
    t = _arr(ticks, "t_utc_ns", np.int64)
    ms = _arr(ticks, "t_servidor_ms", np.int64)
    bid, ask = _arr(ticks, "bid", float), _arr(ticks, "ask", float)
    last, vol = _arr(ticks, "last", float), _arr(ticks, "volume", float)

    # monotonicidade
    ret = np.flatnonzero(np.diff(t) < 0)
    if ret.size:
        a.append(Achado("nao_monotonico", "critico", "t_utc_ns retrocede", int(ret.size), int(t[ret[0] + 1])))
    ret_ms = np.flatnonzero(np.diff(ms) < 0)
    if ret_ms.size:
        a.append(Achado("servidor_retrocede", "aviso", "t_servidor_ms retrocede", int(ret_ms.size),
                        int(t[ret_ms[0] + 1])))

    # duplicatas (chave de dedup do armazenamento)
    df = pd.DataFrame({"ms": ms, "bid": bid, "ask": ask, "last": last, "vol": vol})
    dup = df.duplicated().to_numpy()
    if dup.any():
        a.append(Achado("duplicata", "aviso", "ticks com mesma chave (ms,bid,ask,last,volume)", int(dup.sum()),
                        int(t[np.argmax(dup)])))

    # precos invalidos / book cruzado
    ruim = (bid < 0) | (ask < 0) | (last < 0)
    if ruim.any():
        a.append(Achado("preco_invalido", "critico", "preco negativo", int(ruim.sum()), int(t[np.argmax(ruim)])))
    cruz = (bid > 0) & (ask > 0) & (bid >= ask)
    if cruz.any():
        a.append(Achado("book_cruzado", "critico", "bid >= ask", int(cruz.sum()), int(t[np.argmax(cruz)])))

    # lacunas dentro do pregao (mesmo dia)
    if len(t) > 1:
        dt_s = np.diff(t) / 1e9
        idx = pd.DatetimeIndex(pd.to_datetime(t, unit="ns", utc=True)).tz_convert(SP)
        min_dia = np.asarray(idx.hour * 60 + idx.minute)
        dia = np.asarray(idx.normalize().asi8)
        mesmo = dia[1:] == dia[:-1]
        if cfg.sessao_inicio_min is not None and cfg.sessao_fim_min is not None:
            dentro = ((min_dia[:-1] >= cfg.sessao_inicio_min) & (min_dia[1:] <= cfg.sessao_fim_min))
        else:
            dentro = np.ones(len(dt_s), dtype=bool)
        valido = mesmo & dentro & (dt_s >= 0)
        crit = np.flatnonzero(valido & (dt_s > cfg.lacuna_critica_s))
        avis = np.flatnonzero(valido & (dt_s > cfg.lacuna_aviso_s) & (dt_s <= cfg.lacuna_critica_s))
        if crit.size:
            a.append(Achado("lacuna", "critico", f"{crit.size} lacuna(s) > {cfg.lacuna_critica_s}s; "
                            f"maior {dt_s[crit].max():.1f}s", int(crit.size), int(t[crit[0]])))
        if avis.size:
            a.append(Achado("lacuna", "aviso", f"{avis.size} lacuna(s) > {cfg.lacuna_aviso_s}s; "
                            f"maior {dt_s[avis].max():.1f}s", int(avis.size), int(t[avis[0]])))

        # saltos (tunel de preco) entre ticks consecutivos, pelo ultimo preco negociado valido
        ok = last > 0
        if ok.sum() > 1:
            ti, pl = t[ok], last[ok]
            salto = np.abs(np.diff(pl))
            sc = np.flatnonzero(salto > cfg.tunel_critico_pontos)
            sa = np.flatnonzero((salto > cfg.tunel_pontos) & (salto <= cfg.tunel_critico_pontos))
            if sc.size:
                a.append(Achado("salto", "critico", f"{sc.size} salto(s) > {cfg.tunel_critico_pontos} pts; "
                                f"maior {salto[sc].max():.0f}", int(sc.size), int(ti[sc[0] + 1])))
            if sa.size:
                a.append(Achado("salto", "aviso", f"{sa.size} salto(s) > {cfg.tunel_pontos} pts; "
                                f"maior {salto[sa].max():.0f}", int(sa.size), int(ti[sa[0] + 1])))
    return rel


def verificar_livros(livros: Sequence[LivroSnapshot], cfg: ConfigQualidade | None = None, *,
                     sintetico: bool = False) -> RelatorioQualidade:
    cfg = cfg or ConfigQualidade()
    rel = RelatorioQualidade(n_livros=len(livros), sintetico=sintetico)
    a = rel.achados
    cruz = vazio = volruim = retro = dup = 0
    prim_cruz = prim_vazio = None
    ant = None
    vistos: set[int] = set()
    for lv in livros:
        compras = [n.preco for n in lv.niveis if n.lado == "compra"]
        vendas = [n.preco for n in lv.niveis if n.lado == "venda"]
        if not compras or not vendas:
            vazio += 1
            prim_vazio = prim_vazio or lv.t_utc_ns
        elif max(compras) >= min(vendas):
            cruz += 1
            prim_cruz = prim_cruz or lv.t_utc_ns
        if any(n.volume <= 0 or n.preco <= 0 for n in lv.niveis):
            volruim += 1
        if ant is not None and lv.t_utc_ns < ant:
            retro += 1
        if lv.t_utc_ns in vistos:
            dup += 1
        vistos.add(lv.t_utc_ns)
        ant = lv.t_utc_ns
    if cruz:
        a.append(Achado("book_cruzado", "critico", "melhor compra >= melhor venda no livro", cruz, prim_cruz))
    if retro:
        a.append(Achado("nao_monotonico", "critico", "t_utc_ns do livro retrocede", retro))
    if vazio:
        a.append(Achado("livro_unilateral", "aviso", "livro sem um dos lados", vazio, prim_vazio))
    if volruim:
        a.append(Achado("nivel_invalido", "aviso", "nivel com volume/preco <= 0", volruim))
    if dup:
        a.append(Achado("duplicata", "aviso", "snapshots de livro com mesmo instante", dup))
    return rel


def verificar(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot] = (), cfg: ConfigQualidade | None = None, *,
              agora_ns: int | None = None, sintetico: bool = False) -> RelatorioQualidade:
    return verificar_ticks(ticks, cfg, agora_ns=agora_ns, sintetico=sintetico).unir(
        verificar_livros(livros, cfg, sintetico=sintetico))
