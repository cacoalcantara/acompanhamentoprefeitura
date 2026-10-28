"""Pipeline incremental e causal: ticks/livros -> VetorFeatures.

Garantia de causalidade: o estado so incorpora eventos ja entregues via on_tick/on_livro, que precisam
chegar em ordem nao decrescente de tempo; vetor(t) recusa t anterior ao ultimo evento ja consumido e
nunca olha adiante. calcular_offline alimenta SOMENTE eventos com t_evento <= t antes de cada consulta.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from robowin.core.tipos import LivroSnapshot, Tick, VetorFeatures

from ._janela import JanelaSoma
from .contexto import Contexto
from .fluxo import FluxoAgressor
from .livro import FeaturesLivro
from .registro import validar_nomes, validar_vetor
from .retornos import HistoricoMeio
from .volatilidade import VolRealizada

NS = 1_000_000_000
NAN = float("nan")
AQUECIMENTO_PADRAO_NS = 600 * NS  # maior janela de retorno


class PipelineFeatures:
    def __init__(self, usar_recebido: bool = False, har_rv_dia: float = NAN,
                 abertura: str = "09:00", fechamento: str = "18:25", desligadas: Iterable[str] = ()):
        """usar_recebido=True usa t_recebido_ns (momento em que o dado ficou disponivel) como tempo do evento.
        `desligadas`: features que o instrumento/plataforma nao alimenta (saem NaN; ver
        `registro.features_desligadas_instrumento`)."""
        self.desligadas = frozenset(desligadas)
        validar_nomes(self.desligadas)
        self._cotacoes = JanelaSoma(30 * NS, 1)
        self.usar_recebido = usar_recebido
        self.har_rv_dia = har_rv_dia
        self._fluxo = FluxoAgressor()
        self._livro = FeaturesLivro()
        self._ret = HistoricoMeio()
        self._vol = VolRealizada()
        self._ctx = Contexto(abertura, fechamento)
        self._spread = NAN
        self._ultimo_ns: int | None = None
        self.t_primeiro_ns: int | None = None

    def definir_har(self, valor: float) -> None:
        """Previsao HAR-RV do dia, calculada antes da abertura com pregoes anteriores."""
        self.har_rv_dia = valor

    def _avancar(self, t_ns: int) -> None:
        if self._ultimo_ns is not None and t_ns < self._ultimo_ns:
            raise ValueError(f"evento fora de ordem: {t_ns} < {self._ultimo_ns}")
        self._ultimo_ns = t_ns
        if self.t_primeiro_ns is None:
            self.t_primeiro_ns = t_ns

    def tempo_evento(self, tick: Tick) -> int:
        return tick.t_recebido_ns if self.usar_recebido else tick.t_utc_ns

    def on_tick(self, tick: Tick) -> None:
        t = self.tempo_evento(tick)
        self._avancar(t)
        if tick.bid > 0 and tick.ask > 0 and tick.ask >= tick.bid:
            self._spread = tick.ask - tick.bid
            self._cotacoes.adicionar(t, 1.0)
        meio = tick.meio
        if meio > 0:
            self._ret.atualizar(t, meio)
            self._vol.atualizar(t, meio)
        self._fluxo.atualizar(t, tick)

    def on_livro(self, livro: LivroSnapshot) -> None:
        self._avancar(livro.t_utc_ns)
        self._livro.atualizar(livro.t_utc_ns, livro)

    def aquecido(self, t_ns: int, ns: int = AQUECIMENTO_PADRAO_NS) -> bool:
        return self.t_primeiro_ns is not None and t_ns - self.t_primeiro_ns >= ns

    def vetor(self, t_ns: int) -> VetorFeatures:
        if self._ultimo_ns is not None and t_ns < self._ultimo_ns:
            raise ValueError("vetor(t) pedido antes do ultimo evento consumido: violaria a causalidade")
        v: dict[str, float] = {}
        v.update(self._fluxo.valores(t_ns))
        v.update(self._livro.valores(t_ns))
        v["spread_pts"] = self._spread
        v.update(self._ret.valores(t_ns))
        v.update(self._vol.valores(t_ns))
        v["har_rv_dia"] = self.har_rv_dia
        v.update(self._ctx.valores(t_ns))
        v["volume_tick_30s"] = self._cotacoes.somas(t_ns)[0] / 30.0
        for n in self.desligadas:
            v[n] = NAN
        validar_vetor(v)
        return VetorFeatures(t_ns=t_ns, valores=v)


def calcular_offline(ticks: Sequence[Tick], livros: Sequence[LivroSnapshot], instantes: Iterable[int],
                     **kw) -> list[VetorFeatures]:
    """Calcula o vetor em cada instante (ordenado) consumindo apenas eventos com t_evento <= instante."""
    pipe = PipelineFeatures(**kw)
    eventos: list[tuple[int, int, object]] = [(pipe.tempo_evento(t), 0, t) for t in ticks]
    eventos += [(lv.t_utc_ns, 1, lv) for lv in livros]
    eventos.sort(key=lambda e: (e[0], e[1]))
    out: list[VetorFeatures] = []
    i, n = 0, len(eventos)
    anterior: int | None = None
    for t in instantes:
        if anterior is not None and t < anterior:
            raise ValueError("instantes devem estar em ordem crescente")
        anterior = t
        while i < n and eventos[i][0] <= t:
            ev = eventos[i][2]
            if isinstance(ev, Tick):
                pipe.on_tick(ev)
            else:
                pipe.on_livro(ev)  # type: ignore[arg-type]
            i += 1
        out.append(pipe.vetor(t))
    return out
