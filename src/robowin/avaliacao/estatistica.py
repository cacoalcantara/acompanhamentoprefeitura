"""Estatistica de avaliacao: ICs, amostras, bootstrap, DSR, SPRT e CUSUM."""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
from scipy import stats

EULER_MASCHERONI = 0.5772156649015329


# ---------------------------------------------------------------- proporcoes
def ic_wilson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    z = stats.norm.ppf(0.5 + conf / 2)
    p = k / n
    den = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / den
    meia = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centro - meia), min(1.0, centro + meia))


def ic_clopper_pearson(k: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    a = 1 - conf
    lo = 0.0 if k == 0 else float(stats.beta.ppf(a / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(stats.beta.ppf(1 - a / 2, k + 1, n - k))
    return (lo, hi)


def tamanho_amostra_proporcao(p: float, margem: float, conf: float = 0.95) -> int:
    """n para IC (aproximacao normal) de meia-largura `margem` em torno de p."""
    z = stats.norm.ppf(0.5 + conf / 2)
    return math.ceil(z * z * p * (1 - p) / (margem * margem) - 1e-9)


def teste_proporcao_unilateral(k: int, n: int, p0: float) -> dict[str, float]:
    """H0: p <= p0 contra H1: p > p0. Binomial exato e aproximacao normal (z)."""
    p_exato = float(stats.binomtest(k, n, p0, alternative="greater").pvalue) if n > 0 else 1.0
    p_hat = k / n if n else 0.0
    se = math.sqrt(p0 * (1 - p0) / n) if n else math.inf
    z = (p_hat - p0) / se if se else 0.0
    return {"p_hat": p_hat, "z": z, "p_valor_normal": float(stats.norm.sf(z)), "p_valor_exato": p_exato}


def n_proporcao_poder(p0: float, p1: float, alfa: float = 0.05, poder: float = 0.80,
                      bilateral: bool = False) -> int:
    """n para detectar p1 > p0 (teste z de uma proporcao)."""
    za = stats.norm.ppf(1 - (alfa / 2 if bilateral else alfa))
    zb = stats.norm.ppf(poder)
    num = za * math.sqrt(p0 * (1 - p0)) + zb * math.sqrt(p1 * (1 - p1))
    return math.ceil(num * num / ((p1 - p0) ** 2) - 1e-9)


def acerto_equilibrio(stop_pontos: float, alvo_pontos: float, custos_brl: float,
                      ponto_valor_brl: float = 0.20) -> float:
    """Taxa de acerto que zera a expectativa liquida: p*G - (1-p)*L = c, ou seja p = (L + c)/(G + L), com
    G = alvo*ponto_valor e L = stop*ponto_valor em BRL; `custos_brl` por ida e volta (tarifas + corretagem +
    slippage) e descontado UMA vez do resultado esperado (nao de cada lado)."""
    ganho = alvo_pontos * ponto_valor_brl
    perda = stop_pontos * ponto_valor_brl
    return (perda + custos_brl) / (ganho + perda)


# ---------------------------------------------------------------- expectativa
def n_expectativa(sigma: float, mde: float, alfa: float = 0.05, poder: float = 0.80,
                  bilateral: bool = True) -> int:
    za = stats.norm.ppf(1 - (alfa / 2 if bilateral else alfa))
    zb = stats.norm.ppf(poder)
    return math.ceil(((za + zb) * sigma / mde) ** 2 - 1e-9)


def poder_expectativa(n: int, sigma: float, mde: float, alfa: float = 0.05, bilateral: bool = True) -> float:
    za = stats.norm.ppf(1 - (alfa / 2 if bilateral else alfa))
    return float(stats.norm.cdf(mde * math.sqrt(n) / sigma - za))


# ---------------------------------------------------------------- bootstrap
def _rng(seed: int | np.random.Generator | None) -> np.random.Generator:
    return seed if isinstance(seed, np.random.Generator) else np.random.default_rng(seed)


def bootstrap_estacionario(x: Sequence[float], estatistica: Callable[[np.ndarray], float], n_boot: int = 2000,
                           bloco_medio: float = 5.0, seed: int | np.random.Generator | None = 0) -> np.ndarray:
    """Bootstrap estacionario de Politis-Romano: blocos circulares de comprimento geometrico
    (media `bloco_medio`). Devolve as `n_boot` replicas da estatistica."""
    a = np.asarray(x, dtype=float)
    n = a.size
    if n == 0:
        return np.array([])
    rng = _rng(seed)
    p = 1.0 / max(bloco_medio, 1.0)
    out = np.empty(n_boot)
    for b in range(n_boot):
        novo = rng.random(n) < p
        novo[0] = True
        inicios = rng.integers(n, size=n)
        idx = np.empty(n, dtype=np.int64)
        prev = 0
        for i in range(n):
            prev = inicios[i] if novo[i] else (prev + 1) % n
            idx[i] = prev
        out[b] = estatistica(a[idx])
    return out


def ic_percentil(replicas: np.ndarray, conf: float = 0.95) -> tuple[float, float]:
    r = replicas[~np.isnan(replicas)]
    if r.size == 0:
        return (math.nan, math.nan)
    a = (1 - conf) / 2
    return (float(np.quantile(r, a)), float(np.quantile(r, 1 - a)))


def _fl(x: np.ndarray) -> float:
    g, p = x[x > 0].sum(), -x[x < 0].sum()
    if p == 0:
        return math.inf if g > 0 else math.nan
    return float(g / p)


def bootstrap_por_pregao(pnl: Sequence[float], dias: Sequence[str], n_boot: int = 2000, conf: float = 0.95,
                         seed: int | np.random.Generator | None = 0) -> dict:
    """Reamostra PREGOES inteiros (com reposicao); preserva a dependencia intradia. Devolve IC percentil da
    expectativa por operacao e do fator de lucro, e P*(expectativa <= 0)."""
    x = np.asarray(pnl, dtype=float)
    d = np.asarray(list(dias))
    if x.size != d.size:
        raise ValueError("pnl e dias devem ter o mesmo tamanho")
    uniq = list(dict.fromkeys(d.tolist()))
    grupos = [x[d == u] for u in uniq]
    m = len(grupos)
    rng = _rng(seed)
    exp_b = np.empty(n_boot)
    fl_b = np.empty(n_boot)
    for b in range(n_boot):
        sel = rng.integers(m, size=m) if m else []
        y = np.concatenate([grupos[i] for i in sel]) if m else np.array([])
        exp_b[b] = y.mean() if y.size else math.nan
        fl_b[b] = _fl(y)
    return {
        "n": int(x.size), "pregoes": m,
        "expectativa": float(x.mean()) if x.size else math.nan,
        "expectativa_ic": ic_percentil(exp_b, conf),
        "fator_lucro": _fl(x) if x.size else math.nan,
        "fator_lucro_ic": ic_percentil(fl_b, conf),
        "p_expectativa_nao_positiva": float((exp_b <= 0).mean()),
        "replicas_expectativa": exp_b,
    }


# ---------------------------------------------------------------- Sharpe deflacionado
def sharpe(r: Sequence[float]) -> float:
    a = np.asarray(r, dtype=float)
    sd = a.std(ddof=1)
    return float(a.mean() / sd) if sd > 0 else 0.0


def sharpe_esperado_maximo(n_tentativas: int, var_sr: float) -> float:
    """SR0 de Bailey-Lopez de Prado: maximo esperado do SR sob H0 apos N tentativas independentes."""
    if n_tentativas <= 1:
        return 0.0
    g = EULER_MASCHERONI
    return math.sqrt(var_sr) * ((1 - g) * stats.norm.ppf(1 - 1 / n_tentativas)
                                + g * stats.norm.ppf(1 - 1 / (n_tentativas * math.e)))


def sharpe_probabilistico(sr: float, n: int, assimetria: float, curtose: float, sr_ref: float = 0.0) -> float:
    """PSR: P(SR verdadeiro > sr_ref). `curtose` e a bruta (normal = 3). SR no mesmo periodo dos retornos."""
    den = 1 - assimetria * sr + (curtose - 1) / 4 * sr * sr
    if n < 2 or den <= 0:
        return math.nan
    return float(stats.norm.cdf((sr - sr_ref) * math.sqrt(n - 1) / math.sqrt(den)))


def deflated_sharpe(retornos: Sequence[float], n_tentativas: int,
                    var_sr_tentativas: float | None = None) -> dict:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014). `n_tentativas` = TODAS as configuracoes
    testadas (inclui iteracoes de prompt). `var_sr_tentativas` = variancia dos SR entre as tentativas
    (no periodo dos retornos); se None usa a variancia amostral do proprio SR (aproximacao que NAO mede a
    dispersao entre tentativas: o resultado traz `var_aproximada=True` e o relatorio nao concede selo)."""
    a = np.asarray(retornos, dtype=float)
    n = a.size
    if n < 3 or a.std(ddof=1) == 0:
        return {"sr": 0.0, "sr0": 0.0, "dsr": math.nan, "n": n, "n_tentativas": n_tentativas}
    sr = sharpe(a)
    g3 = float(stats.skew(a, bias=True))
    g4 = float(stats.kurtosis(a, fisher=False, bias=True))
    var_aprox = var_sr_tentativas is None
    if var_sr_tentativas is None:
        var_sr_tentativas = max((1 - g3 * sr + (g4 - 1) / 4 * sr * sr) / (n - 1), 0.0)
    sr0 = sharpe_esperado_maximo(n_tentativas, var_sr_tentativas)
    dsr = sharpe_probabilistico(sr, n, g3, g4, sr0)
    return {"sr": sr, "sr0": sr0, "dsr": dsr, "n": n, "n_tentativas": n_tentativas,
            "assimetria": g3, "curtose": g4, "var_aproximada": var_aprox}


# ---------------------------------------------------------------- monitoramento sequencial
@dataclass(frozen=True)
class ResultadoSPRT:
    decisao: str  # "aceita_H1" | "aceita_H0" | "continua"
    indice: int | None  # observacao (1-based) em que decidiu
    llr: float
    limite_superior: float
    limite_inferior: float


def _limites_sprt(alfa: float, beta: float) -> tuple[float, float]:
    return math.log((1 - beta) / alfa), math.log(beta / (1 - alfa))


def sprt_bernoulli(sucessos: Sequence[int | bool], p0: float, p1: float, alfa: float = 0.05,
                   beta: float = 0.20) -> ResultadoSPRT:
    """SPRT de Wald: H0: p=p0 contra H1: p=p1. Para monitorar piora use p1 < p0 (aceita_H1 = piorou)."""
    sup, inf = _limites_sprt(alfa, beta)
    llr = 0.0
    for i, s in enumerate(sucessos, 1):
        llr += math.log(p1 / p0) if s else math.log((1 - p1) / (1 - p0))
        if llr >= sup:
            return ResultadoSPRT("aceita_H1", i, llr, sup, inf)
        if llr <= inf:
            return ResultadoSPRT("aceita_H0", i, llr, sup, inf)
    return ResultadoSPRT("continua", None, llr, sup, inf)


def sprt_media_normal(x: Sequence[float], mu0: float, mu1: float, sigma: float, alfa: float = 0.05,
                      beta: float = 0.20) -> ResultadoSPRT:
    """SPRT de Wald para media normal com sigma conhecido."""
    sup, inf = _limites_sprt(alfa, beta)
    llr = 0.0
    for i, v in enumerate(x, 1):
        llr += (mu1 - mu0) * (v - (mu0 + mu1) / 2) / (sigma * sigma)
        if llr >= sup:
            return ResultadoSPRT("aceita_H1", i, llr, sup, inf)
        if llr <= inf:
            return ResultadoSPRT("aceita_H0", i, llr, sup, inf)
    return ResultadoSPRT("continua", None, llr, sup, inf)


def cusum_page(x: Sequence[float], mu0: float, k: float, h: float, direcao: str = "queda") -> dict:
    """CUSUM de Page unilateral. direcao="queda": alarma quando a media cai abaixo de mu0
    (S_t = max(0, S_{t-1} + (mu0 - x_t) - k)); "alta" e o inverso. Alarma quando S_t > h."""
    sinal = 1.0 if direcao == "queda" else -1.0
    s, caminho, alarme = 0.0, [], None
    for i, v in enumerate(x, 1):
        s = max(0.0, s + sinal * (mu0 - v) - k)
        caminho.append(s)
        if alarme is None and s > h:
            alarme = i
    return {"alarme_em": alarme, "caminho": caminho, "s_final": s}
