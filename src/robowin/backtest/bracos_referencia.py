"""Bracos de REFERENCIA, simples e rotulados, para testar a infraestrutura do backtest.

Nenhum usa indicador tecnico nem le preco/feature: sao controles. NAO sao estrategias.
* `controle_aleatorio`: entra com probabilidade fixa, lado sorteado (moeda justa), stop/alvo fixos.
  Serve de linha de base do experimento (§5.4): acerto alto aqui nao significa expectativa positiva.
* `nao_operar`: nunca opera. Equity constante.
* `BracoSonda`: decide em funcao do HASH do dossie (maxima sensibilidade a qualquer dado que vaze);
  usado pelo teste de perturbacao futura. Nao e estrategia.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from robowin.core.tipos import Acao, Intencao

ROTULO_CONTROLE = "REFERENCIA: controle aleatorio (sem indicadores); nao e estrategia"
ROTULO_NAO_OPERAR = "REFERENCIA: nao operar; nao e estrategia"
NS = 1_000_000_000


class ControleAleatorio:
    """Entra a mercado com probabilidade `p_entrada` por decisao quando esta zerado."""

    rotulo = ROTULO_CONTROLE

    def __init__(self, seed: int, id: str = "controle_aleatorio", p_entrada: float = 0.01,
                 stop_pontos: float = 100.0, alvo_pontos: float | None = 100.0, tempo_max_s: int | None = 900,
                 validade_s: int = 60) -> None:
        self.id = id
        self.seed = seed
        self.p_entrada, self.stop_pontos, self.alvo_pontos = p_entrada, stop_pontos, alvo_pontos
        self.tempo_max_s, self.validade_s = tempo_max_s, validade_s
        self._rng = np.random.default_rng(seed)
        self._n = 0

    def decidir(self, t_ns: int, dossie: dict[str, Any]) -> Intencao | None:
        # sempre exatamente dois sorteios por chamada: a sequencia nao depende do estado do mercado
        u, lado = float(self._rng.random()), float(self._rng.random())
        if int(dossie.get("posicao", {}).get("quantidade", 0) or 0) > 0:
            return None  # a saida e do stop/alvo/tempo-limite/zeragem (deterministicos)
        if u >= self.p_entrada:
            return None
        self._n += 1
        return Intencao(
            id=f"{self.id}-{self._n:06d}", braco=self.id, acao=Acao.COMPRAR if lado < 0.5 else Acao.VENDER,
            quantidade=1, stop_pontos=self.stop_pontos, alvo_pontos=self.alvo_pontos,
            tempo_max_s=self.tempo_max_s, validade_ns=t_ns + self.validade_s * NS, modo="quant", t0_ns=t_ns)


class NaoOperar:
    """Sempre NAO_OPERAR (decisao registrada, nenhuma ordem)."""

    rotulo = ROTULO_NAO_OPERAR

    def __init__(self, id: str = "nao_operar", validade_s: int = 60) -> None:
        self.id = id
        self.validade_s = validade_s
        self._n = 0

    def decidir(self, t_ns: int, dossie: dict[str, Any]) -> Intencao | None:
        self._n += 1
        return Intencao(id=f"{self.id}-{self._n:06d}", braco=self.id, acao=Acao.NAO_OPERAR,
                        validade_ns=t_ns + self.validade_s * NS, modo="quant", t0_ns=t_ns)


class BracoSonda:
    """Acao = funcao do hash do dossie. Qualquer dado futuro que entre no dossie muda as decisoes."""

    rotulo = "SONDA de vazamento (nao e estrategia)"

    def __init__(self, id: str = "sonda", stop_pontos: float = 100.0, alvo_pontos: float = 100.0,
                 p_operar: float = 0.25, validade_s: int = 60) -> None:
        self.id = id
        self.stop_pontos, self.alvo_pontos, self.p_operar, self.validade_s = stop_pontos, alvo_pontos, p_operar, validade_s
        self._n = 0

    def decidir(self, t_ns: int, dossie: dict[str, Any]) -> Intencao | None:
        from robowin.ia.dossie import hash_dossie
        h = int(hashlib.sha256(hash_dossie(dossie).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        self._n += 1
        if h >= self.p_operar or int(dossie.get("posicao", {}).get("quantidade", 0) or 0) > 0:
            return Intencao(id=f"{self.id}-{self._n:06d}", braco=self.id, acao=Acao.NAO_OPERAR,
                            validade_ns=t_ns + self.validade_s * NS, modo="quant", t0_ns=t_ns)
        acao = Acao.COMPRAR if h < self.p_operar / 2 else Acao.VENDER
        return Intencao(id=f"{self.id}-{self._n:06d}", braco=self.id, acao=acao, quantidade=1,
                        stop_pontos=self.stop_pontos, alvo_pontos=self.alvo_pontos, tempo_max_s=600,
                        validade_ns=t_ns + self.validade_s * NS, modo="quant", t0_ns=t_ns)
