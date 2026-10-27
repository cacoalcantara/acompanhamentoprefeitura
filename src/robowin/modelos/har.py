"""HAR-RV de Corsi (2009): RV(t+1) = b0 + bd*RV(t) + bw*RV_semana(t) + bm*RV_mes(t) + e, por MQO.

RV_semana e RV_mes sao os niveis medios de 5 e 22 pregoes anteriores (componentes heterogeneos).
Sem busca de parametros: as defasagens sao fixas.
"""
from __future__ import annotations

import numpy as np

SEMANA = 5
MES = 22


def _regressores(rv: np.ndarray) -> np.ndarray:
    """Linhas [1, d, w, m] para t = MES-1 .. len-1 (usa so rv[:t+1])."""
    cs = np.concatenate([[0.0], np.cumsum(rv)])
    t = np.arange(MES - 1, len(rv))
    d = rv[t]
    w = (cs[t + 1] - cs[t + 1 - SEMANA]) / SEMANA
    m = (cs[t + 1] - cs[t + 1 - MES]) / MES
    return np.column_stack([np.ones(len(t)), d, w, m])


class HARRV:
    def __init__(self) -> None:
        self.coef: np.ndarray | None = None

    def ajustar(self, rv_diaria: np.ndarray) -> HARRV:
        rv = np.asarray(rv_diaria, float)
        if len(rv) < MES + 10:
            raise ValueError(f"HAR-RV precisa de pelo menos {MES + 10} pregoes")
        x = _regressores(rv)[:-1]  # o ultimo regressor nao tem alvo
        y = rv[MES:]
        self.coef, *_ = np.linalg.lstsq(x, y, rcond=None)
        return self

    def prever(self, rv_historico: np.ndarray) -> float:
        """Previsao da RV do proximo pregao usando apenas o historico passado."""
        if self.coef is None:
            raise RuntimeError("modelo nao ajustado")
        rv = np.asarray(rv_historico, float)
        if len(rv) < MES:
            raise ValueError(f"historico precisa de pelo menos {MES} pregoes")
        x = _regressores(rv)[-1]
        return float(max(x @ self.coef, 1e-12))
