"""Monitoramento de deriva e de custo de execucao (SO observacao; nao decide nada).

* ``psi`` / ``psi_amostras``: Population Stability Index entre a distribuicao de uma feature (ou de uma saida de
  modelo) usada no treino/validacao e a observada depois. PSI alto = o mercado que o modelo ve mudou. Alerta para
  REVALIDAR o modelo (o registro de modelos exige novo OOS), nunca para ajustar parametro sozinho.
* ``RastreadorSlippage``: custo de execucao REAL contra o esperado pelo modelo de custos do simulador, em TICKS do
  ativo. Se a execucao na conta DEMO custa bem mais que o simulado, os resultados do backtest estao otimistas.

Ideias adaptadas de ``aurum/live/monitor.py`` (Aurum, MIT), reimplementadas em pt-BR e sem constantes de ativo
(tudo em ticks). Ligado ao ``robowin avaliar`` e ao painel por ``monitoramento_banco`` (informativo; nao altera o
veto de risco); nao roda a cada decisao do nucleo.
"""
from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

PSI_AVISO = 0.10
PSI_ALERTA = 0.25
Nivel = Literal["estavel", "aviso", "alerta", "indisponivel"]


def psi(esperado: Sequence[float] | np.ndarray, atual: Sequence[float] | np.ndarray, eps: float = 1e-4) -> float:
    """PSI entre duas distribuicoes ja agrupadas em bins (contagens ou proporcoes). Bins vazios valem ``eps``."""
    e = np.asarray(esperado, dtype=float)
    a = np.asarray(atual, dtype=float)
    if e.shape != a.shape or e.ndim != 1 or e.size == 0:
        raise ValueError("esperado e atual devem ser vetores 1-D do mesmo tamanho")
    if e.sum() <= 0 or a.sum() <= 0:
        return math.nan
    e = np.maximum(e / e.sum(), eps)
    a = np.maximum(a / a.sum(), eps)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def psi_amostras(referencia: Sequence[float], atual: Sequence[float], n_bins: int = 10) -> float:
    """PSI de duas amostras, com bins pelos quantis da REFERENCIA (valores fora dos extremos caem nos bins de ponta)."""
    r = np.asarray(referencia, dtype=float)
    x = np.asarray(atual, dtype=float)
    r, x = r[np.isfinite(r)], x[np.isfinite(x)]
    if r.size < n_bins * 5 or x.size == 0:
        return math.nan
    bordas = np.unique(np.quantile(r, np.linspace(0, 1, n_bins + 1)[1:-1]))
    if bordas.size == 0:
        return math.nan
    ce = np.bincount(np.searchsorted(bordas, r, side="right"), minlength=bordas.size + 1)
    ca = np.bincount(np.searchsorted(bordas, x, side="right"), minlength=bordas.size + 1)
    return psi(ce, ca)


def classificar_psi(valor: float) -> Nivel:
    if not math.isfinite(valor):
        return "indisponivel"
    return "alerta" if valor >= PSI_ALERTA else ("aviso" if valor >= PSI_AVISO else "estavel")


def deriva_de_features(referencia: Mapping[str, Sequence[float]], atual: Mapping[str, Sequence[float]],
                       n_bins: int = 10) -> dict[str, Any]:
    """PSI por feature presente nos dois conjuntos + o pior nivel."""
    por_feature = {k: psi_amostras(referencia[k], atual[k], n_bins) for k in sorted(referencia) if k in atual}
    niveis = {k: classificar_psi(v) for k, v in por_feature.items()}
    ordem = ("indisponivel", "estavel", "aviso", "alerta")
    pior = max(niveis.values(), key=ordem.index) if niveis else "indisponivel"
    return {"psi": por_feature, "niveis": niveis, "pior": pior}


@dataclass(frozen=True)
class RegistroSlippage:
    lado: int  # +1 compra, -1 venda
    custo_real_ticks: float
    custo_esperado_ticks: float


class RastreadorSlippage:
    """Custo real (preco de execucao contra o de referencia, em ticks) contra o esperado, numa janela deslizante."""

    def __init__(self, janela: int = 50, razao_alerta: float = 2.0, tolerancia_ticks: float = 0.25,
                 min_execucoes: int = 10) -> None:
        self.registros: deque[RegistroSlippage] = deque(maxlen=janela)
        self.razao_alerta, self.tolerancia_ticks, self.min_execucoes = razao_alerta, tolerancia_ticks, min_execucoes
        self.total = 0

    def registrar(self, lado: int, preco_execucao: float, preco_referencia: float, tick_pontos: float,
                  esperado_ticks: float) -> RegistroSlippage:
        if lado not in (1, -1) or tick_pontos <= 0:
            raise ValueError("lado deve ser +1/-1 e tick_pontos positivo")
        r = RegistroSlippage(lado, lado * (preco_execucao - preco_referencia) / tick_pontos, esperado_ticks)
        self.registros.append(r)
        self.total += 1
        return r

    def resumo(self) -> dict[str, float]:
        n = len(self.registros)
        if n == 0:
            return {"n": 0.0, "total": float(self.total)}
        real = float(np.mean([r.custo_real_ticks for r in self.registros]))
        esp = float(np.mean([r.custo_esperado_ticks for r in self.registros]))
        return {"n": float(n), "total": float(self.total), "real_ticks": real, "esperado_ticks": esp,
                "razao": real / esp if esp > 0 else math.nan}

    def alerta(self) -> str | None:
        s = self.resumo()
        if s["n"] < self.min_execucoes:
            return None
        if s["real_ticks"] > self.razao_alerta * s["esperado_ticks"] + self.tolerancia_ticks:
            return (f"custo de execucao real {s['real_ticks']:.2f} ticks vs esperado {s['esperado_ticks']:.2f} "
                    f"nas ultimas {int(s['n'])} execucoes: o simulador esta otimista; revisar o modelo de custos")
        return None
