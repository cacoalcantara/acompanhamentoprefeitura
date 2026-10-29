"""Pontuacao e recalibracao das probabilidades declaradas pela IA (``previsoes_ia``; docs/16 C33 e secao 4.3).

``p_alvo`` e a probabilidade declarada de o alvo sair antes do stop e do tempo. Aqui ela so e PONTUADA: nunca libera
nem bloqueia entrada. ``y_alvo`` vem do replay do plano original sem gestao (``avaliacao/contrafactual``).

* ``pontuar``: Brier; Brier de referencia pela taxa-base CAUSAL (so pregoes anteriores) e pelo teorico do passeio
  aleatorio sem deriva ``s/(s+a)`` (stop s, alvo a); BSS com intervalo por bootstrap de PREGOES (a unidade do
  experimento); ECE, curva de confiabilidade e decomposicao de Murphy.
* ``recalibrador_walk_forward``: so usa previsoes RESOLVIDAS antes de ``t`` (``resolvido_ns < t``). Com pelo menos
  ``MIN_CALIBRACAO`` usa regressao isotonica; antes disso encolhe para a taxa-base com peso ``n/(n+n0)`` no ``p``
  bruto. Mudar desfechos futuros nao altera ``p_cal(t)``.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression

from robowin.modelos.calibracao import MIN_CALIBRACAO, brier, curva_confiabilidade, decomposicao_brier, ece
from robowin.risco.tempo import dia_sp

N0_ENCOLHIMENTO = 50
TAXA_PADRAO = 0.5  # sem passado e sem referencia: probabilidade neutra


@dataclass(frozen=True)
class Previsao:
    """Uma linha de ``previsoes_ia`` ja resolvida (``y`` em {0,1})."""
    p: float
    y: int
    t0_ns: int
    resolvido_ns: int | None
    stop_pts: float | None
    alvo_pts: float | None
    incerteza: float | None
    dia: str


def _num(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def resolvidas(previsoes: Iterable[Mapping[str, Any]]) -> list[Previsao]:
    """Previsoes com ``p_alvo`` em [0,1] e ``y_alvo`` em {0,1}, ordenadas por ``t0_ns`` (as demais sao ignoradas)."""
    out: list[Previsao] = []
    for r in previsoes:
        p, y, t0 = _num(r.get("p_alvo")), r.get("y_alvo"), r.get("t0_ns")
        if p is None or not 0.0 <= p <= 1.0 or y not in (0, 1, True, False) or t0 is None:
            continue
        res = r.get("resolvido_ns")
        out.append(Previsao(p=p, y=int(y), t0_ns=int(t0), resolvido_ns=None if res is None else int(res),
                            stop_pts=_num(r.get("stop_pts")), alvo_pts=_num(r.get("alvo_pts")),
                            incerteza=_num(r.get("incerteza")), dia=dia_sp(int(t0)).isoformat()))
    out.sort(key=lambda x: x.t0_ns)
    return out


def p_teorico(stop_pts: float | None, alvo_pts: float | None) -> float | None:
    """P(alvo antes do stop) num passeio aleatorio sem deriva: s/(s+a). None sem geometria valida."""
    if stop_pts is None or alvo_pts is None or stop_pts <= 0 or alvo_pts <= 0:
        return None
    return stop_pts / (stop_pts + alvo_pts)


def _taxas_base_causais(prev: Sequence[Previsao]) -> list[float | None]:
    """Para cada previsao do dia d: frequencia de acerto das previsoes de dias < d (None no primeiro dia)."""
    por_dia: dict[str, list[int]] = {}
    for x in prev:
        por_dia.setdefault(x.dia, []).append(x.y)
    acum_s = acum_n = 0
    base: dict[str, float | None] = {}
    for d in sorted(por_dia):
        base[d] = acum_s / acum_n if acum_n else None
        acum_s += sum(por_dia[d])
        acum_n += len(por_dia[d])
    return [base[x.dia] for x in prev]


def _bss(p: np.ndarray, y: np.ndarray, ref: np.ndarray) -> float:
    br = float(np.mean((ref - y) ** 2))
    if br <= 0:
        return float("nan")
    return 1.0 - float(np.mean((p - y) ** 2)) / br


def _ic(vals: list[float]) -> tuple[float, float] | None:
    v = np.array([x for x in vals if math.isfinite(x)])
    if v.size < 2:
        return None
    return float(np.quantile(v, 0.025)), float(np.quantile(v, 0.975))


def pontuar(previsoes: Iterable[Mapping[str, Any]], *, n_boot: int = 2000, seed: int = 0,
            bins: int = 10) -> dict[str, Any]:
    """Pontuacao das previsoes resolvidas (ver o cabecalho do modulo). So diagnostico."""
    prev = resolvidas(previsoes)
    n = len(prev)
    out: dict[str, Any] = {"n": n, "n_pregoes": len({x.dia for x in prev}),
                           "amostra_insuficiente": n < MIN_CALIBRACAO, "min_calibracao": MIN_CALIBRACAO}
    if n == 0:
        return out
    p = np.array([x.p for x in prev])
    y = np.array([x.y for x in prev], float)
    teo = [p_teorico(x.stop_pts, x.alvo_pts) for x in prev]
    base = _taxas_base_causais(prev)
    m_teo = np.array([t is not None for t in teo])
    m_base = np.array([b is not None for b in base])
    r_teo = np.array([t if t is not None else np.nan for t in teo])
    r_base = np.array([b if b is not None else np.nan for b in base])
    out.update(brier=brier(p, y), taxa_acerto=float(y.mean()), p_medio=float(p.mean()), ece=ece(p, y, bins),
               curva_confiabilidade=curva_confiabilidade(p, y, bins), murphy=decomposicao_brier(p, y, bins))
    out["n_ref_teorico"], out["n_ref_taxa_base"] = int(m_teo.sum()), int(m_base.sum())
    out["brier_ref_teorico"] = float(np.mean((r_teo[m_teo] - y[m_teo]) ** 2)) if m_teo.any() else None
    out["brier_ref_taxa_base"] = float(np.mean((r_base[m_base] - y[m_base]) ** 2)) if m_base.any() else None
    out["bss_teorico"] = _bss(p[m_teo], y[m_teo], r_teo[m_teo]) if m_teo.any() else None
    out["bss_taxa_base"] = _bss(p[m_base], y[m_base], r_base[m_base]) if m_base.any() else None
    # bootstrap por PREGAO: reamostra dias inteiros (as previsoes do mesmo dia sao dependentes)
    dias = sorted({x.dia for x in prev})
    idx_dia = {d: np.array([i for i, x in enumerate(prev) if x.dia == d]) for d in dias}
    rng = np.random.default_rng(seed)
    b_teo: list[float] = []
    b_base: list[float] = []
    if len(dias) >= 2 and n_boot > 0:
        for _ in range(n_boot):
            ii = np.concatenate([idx_dia[dias[k]] for k in rng.integers(0, len(dias), len(dias))])
            mt, mb = ii[m_teo[ii]], ii[m_base[ii]]
            if mt.size:
                b_teo.append(_bss(p[mt], y[mt], r_teo[mt]))
            if mb.size:
                b_base.append(_bss(p[mb], y[mb], r_base[mb]))
    out["ic95_bss_teorico"], out["ic95_bss_taxa_base"] = _ic(b_teo), _ic(b_base)
    inc = np.array([x.incerteza if x.incerteza is not None else np.nan for x in prev])
    mi = np.isfinite(inc)
    out["incerteza_media"] = float(inc[mi].mean()) if mi.any() else None
    err = (p - y) ** 2
    out["corr_incerteza_erro"] = (float(np.corrcoef(inc[mi], err[mi])[0, 1])
                                  if mi.sum() >= 3 and np.std(inc[mi]) > 0 and np.std(err[mi]) > 0 else None)
    return out


# =========================================================================================== recalibracao
@dataclass(frozen=True)
class Recalibrador:
    """``p_cal = f(p)`` ajustado so com o passado de ``t_corte_ns``."""
    t_corte_ns: int
    n: int
    metodo: str  # "isotonica" | "encolhimento"
    taxa_base: float
    peso_bruto: float  # encolhimento: peso do p bruto; isotonica: 1.0
    _iso: IsotonicRegression | None = None

    def __call__(self, p: float) -> float:
        return float(self.transformar(np.array([p]))[0])

    def transformar(self, p: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(p, float), 0.0, 1.0)
        if self._iso is not None:
            return np.clip(self._iso.predict(p), 0.0, 1.0)
        return self.peso_bruto * p + (1.0 - self.peso_bruto) * self.taxa_base


def recalibrador_walk_forward(previsoes: Iterable[Mapping[str, Any]], t: int, *, n0: int = N0_ENCOLHIMENTO,
                              taxa_padrao: float = TAXA_PADRAO, minimo: int = MIN_CALIBRACAO) -> Recalibrador:
    """Recalibrador no instante ``t`` (ns): so previsoes com ``resolvido_ns < t`` (sem resolucao registrada nao
    entram: o desfecho nao e conhecido). Isotonica a partir de ``minimo``; antes, encolhimento para a taxa-base."""
    passado = [x for x in resolvidas(previsoes) if x.resolvido_ns is not None and x.resolvido_ns < t]
    n = len(passado)
    if n == 0:
        return Recalibrador(t_corte_ns=t, n=0, metodo="encolhimento", taxa_base=taxa_padrao, peso_bruto=0.0)
    p = np.array([x.p for x in passado])
    y = np.array([x.y for x in passado], float)
    base = float(y.mean())
    if n >= minimo and 0 < y.sum() < n:
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(p, y)
        return Recalibrador(t_corte_ns=t, n=n, metodo="isotonica", taxa_base=base, peso_bruto=1.0, _iso=iso)
    return Recalibrador(t_corte_ns=t, n=n, metodo="encolhimento", taxa_base=base, peso_bruto=n / (n + n0))


def p_cal_walk_forward(previsoes: Sequence[Mapping[str, Any]], **kw: Any) -> list[float | None]:
    """``p_cal`` de cada previsao no seu proprio ``t0_ns`` (so com o passado). None se ``p_alvo`` ausente."""
    out: list[float | None] = []
    for r in previsoes:
        p, t0 = _num(r.get("p_alvo")), r.get("t0_ns")
        if p is None or t0 is None:
            out.append(None)
            continue
        out.append(recalibrador_walk_forward(previsoes, int(t0), **kw)(p))
    return out
