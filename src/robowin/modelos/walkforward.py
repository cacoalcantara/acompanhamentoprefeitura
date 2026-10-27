"""Walk-forward com PURGA e EMBARGO (Lopez de Prado), sem vazamento de rotulos.

Cada amostra i tem t0[i] (instante da decisao) e t1[i] (instante em que o rotulo fica conhecido).
Regra: so entra no treino a amostra cujo rotulo termina ate `inicio_do_teste - embargo`.
Isso purga toda sobreposicao [t0, t1] com o segmento de teste e garante que nunca se treina com
rotulos que terminam depois do inicio do segmento de teste.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from .microestrutura import Amostras, ModeloMicroestrutura


class VazamentoDetectado(AssertionError):
    pass


@dataclass(frozen=True)
class Dobra:
    idx_treino: np.ndarray
    idx_teste: np.ndarray
    teste_ini_ns: int
    teste_fim_ns: int


def mascara_treino(t0: np.ndarray, t1: np.ndarray, teste_ini_ns: int, teste_fim_ns: int,
                   embargo_ns: int = 0) -> np.ndarray:
    """Amostras utilizaveis no treino para o segmento de teste [teste_ini, teste_fim].

    Mantem a amostra se terminou (rotulo conhecido) ate teste_ini - embargo, ou se comecou so depois de
    teste_fim + embargo (caso de validacao cruzada com treino posterior ao teste).
    """
    antes = t1 <= teste_ini_ns - embargo_ns
    depois = t0 >= teste_fim_ns + embargo_ns
    return antes | depois


def dividir_walkforward(t0: np.ndarray, t1: np.ndarray, n_dobras: int = 5, frac_treino_inicial: float = 0.4,
                        embargo_ns: int = 0, janela_treino_ns: int | None = None) -> list[Dobra]:
    """Dobras de teste consecutivas e sem sobreposicao; treino sempre no passado, purgado e com embargo."""
    t0 = np.asarray(t0, dtype=np.int64)
    t1 = np.asarray(t1, dtype=np.int64)
    if len(t0) != len(t1) or np.any(t1 < t0):
        raise ValueError("t1 deve ser >= t0 e de mesmo tamanho")
    n = len(t0)
    ordem = np.argsort(t0, kind="stable")
    k0 = int(n * frac_treino_inicial)
    blocos = np.array_split(ordem[k0:], n_dobras)
    out: list[Dobra] = []
    for b in blocos:
        if len(b) == 0:
            continue
        ini = int(t0[b].min())
        fim = int(t0[b].max())
        m = t1 <= ini - embargo_ns
        if janela_treino_ns is not None:
            m &= t0 >= ini - janela_treino_ns
        tr = np.flatnonzero(m)
        if len(tr) == 0:
            continue
        d = Dobra(idx_treino=tr, idx_teste=np.sort(b), teste_ini_ns=ini, teste_fim_ns=fim)
        verificar_sem_vazamento(d, t0, t1, embargo_ns)
        out.append(d)
    return out


def verificar_sem_vazamento(d: Dobra, t0: np.ndarray, t1: np.ndarray, embargo_ns: int = 0) -> None:
    """Levanta VazamentoDetectado se algum rotulo de treino termina depois do inicio do teste (menos embargo)."""
    if len(d.idx_treino) and int(t1[d.idx_treino].max()) > d.teste_ini_ns - embargo_ns:
        raise VazamentoDetectado("rotulo de treino termina depois do inicio do segmento de teste")
    if np.intersect1d(d.idx_treino, d.idx_teste).size:
        raise VazamentoDetectado("amostra no treino e no teste")


@dataclass
class ResultadoWF:
    dobras: list[Dobra]
    pred: pd.DataFrame  # previsoes fora da amostra (indice = posicao em Amostras)
    idx_oos: np.ndarray
    treinado_ate_ns: list[int]


def executar_walkforward(am: Amostras, fabrica: Callable[[], ModeloMicroestrutura], n_dobras: int = 5,
                         embargo_ns: int = 0, frac_treino_inicial: float = 0.4, origem: str = "sintetico",
                         janela_treino_ns: int | None = None) -> ResultadoWF:
    """Treina um modelo novo por dobra (parametros fixos) e junta as previsoes fora da amostra."""
    dobras = dividir_walkforward(am.t_ns, am.t_fim_ns, n_dobras, frac_treino_inicial, embargo_ns,
                                 janela_treino_ns)
    preds: list[pd.DataFrame] = []
    idxs: list[np.ndarray] = []
    ate: list[int] = []
    for d in dobras:
        m = fabrica()
        m.treinar(am.subconjunto(d.idx_treino), origem=origem)
        if m.treinado_ate_ns > d.teste_ini_ns - embargo_ns:
            raise VazamentoDetectado("modelo treinado alem do inicio do segmento de teste")
        p = m.prever_lote(am.X.iloc[d.idx_teste])
        p.index = d.idx_teste
        preds.append(p)
        idxs.append(d.idx_teste)
        ate.append(m.treinado_ate_ns)
    if not preds:
        raise ValueError("nenhuma dobra valida")
    return ResultadoWF(dobras, pd.concat(preds), np.concatenate(idxs), ate)
