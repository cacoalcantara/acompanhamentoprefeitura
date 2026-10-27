"""Utilidades contra vazamento de futuro e o TESTE DE PERTURBACAO FUTURA.

Teste de perturbacao futura: roda o backtest completo duas vezes, uma com os dados originais e outra com
todos os dados que so ficam DISPONIVEIS depois de `t_corte` alterados (precos, volumes, fluxo, livro). Se
alguma decisao (instante, acao, hash do dossie, intencao, resultado de risco) ate `t_corte` diferir, algo
do futuro entrou no passado. O teste tambem exige que a perturbacao tenha sido EFETIVA (que o futuro
tenha mudado de fato as decisoes depois de `t_corte`), para nao passar de forma vazia.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from robowin.core.calendario_b3 import SP
from robowin.core.tipos import LivroSnapshot, NivelBook, Tick
from robowin.features.pipeline import PipelineFeatures
from robowin.ia.dossie import verificar_anonimizacao

from .motor import ResultadoBacktest
from .replay import disponivel_tick

FLAG_BUY, FLAG_SELL = 32, 64
TICK_PTS = 5.0


# ============================================================================================ perturbacao
def perturbar_futuro(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot], t_corte_ns: int, semente: int = 0,
                     escala_pts: float = 1500.0) -> tuple[list[Tick], list[LivroSnapshot]]:
    """Altera TUDO que so fica disponivel apos `t_corte_ns` (tick: max(t_utc, t_recebido); livro: t_utc).

    Mantem os carimbos de tempo (a ordem de replay nao muda); altera precos, volumes, flags e niveis do livro.
    Um backfill com t_utc <= t_corte mas recebido depois e perturbado tambem (ele e futuro para o sistema).
    """
    rng = np.random.default_rng(semente)
    out_t: list[Tick] = []
    for t in ticks:
        if disponivel_tick(t) <= t_corte_ns:
            out_t.append(t)
            continue
        d = float(np.round(rng.normal(0.0, escala_pts) / TICK_PTS) * TICK_PTS)
        flags = t.flags
        if rng.random() < 0.5:  # troca o lado do agressor
            flags = (flags & ~(FLAG_BUY | FLAG_SELL)) | (FLAG_SELL if flags & FLAG_BUY else FLAG_BUY)
        out_t.append(t.model_copy(update={
            "bid": max(t.bid + d, TICK_PTS), "ask": max(t.ask + d, 2 * TICK_PTS), "last": max(t.last + d, TICK_PTS),
            "volume": float(max(1.0, round(t.volume * float(rng.uniform(0.2, 5.0))))), "flags": flags}))
    out_l: list[LivroSnapshot] = []
    for lv in livros:
        if lv.t_utc_ns <= t_corte_ns:
            out_l.append(lv)
            continue
        d = float(np.round(rng.normal(0.0, escala_pts) / TICK_PTS) * TICK_PTS)
        out_l.append(LivroSnapshot(t_utc_ns=lv.t_utc_ns, niveis=tuple(
            NivelBook(preco=max(n.preco + d, TICK_PTS), volume=float(rng.integers(1, 400)), lado=n.lado)
            for n in lv.niveis)))
    return out_t, out_l


def assinatura_decisoes(res: ResultadoBacktest, t_corte_ns: int | None = None) -> list[tuple[Any, ...]]:
    """Tudo que caracteriza as decisoes (e seus efeitos) com t_ns <= t_corte."""
    out: list[tuple[Any, ...]] = []
    for d in res.decisoes:
        if t_corte_ns is None or d["t_ns"] <= t_corte_ns:
            out.append(("decisao", d["t_ns"], d["braco"], d["gatilho"], d["acao"], d["hash_dossie"],
                        d["intencao_json"], d["risco_json"], d["resultado"], d["motivos"]))
    for e in res.execucoes:
        if t_corte_ns is None or e["t_ns"] <= t_corte_ns:
            out.append(("execucao", e["braco"], e["t_ns"], e["preco"], e["quantidade"], e["taxas_brl"]))
    for q in res.equity:
        if t_corte_ns is None or q["t_ns"] <= t_corte_ns:
            out.append(("equity", q["braco"], q["t_ns"], q["equity_brl"], q["drawdown_brl"]))
    return out


@dataclass
class RelatorioVazamento:
    ok: bool
    n_comparados: int
    divergencias: list[str] = field(default_factory=list)
    perturbacao_efetiva: bool = False
    n_ticks_perturbados: int = 0
    n_livros_perturbados: int = 0
    t_corte_ns: int = 0

    def texto(self) -> str:
        if self.ok:
            return f"sem vazamento: {self.n_comparados} itens identicos ate t={self.t_corte_ns}"
        return "VAZAMENTO/TESTE INVALIDO: " + "; ".join(self.divergencias[:5])


def verificar_perturbacao_futura(
    executar: Callable[[Sequence[Tick], Sequence[LivroSnapshot]], ResultadoBacktest],
    ticks: Sequence[Tick], livros: Sequence[LivroSnapshot], t_corte_ns: int, *, semente: int = 0,
    escala_pts: float = 1500.0, exigir_efetiva: bool = True,
) -> RelatorioVazamento:
    """`executar(ticks, livros)` deve montar um backtest NOVO (bracos novos) e executa-lo."""
    base = executar(ticks, livros)
    t_p, l_p = perturbar_futuro(ticks, livros, t_corte_ns, semente, escala_pts)
    pert = executar(t_p, l_p)
    a, b = assinatura_decisoes(base, t_corte_ns), assinatura_decisoes(pert, t_corte_ns)
    div: list[str] = []
    if len(a) != len(b):
        div.append(f"quantidade de itens ate t difere: {len(a)} != {len(b)}")
    for x, y in zip(a, b, strict=False):
        if x != y:
            div.append(f"divergencia ate t em {x[0]} {x[1:3]}")
            break
    fut_a = [x for x in assinatura_decisoes(base) if x not in set(a)]
    fut_b = [x for x in assinatura_decisoes(pert) if x not in set(b)]
    efetiva = fut_a != fut_b
    if exigir_efetiva and not efetiva:
        div.append("perturbacao nao alterou nada depois de t: o teste e vazio (braco ignora os dados?)")
    if len(a) == 0:
        div.append("nenhuma decisao ate t: nada a comparar")
    n_t = sum(1 for x, y in zip(ticks, t_p, strict=True) if x != y)
    n_l = sum(1 for x, y in zip(livros, l_p, strict=True) if x != y)
    return RelatorioVazamento(ok=not div, n_comparados=len(a), divergencias=div, perturbacao_efetiva=efetiva,
                              n_ticks_perturbados=n_t, n_livros_perturbados=n_l, t_corte_ns=t_corte_ns)


# ============================================================================================ causalidade
def perturbar_serie(x: Sequence[float], k: int, semente: int = 0, escala: float | None = None) -> list[float]:
    """Mantem x[0..k] e substitui x[k+1:] por valores muito diferentes."""
    rng = np.random.default_rng(semente)
    a = np.asarray(x, dtype=float)
    esc = escala if escala is not None else (float(np.nanstd(a)) or 1.0) * 10.0
    b = a.copy()
    b[k + 1:] = a[k + 1:] + rng.normal(0.0, esc, size=len(a) - k - 1) + 5 * esc
    return [float(v) for v in b]


def _iguais(u: Sequence[float], v: Sequence[float], tol: float = 0.0) -> bool:
    if len(u) != len(v):
        return False
    for p, q in zip(u, v, strict=True):
        if (isinstance(p, float) and math.isnan(p)) and (isinstance(q, float) and math.isnan(q)):
            continue
        if abs(p - q) > tol or (p != p) != (q != q):
            return False
    return True


def verificar_causal(fn: Callable[[Sequence[float]], Sequence[float]], serie: Sequence[float],
                     pontos: Iterable[int], semente: int = 0) -> list[int]:
    """Verifica que `fn` (normalizacao, classificacao de regime...) e CAUSAL, de dois modos:

    * perturbacao: alterar serie[k+1:] nao muda a saida em 0..k;
    * prefixo: fn(serie[:k+1]) == fn(serie)[:k+1].

    Devolve os pontos `k` que violam (lista vazia = causal).
    """
    base = list(fn(serie))
    ruins: list[int] = []
    for k in pontos:
        pert = list(fn(perturbar_serie(serie, k, semente)))
        pref = list(fn(list(serie)[:k + 1]))
        if not _iguais(base[:k + 1], pert[:k + 1]) or not _iguais(base[:k + 1], pref[:k + 1]):
            ruins.append(k)
    return ruins


def zscore_expansivo(x: Sequence[float]) -> list[float]:
    """Exemplo CAUSAL: z-score com media e desvio calculados so do passado (inclui o ponto atual)."""
    a = np.asarray(x, dtype=float)
    n = np.arange(1, len(a) + 1)
    m = np.cumsum(a) / n
    v = np.cumsum(a * a) / n - m * m
    s = np.sqrt(np.maximum(v, 0.0))
    return [float((a[i] - m[i]) / s[i]) if s[i] > 1e-12 else 0.0 for i in range(len(a))]


def zscore_global(x: Sequence[float]) -> list[float]:
    """Exemplo NAO CAUSAL (vaza o futuro pela media/desvio globais). So para testar o verificador."""
    a = np.asarray(x, dtype=float)
    s = float(a.std()) or 1.0
    return [float(v) for v in (a - a.mean()) / s]


def features_causais(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot], instantes: Sequence[int],
                     t_corte_ns: int, semente: int = 0) -> list[str]:
    """PipelineFeatures: vetores em instantes <= t_corte nao podem mudar se o futuro for perturbado."""
    from robowin.features.pipeline import calcular_offline
    ate = [t for t in instantes if t <= t_corte_ns]
    t_p, l_p = perturbar_futuro(ticks, livros, t_corte_ns, semente)
    kw = {"usar_recebido": True}
    v0 = calcular_offline(ticks, livros, ate, **kw)
    v1 = calcular_offline(t_p, l_p, ate, **kw)
    erros: list[str] = []
    for a, b in zip(v0, v1, strict=True):
        for nome in a.valores:
            x, y = a.valores[nome], b.valores[nome]
            if not (x == y or (x != x and y != y)):
                erros.append(f"{nome}@{a.t_ns}: {x} != {y}")
    return erros


def pipeline_incremental(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot], instante_ns: int) -> dict[str, float]:
    """Vetor do pipeline em `instante_ns` alimentando APENAS eventos ja disponiveis nesse instante."""
    from .replay import ordenar_por_disponibilidade
    pipe = PipelineFeatures(usar_recebido=True)
    for ev in ordenar_por_disponibilidade(ticks, livros):
        if ev.t_disp_ns > instante_ns:
            break
        if ev.tipo == "tick":
            pipe.on_tick(ev.tick.model_copy(update={"t_recebido_ns": ev.t_disp_ns}))  # type: ignore[union-attr]
        else:
            pipe.on_livro(ev.livro)  # type: ignore[arg-type]
    return pipe.vetor(instante_ns).valores


# ============================================================================================ anonimizacao
def _formas_da_data(t_ns: int) -> list[str]:
    d = dt.datetime.fromtimestamp(t_ns / 1e9, SP)
    u = dt.datetime.fromtimestamp(t_ns / 1e9, dt.UTC)
    formas = {d.strftime("%Y-%m-%d"), d.strftime("%d/%m/%Y"), d.strftime("%d/%m/%y"), d.strftime("%Y%m%d"),
              u.strftime("%Y-%m-%d"), str(d.year)}
    return sorted(formas)


def _folhas(x: Any, caminho: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(x, dict):
        for k, v in x.items():
            yield (f"{caminho}.{k}#chave", str(k))
            yield from _folhas(v, f"{caminho}.{k}")
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            yield from _folhas(v, f"{caminho}[{i}]")
    else:
        yield caminho, x


def dossie_sem_datas(dossie: dict[str, Any], t_ns: int, niveis_absolutos: Iterable[float] = ()) -> list[str]:
    """Violacoes de anonimizacao (vazia = ok): datas de `t_ns` em qualquer formato, epoch em s/ms/us/ns,
    alem das regras de ia.dossie.verificar_anonimizacao (chaves e niveis absolutos)."""
    erros = list(verificar_anonimizacao(dossie, niveis_absolutos))
    datas = _formas_da_data(t_ns)
    epochs = {t_ns, t_ns // 1_000, t_ns // 1_000_000, t_ns // 1_000_000_000}
    for caminho, v in _folhas(dossie):
        if isinstance(v, str):
            for f in datas:
                if f in v:
                    erros.append(f"data {f!r} em {caminho}")
            if re.search(r"\d{4}-\d{2}-\d{2}", v):
                erros.append(f"data ISO em {caminho}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
            if int(v) in epochs and abs(v) > 1e8:
                erros.append(f"epoch do instante em {caminho}")
    return erros
