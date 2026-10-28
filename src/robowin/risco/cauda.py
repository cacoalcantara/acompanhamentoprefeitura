"""VaR e Expected Shortfall (ES) HISTORICOS: metrica de MONITORAMENTO da cauda de perdas diarias.

*** NUNCA e um limite nem um veto. *** Quem veta e o ``MotorRiscoPadrao`` com os limites FIXOS de
``config/risco.toml`` (perda por operacao/dia/semana, drawdown, stop obrigatorio...). Este modulo nao e importado
pelo motor de risco, pelo OMS nem pelo roteador (um teste estatico confere), e nenhuma saida daqui reduz ou
aumenta posicao. Serve para a pessoa ver, nos relatorios, se a cauda observada de um braco/estrategia ja ameaca os
limites aprovados, e para perceber quando o modelo de risco esta subestimando perdas (teste de Kupiec).

Inspirado (so no conceito) em ``aurum/risk/var.py`` do Aurum (MIT); reimplementado para P&L diario em moeda do
instrumento. Convencao: P&L positivo = ganho; VaR e ES saem como PERDA positiva (R$). Com poucos dias a estimativa
e instavel: ``relatorio_cauda`` avisa quando a amostra e curta e nunca esconde isso.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ..core.tipos import ConfigRisco

MIN_DIAS_VAR_95 = 60  # abaixo disso a cauda de 5% tem < 3 observacoes
MIN_DIAS_VAR_99 = 250  # abaixo disso a cauda de 1% tem < 3 observacoes


def _limpo(pnl: Sequence[float] | np.ndarray) -> np.ndarray:
    a = np.asarray(pnl, dtype=float)
    return a[np.isfinite(a)]


def var_es_historico(pnl: Sequence[float] | np.ndarray, nivel: float = 0.95) -> tuple[float, float]:
    """(VaR, ES) historicos no ``nivel`` (ex.: 0,95), como PERDA positiva. Serie vazia => ValueError."""
    if not 0.5 < nivel < 1.0:
        raise ValueError("nivel deve estar em (0,5; 1)")
    a = _limpo(pnl)
    if a.size == 0:
        raise ValueError("serie de P&L vazia")
    q = float(np.quantile(a, 1.0 - nivel))
    cauda = a[a <= q]
    var = max(-q, 0.0)
    es = max(-float(cauda.mean()), 0.0) if cauda.size else var
    return var, max(es, var)


def _chi2_1_sf(x: float) -> float:
    """P(chi2 com 1 grau de liberdade > x), sem scipy: erfc(sqrt(x/2))."""
    return math.erfc(math.sqrt(max(x, 0.0) / 2.0))


def kupiec_pof(n_obs: int, n_violacoes: int, nivel: float = 0.95) -> dict[str, float]:
    """Teste de cobertura incondicional de Kupiec: as violacoes do VaR ocorrem na frequencia 1-nivel?

    ``p_valor`` baixo => a taxa de violacoes difere da esperada (VaR mal calibrado). Amostra pequena tem pouco poder."""
    if n_obs <= 0 or not 0 <= n_violacoes <= n_obs:
        raise ValueError("n_obs/n_violacoes invalidos")
    p = 1.0 - nivel
    x = n_violacoes

    def ll(q: float) -> float:
        a = 0.0 if x == 0 else x * math.log(q)
        b = 0.0 if x == n_obs else (n_obs - x) * math.log(1.0 - q)
        return a + b

    taxa = x / n_obs
    lr = -2.0 * (ll(p) - ll(min(max(taxa, 1e-12), 1.0 - 1e-12)))
    lr = max(lr, 0.0)
    return {"n_obs": float(n_obs), "violacoes": float(x), "taxa": taxa, "esperada": p, "lr": lr,
            "p_valor": _chi2_1_sf(lr)}


def backtest_var_deslizante(pnl: Sequence[float], janela: int = 60, nivel: float = 0.95) -> dict[str, float]:
    """VaR historico estimado SO com os ``janela`` dias anteriores (sem olhar o dia avaliado); conta violacoes."""
    a = _limpo(pnl)
    if janela < 20 or a.size <= janela:
        raise ValueError("serie curta demais para o backtest do VaR")
    viol = 0
    for i in range(janela, a.size):
        var, _ = var_es_historico(a[i - janela:i], nivel)
        if -a[i] > var:
            viol += 1
    return kupiec_pof(a.size - janela, viol, nivel)


@dataclass(frozen=True)
class RelatorioCauda:
    dias: int
    niveis: dict[str, dict[str, float]]
    pior_dia_brl: float
    avisos: tuple[str, ...]
    alertas_limites: tuple[str, ...]  # comparacao INFORMATIVA com os limites aprovados; nao e veto

    def como_dict(self) -> dict[str, object]:
        return {"dias": self.dias, "niveis": self.niveis, "pior_dia_brl": self.pior_dia_brl,
                "avisos": list(self.avisos), "alertas_limites": list(self.alertas_limites)}


def relatorio_cauda(pnl_diario: Sequence[float], cfg: ConfigRisco | None = None,
                    niveis: tuple[float, ...] = (0.95, 0.99)) -> RelatorioCauda:
    """VaR/ES por nivel + avisos de amostra curta + comparacao informativa com ``perda_max_dia_brl`` e ``dd_max_brl``."""
    a = _limpo(pnl_diario)
    avisos: list[str] = []
    if a.size == 0:
        return RelatorioCauda(0, {}, 0.0, ("sem dias de P&L: VaR/ES indisponiveis",), ())
    out: dict[str, dict[str, float]] = {}
    alertas: list[str] = []
    for nv in niveis:
        var, es = var_es_historico(a, nv)
        out[f"{nv:.2f}"] = {"var_brl": var, "es_brl": es}
        minimo = MIN_DIAS_VAR_95 if nv < 0.99 else MIN_DIAS_VAR_99
        if a.size < minimo:
            avisos.append(f"amostra curta para o nivel {nv:.2f}: {a.size} dias (< {minimo}); estimativa instavel")
        if cfg is not None:
            if es > cfg.perda_max_dia_brl:
                alertas.append(f"ES {nv:.2f} historico (R$ {es:.2f}) acima de perda_max_dia_brl "
                               f"(R$ {cfg.perda_max_dia_brl:.2f}): informativo; o veto continua sendo o motor de risco")
    if cfg is not None and -float(a.min()) > cfg.perda_max_dia_brl:
        alertas.append("houve dia com perda acima de perda_max_dia_brl: conferir se o limite foi respeitado "
                       "(deveria ter parado as entradas)")
    avisos.append("VaR/ES historicos descrevem o passado observado; nao limitam nem preveem perdas futuras")
    return RelatorioCauda(int(a.size), out, -float(a.min()), tuple(avisos), tuple(alertas))
