"""Sinal StatArb padrao (implementa `SinalStatArb`): z-score do residuo WIN x referencias, direcional para o WIN.

Status segue o registro de modelos: 'nao_treinado' -> 'em_validacao' (treinar) -> 'validado_oos' | 'reprovado'
(validar_oos). Enquanto nao validado, o sinal direcional e sempre 0.0 (os z-scores continuam sendo reportados).
A atualizacao e incremental e causal: `sinal(t, precos)` so usa precos ate t.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

from .base import BaseWinVista
from .cointegracao import ResultadoEG, engle_granger


class SinalStatArbPadrao:
    nome = "statarb_padrao"

    def __init__(self, alvo: str = "WIN", referencias: Sequence[str] = ("BOVA11",), z_entrada: float = 2.0,
                 alfa: float = 0.05, meia_vida_max: float = 500.0, delta: float = 1e-4, var_obs: float = 1.0,
                 halflife_z: float = 60.0, min_obs: int = 30) -> None:
        self.alvo = alvo
        self.referencias = tuple(referencias)
        self.z_entrada = z_entrada
        self.alfa = alfa
        self.meia_vida_max = meia_vida_max
        self._cfg = dict(delta=delta, var_obs=var_obs, halflife_z=halflife_z, min_obs=min_obs)
        self.status = "nao_treinado"
        self.ativas: tuple[str, ...] = ()
        self.t_ns = 0
        self._bases = {r: BaseWinVista(**self._cfg) for r in self.referencias}

    def _reiniciar(self, betas: Mapping[str, float] | None = None) -> None:
        betas = betas or {}
        self._bases = {r: BaseWinVista(beta0=betas.get(r, 0.0), **self._cfg) for r in self.referencias}

    def treinar(self, historico: Mapping[str, np.ndarray]) -> dict[str, ResultadoEG]:
        """Engle-Granger do alvo contra cada referencia no historico de treino; aquece os filtros (causal)."""
        res: dict[str, ResultadoEG] = {}
        y = np.asarray(historico[self.alvo], dtype=float)
        for r in self.referencias:
            if r in historico:
                res[r] = engle_granger(y, np.asarray(historico[r], dtype=float), self.alfa, self.meia_vida_max)
        self.ativas = tuple(r for r, e in res.items() if e.cointegrado)
        self._reiniciar({r: e.betas[0] for r, e in res.items()})
        for i in range(len(y)):
            for r in res:
                self._bases[r].atualizar(float(y[i]), float(historico[r][i]))
        self.status = "em_validacao"
        return res

    def validar_oos(self, oos: Mapping[str, np.ndarray]) -> dict[str, ResultadoEG]:
        """Reteste fora da amostra; mantem so referencias que continuam cointegradas. Sem nenhuma -> 'reprovado'."""
        if self.status == "nao_treinado":
            raise RuntimeError("treinar() antes de validar_oos()")
        y = np.asarray(oos[self.alvo], dtype=float)
        res = {r: engle_granger(y, np.asarray(oos[r], dtype=float), self.alfa, self.meia_vida_max)
               for r in self.ativas if r in oos}
        self.ativas = tuple(r for r, e in res.items() if e.cointegrado)
        self.status = "validado_oos" if self.ativas else "reprovado"
        return res

    def sinal(self, t_ns: int, precos: dict[str, float]) -> dict[str, float]:
        self.t_ns = t_ns
        saida: dict[str, float] = {}
        zs: list[float] = []
        if self.alvo in precos:
            for r, base in self._bases.items():
                if r in precos:
                    est = base.atualizar(precos[self.alvo], precos[r])
                    saida[f"z_{r}"] = est.z
                    saida[f"beta_{r}"] = est.beta
                    if r in self.ativas:
                        zs.append(est.z)
        z = float(np.mean(zs)) if zs else 0.0
        direcional = 0.0
        if self.status == "validado_oos" and abs(z) >= self.z_entrada:
            direcional = float(np.clip(-z / (2 * self.z_entrada), -1.0, 1.0))  # WIN caro -> vender
        saida["z"] = z
        saida["direcional_win"] = direcional
        saida["validado"] = 1.0 if self.status == "validado_oos" else 0.0
        return saida
