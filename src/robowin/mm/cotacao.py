"""CotadorMM: implementa `EstrategiaMM.cotar(t_ns, estado) -> {"bid": preco|None, "ask": preco|None}`.

O retorno e o conjunto de cotacoes que devem estar VIVAS no livro depois desta chamada (None = sem ordem
naquele lado). As acoes necessarias (nova / modificacao / cancelamento) ficam em `ultimas_mensagens`.

Regras rigidas (nao negociaveis):
* estoque |q| <= q_max (1): sem bid com q >= q_max, sem ask com q <= -q_max; quantidade sempre 1;
* no maximo UMA ordem por lado; cotacao so ate `afastamento_max_ticks` do topo -- NUNCA ordens-isca;
* ORCAMENTO DE MENSAGENS (novas + modificacoes + cancelamentos): `mensagens_min_max` por minuto e
  `mensagens_sessao_max` por sessao (proprio) E os limites do `MonitorAntimanipulacao` (mensagens/min,
  franquia diaria, taxa de cancelamento). Sem orcamento, a cotacao simplesmente NAO e alterada;
* tempo minimo de 2 s no livro: nenhuma ordem e alterada/cancelada antes disso (sem excecao, nem por
  toxicidade -- o custo disso e maior exposicao a selecao adversa, que os markouts medem);
* mudanca de preco so quando a cotacao-alvo difere da viva em >= `histerese_ticks` (poupa mensagens);
* toxicidade (estado["toxicidade"] em [0,1]) >= limiar => cancelar as duas pontas e nao recotar.

estado: microprice, melhor_bid, melhor_ask, estoque (int assinado), sigma (pts/sqrt(s)), deriva (pts, opcional),
toxicidade (opcional).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from ..core.tipos import Lado
from ..risco.antimanipulacao import LimitesAntimanipulacao, MonitorAntimanipulacao
from .avellaneda_stoikov import ParamsAS, cotacoes_continuas, discretizar
from .config import ConfigMM

NS = 1_000_000_000


@dataclass(frozen=True)
class Mensagem:
    tipo: str          # "nova" | "modificacao" | "cancelamento"
    lado: str          # "bid" | "ask"
    ordem_id: str
    preco: float | None
    t_ns: int


@dataclass
class _Viva:
    ordem_id: str
    preco: float
    t_ns: int


@dataclass
class CotadorMM:
    params: ParamsAS
    cfg: ConfigMM = field(default_factory=ConfigMM)
    monitor: MonitorAntimanipulacao = field(default_factory=lambda: MonitorAntimanipulacao(LimitesAntimanipulacao()))
    braco: str = "mm"
    limiar_toxicidade: float = 0.7
    histerese_ticks: int = 1
    vivas: dict[str, _Viva] = field(default_factory=dict)
    historico: list[Mensagem] = field(default_factory=list)
    ultimas_mensagens: list[Mensagem] = field(default_factory=list)
    _janela: deque[int] = field(default_factory=deque)
    _n: int = 0

    nome = "avellaneda_stoikov"

    # ------------------------------------------------------------------ orcamento -------------------
    def _cabe(self, t_ns: int) -> bool:
        while self._janela and self._janela[0] < t_ns - 60 * NS:
            self._janela.popleft()
        if len(self._janela) >= self.cfg.mensagens_min_max:
            return False
        if len(self.historico) >= self.cfg.mensagens_sessao_max:
            return False
        return self.monitor.pode_enviar(self.braco, t_ns)[0]

    def _emitir(self, tipo: str, lado: str, oid: str, preco: float | None, t_ns: int) -> None:
        m = Mensagem(tipo, lado, oid, preco, t_ns)
        self.historico.append(m)
        self.ultimas_mensagens.append(m)
        self._janela.append(t_ns)

    @staticmethod
    def _lado(lado: str) -> Lado:
        return Lado.COMPRA if lado == "bid" else Lado.VENDA

    def _pode_mexer(self, viva: _Viva, t_ns: int) -> bool:
        """Tempo minimo no livro contado desde a ULTIMA colocacao/alteracao (alterar preco perde a prioridade)."""
        if t_ns - viva.t_ns < int(self.monitor.limites.tempo_min_livro_s * NS):
            return False
        return self.monitor.pode_cancelar(viva.ordem_id, t_ns)[0]

    # ------------------------------------------------------------------ alvo ----------------------------
    def alvo(self, estado: dict[str, float]) -> dict[str, float | None]:
        if estado.get("toxicidade", 0.0) >= self.limiar_toxicidade:
            return {"bid": None, "ask": None}
        q = int(estado.get("estoque", 0))
        s = estado["microprice"] + estado.get("deriva", 0.0)
        b, a = cotacoes_continuas(s, q, max(estado.get("sigma", 1.0), 1e-9), self.params)
        b, a = discretizar(b, a, estado["melhor_bid"], estado["melhor_ask"], self.params)
        return {"bid": b, "ask": a}

    # ------------------------------------------------------------------ EstrategiaMM --------------------
    def cotar(self, t_ns: int, estado: dict[str, float]) -> dict[str, float | None]:
        self.ultimas_mensagens = []
        alvo = self.alvo(estado)
        bloqueado = self.monitor.bloqueado(self.braco, t_ns)
        for lado in ("bid", "ask"):
            viva, desejado = self.vivas.get(lado), alvo[lado]
            if viva is None:
                if desejado is not None and not bloqueado and self._cabe(t_ns):
                    self._n += 1
                    oid = f"{self.braco}-{self._n}"
                    self.vivas[lado] = _Viva(oid, desejado, t_ns)
                    self.monitor.registrar_nova(oid, self.braco, self._lado(lado), t_ns)
                    self._emitir("nova", lado, oid, desejado, t_ns)
                continue
            if desejado is None:
                if self._pode_mexer(viva, t_ns) and self._cabe(t_ns):
                    self.monitor.registrar_cancelamento(viva.ordem_id, t_ns)
                    self._emitir("cancelamento", lado, viva.ordem_id, None, t_ns)
                    del self.vivas[lado]
            elif abs(desejado - viva.preco) >= self.histerese_ticks * self.params.tick - 1e-9:
                if self._pode_mexer(viva, t_ns) and self._cabe(t_ns):
                    self.monitor.registrar_alteracao(viva.ordem_id, t_ns)
                    self._emitir("modificacao", lado, viva.ordem_id, desejado, t_ns)
                    viva.preco = desejado
                    viva.t_ns = t_ns  # alterar preco perde a prioridade: novo tempo minimo no livro
        return {"bid": self.vivas["bid"].preco if "bid" in self.vivas else None,
                "ask": self.vivas["ask"].preco if "ask" in self.vivas else None}

    def registrar_execucao(self, lado: str, t_ns: int) -> None:
        """Informa que a ordem do lado ("bid"/"ask") foi executada (some do livro)."""
        v = self.vivas.pop(lado, None)
        if v is not None:
            self.monitor.registrar_execucao(v.ordem_id, self.braco, self._lado(lado), t_ns)

    def contagem(self) -> dict[str, int]:
        c = {"nova": 0, "modificacao": 0, "cancelamento": 0}
        for m in self.historico:
            c[m.tipo] += 1
        return c
