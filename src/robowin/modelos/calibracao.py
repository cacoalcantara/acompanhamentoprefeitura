"""Calibracao isotonica (um-contra-todos, renormalizada) e metricas de calibracao."""
from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression

MIN_CALIBRACAO = 200
PISO_PROB = 0.01  # isotonica pode devolver 0 exato: piso evita log-loss infinito


class CalibradorIsotonico:
    """Calibra probabilidades multiclasse com uma regressao isotonica por classe e renormaliza."""

    def __init__(self, n_classes: int = 3):
        self.n_classes = n_classes
        self._iso: list[IsotonicRegression | None] = [None] * n_classes
        self.ajustado = False

    def ajustar(self, p: np.ndarray, y: np.ndarray) -> CalibradorIsotonico:
        p = np.asarray(p, float)
        y = np.asarray(y)
        if len(y) >= MIN_CALIBRACAO:
            for c in range(self.n_classes):
                yc = (y == c).astype(float)
                if 0 < yc.sum() < len(yc):
                    self._iso[c] = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(p[:, c], yc)
            self.ajustado = True
        return self

    def transformar(self, p: np.ndarray) -> np.ndarray:
        p = np.asarray(p, float)
        q = np.empty_like(p)
        for c in range(self.n_classes):
            iso = self._iso[c]
            q[:, c] = iso.predict(p[:, c]) if iso is not None else p[:, c]
        s = q.sum(axis=1, keepdims=True)
        zero = s[:, 0] <= 1e-12
        q[~zero] /= s[~zero]
        q[zero] = 1.0 / self.n_classes
        q = np.maximum(q, PISO_PROB)
        return q / q.sum(axis=1, keepdims=True)


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    """Erro de calibracao esperado (binario): p = prob. prevista do evento, y em {0,1}."""
    p = np.asarray(p, float)
    y = np.asarray(y, float)
    if len(p) == 0:
        return float("nan")
    idx = np.minimum((p * bins).astype(int), bins - 1)
    tot = 0.0
    for b in range(bins):
        m = idx == b
        if m.any():
            tot += m.sum() * abs(p[m].mean() - y[m].mean())
    return tot / len(p)


def curva_confiabilidade(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[tuple[float, float, int]]:
    """Lista (prob. media prevista, frequencia observada, n) por faixa."""
    p = np.asarray(p, float)
    y = np.asarray(y, float)
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return [(float(p[idx == b].mean()), float(y[idx == b].mean()), int((idx == b).sum()))
            for b in range(bins) if (idx == b).any()]


def log_loss_multiclasse(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(np.asarray(p, float), 1e-9, 1.0)
    return float(-np.mean(np.log(p[np.arange(len(y)), np.asarray(y, int)])))


def brier(p: np.ndarray, y: np.ndarray) -> float:
    p = np.asarray(p, float)
    return float(np.mean((p - np.asarray(y, float)) ** 2))
