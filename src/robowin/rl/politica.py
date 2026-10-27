"""Adaptadores que implementam `PoliticaExecucao` (escolher(estado) -> str) para os agentes de RL.

Vocabulario de saida: "agredir", "passiva" (= apregoar no melhor preco) e "aguardar". ATENCAO: o roteador
atual traduz via `execucao.politicas.tipo_de_ordem`, que mapeia escolha desconhecida ("aguardar") para
mercado; o roteador precisa tratar "aguardar" (nao enviar nada neste passo) antes de usar estas politicas.

Saidas de protecao (`estado["saida_protecao"] > 0`): o RL fica DESLIGADO; sempre "agredir", propensao 1.

`PoliticaAtribuicaoAleatoria` sorteia, POR ORDEM-MAE, entre a politica padrao e a politica RL (propensao de
atribuicao registrada) e registra, a cada decisao, a propensao da acao dentro da politica atribuida. A
propensao conjunta (atribuicao x acao) e o que a avaliacao off-policy por IPS deve usar.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..execucao.politicas import AGREDIR, PASSIVA, PoliticaExecucao, PoliticaPadrao
from .ambiente import AGREDIR as A_AGREDIR
from .ambiente import AGUARDAR as A_AGUARDAR
from .ambiente import APREGOAR as A_APREGOAR
from .ambiente import N_ACOES
from .q_tabular import Discretizador, QTabularFinito

AGUARDAR = "aguardar"
ACAO_PARA_ESCOLHA = {A_AGREDIR: AGREDIR, A_APREGOAR: PASSIVA, A_AGUARDAR: AGUARDAR}
ESCOLHA_PARA_ACAO = {v: k for k, v in ACAO_PARA_ESCOLHA.items()}
CHAVES_CONTEXTO = ("spread_ticks", "desequilibrio", "tempo_restante", "deriva_ticks")


def contexto(estado: dict[str, float]) -> np.ndarray:
    """Vetor de contexto do bandit: [1, spread_ticks, desequilibrio, tempo_restante, deriva_ticks]."""
    return np.array([1.0] + [float(estado.get(k, 0.0)) for k in CHAVES_CONTEXTO])


def _protecao(estado: dict[str, float]) -> bool:
    return estado.get("saida_protecao", 0.0) > 0


class PoliticaBandit:
    """Politica sobre um BanditThompsonLinear (contexto = `contexto(estado)`)."""
    nome = "rl_bandit"

    def __init__(self, bandit) -> None:
        self.bandit = bandit
        self.ultima_propensao = 1.0
        self.ultima_acao = A_AGREDIR

    def probabilidades(self, estado: dict[str, float]) -> np.ndarray:
        if _protecao(estado):
            p = np.zeros(N_ACOES)
            p[A_AGREDIR] = 1.0
            return p
        return self.bandit.probabilidades(contexto(estado))

    def escolher(self, estado: dict[str, float]) -> str:
        if _protecao(estado):
            self.ultima_acao, self.ultima_propensao = A_AGREDIR, 1.0
            return AGREDIR
        a, p = self.bandit.escolher(contexto(estado))
        self.ultima_acao, self.ultima_propensao = a, p
        return ACAO_PARA_ESCOLHA[a]


class PoliticaQ:
    """Politica epsilon-gulosa sobre um QTabularFinito; estado = discretizacao de `chaves` de `estado`."""
    nome = "rl_q_tabular"

    def __init__(self, q: QTabularFinito, disc: Discretizador, chaves: tuple[str, ...] = ("spread_ticks", "deriva_ticks"),
                 epsilon: float = 0.1) -> None:
        self.q, self.disc, self.chaves, self.epsilon = q, disc, chaves, epsilon
        self.ultima_propensao = 1.0
        self.ultima_acao = A_AGREDIR

    def _hs(self, estado: dict[str, float]) -> tuple[int, int]:
        h = min(self.q.H - 1, int((1.0 - estado.get("tempo_restante", 1.0)) * self.q.H))
        return h, self.disc([estado.get(k, 0.0) for k in self.chaves])

    def probabilidades(self, estado: dict[str, float]) -> np.ndarray:
        if _protecao(estado):
            p = np.zeros(N_ACOES)
            p[A_AGREDIR] = 1.0
            return p
        h, s = self._hs(estado)
        return self.q.probabilidades(h, s, self.epsilon)

    def escolher(self, estado: dict[str, float]) -> str:
        if _protecao(estado):
            self.ultima_acao, self.ultima_propensao = A_AGREDIR, 1.0
            return AGREDIR
        h, s = self._hs(estado)
        a, p = self.q.escolher(h, s, self.epsilon)
        self.ultima_acao, self.ultima_propensao = a, p
        return ACAO_PARA_ESCOLHA[a]


@dataclass(frozen=True)
class RegistroPropensao:
    ordem_mae_id: str
    braco: str                  # "padrao" | "rl" | "protecao"
    propensao_atribuicao: float  # P(braco) ; 1.0 em saida de protecao (nao randomizada)
    escolha: str
    propensao_acao: float       # P(escolha | estado, braco)
    estado: dict[str, float]

    @property
    def propensao(self) -> float:
        return self.propensao_atribuicao * self.propensao_acao


class PoliticaAtribuicaoAleatoria:
    """Sorteia padrao x RL por ordem-mae (seed deterministica) e registra todas as propensoes."""
    nome = "atribuicao_aleatoria"

    def __init__(self, rl: PoliticaExecucao, padrao: PoliticaExecucao | None = None, p_rl: float = 0.5,
                 seed: int = 0) -> None:
        if not 0.0 < p_rl < 1.0:
            raise ValueError("p_rl em (0,1): ambos os bracos precisam de suporte")
        self.rl, self.padrao, self.p_rl = rl, padrao or PoliticaPadrao(), p_rl
        self._rng = np.random.default_rng(seed)
        self.atribuicao: dict[str, str] = {}
        self.registros: list[RegistroPropensao] = []
        self._atual: str | None = None
        self._n = 0

    def iniciar_ordem_mae(self, ordem_mae_id: str) -> str:
        """Define (uma unica vez por ordem-mae) o braco sorteado e o torna o corrente."""
        if ordem_mae_id not in self.atribuicao:
            self.atribuicao[ordem_mae_id] = "rl" if self._rng.random() < self.p_rl else "padrao"
        self._atual = ordem_mae_id
        return self.atribuicao[ordem_mae_id]

    def escolher(self, estado: dict[str, float]) -> str:
        if self._atual is None:
            self._n += 1
            self.iniciar_ordem_mae(f"implicita-{self._n}")
        oid = self._atual
        assert oid is not None
        if _protecao(estado):  # RL desligado; sem randomizacao
            self.registros.append(RegistroPropensao(oid, "protecao", 1.0, AGREDIR, 1.0, dict(estado)))
            return AGREDIR
        braco = self.atribuicao[oid]
        pol = self.rl if braco == "rl" else self.padrao
        escolha = pol.escolher(estado)
        p_acao = float(getattr(pol, "ultima_propensao", 1.0))  # padrao e deterministica: 1.0
        p_atrib = self.p_rl if braco == "rl" else 1.0 - self.p_rl
        self.registros.append(RegistroPropensao(oid, braco, p_atrib, escolha, p_acao, dict(estado)))
        return escolha
