"""Conversao de moeda: P&L e risco NA MOEDA DA CONTA a partir da cotacao do par de conversao NO MESMO INSTANTE.

Regra unica: nenhuma taxa fixa e inventada. O fator (unidades da moeda da conta por 1 unidade da moeda de lucro do
simbolo) vem do ponto medio da cotacao bid/ask do par de conversao lida do terminal (ou dos dados gravados) no
instante da decisao; cotacao ausente, invalida (bid/ask <= 0, ask < bid) ou velha => `None`, e quem decide
(motor de risco) BLOQUEIA entradas com motivo claro. Rotas: direta (USDBRL), inversa (BRLUSD => 1/medio) ou por
uma moeda intermediaria (JPY->BRL = 1/USDJPY x USDBRL), sempre com cotacoes do mesmo instante.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .bolsas import SemTaxaDeCambio

INTERMEDIARIAS_PADRAO = ("USD", "EUR")
# (bid, ask, t_ms) no relogio que o chamador usar de forma consistente (UTC dos dados ou o do servidor)
Cotacao = tuple[float, float, int]


class SemCotacaoConversao(SemTaxaDeCambio):
    """Sem cotacao valida do par de conversao no instante: entradas bloqueadas (nunca se usa taxa fixa)."""


@dataclass(frozen=True)
class PernaConversao:
    simbolo: str  # nome no terminal (com o sufixo da corretora, se houver)
    de: str
    para: str
    inverter: bool  # True: o simbolo cota `para`/`de` (fator = 1/medio)


@dataclass(frozen=True)
class RotaConversao:
    de: str
    para: str
    pernas: tuple[PernaConversao, ...]

    def descrever(self) -> str:
        if not self.pernas:
            return f"{self.de}->{self.para}: mesma moeda (fator 1)"
        return f"{self.de}->{self.para} via " + " x ".join(
            (f"1/{p.simbolo}" if p.inverter else p.simbolo) for p in self.pernas)


def _par(nomes: Sequence[str], base: str, cotada: str) -> str | None:
    """Simbolo do par base+cotada entre `nomes`: o exato vence; senao o mais curto com decoracao da corretora
    (prefixo/sufixo nao alfabeticos ou minusculos, ex.: EURUSD.m, EURUSDpro, m.EURUSD)."""
    alvo = f"{base}{cotada}"
    if alvo in nomes:
        return alvo
    cands = []
    for n in nomes:
        i = n.find(alvo)
        if i < 0:
            continue
        pre, suf = n[:i], n[i + len(alvo):]
        if decoracao_ok(pre, prefixo=True) and decoracao_ok(suf, prefixo=False):
            cands.append(n)
    cands.sort(key=lambda n: (len(n), n))
    return cands[0] if cands else None


def decoracao_ok(resto: str, *, prefixo: bool) -> bool:
    """Prefixo/sufixo de corretora aceitavel: vazio, minusculo (`pro`, `m`) ou iniciado (sufixo) / terminado
    (prefixo) por separador nao alfabetico (`.m`, `_i`, `m.`). Letras maiusculas coladas formariam OUTRO simbolo."""
    if not resto:
        return True
    if re.fullmatch(r"[a-z0-9]+", resto):
        return True
    borda = resto[-1] if prefixo else resto[0]
    return not borda.isalnum()


def rota_conversao(de: str, para: str, simbolos: Iterable[str],
                   intermediarias: Sequence[str] = INTERMEDIARIAS_PADRAO) -> RotaConversao | None:
    """Rota de conversao de `de` para `para` com os simbolos oferecidos pela corretora (None = sem rota)."""
    de, para = de.strip().upper(), para.strip().upper()
    if de == para:
        return RotaConversao(de, para, ())
    nomes = list(simbolos)

    def direta(a: str, b: str) -> PernaConversao | None:
        s = _par(nomes, a, b)
        if s:
            return PernaConversao(s, a, b, False)
        s = _par(nomes, b, a)
        return PernaConversao(s, a, b, True) if s else None

    p = direta(de, para)
    if p is not None:
        return RotaConversao(de, para, (p,))
    for x in intermediarias:
        x = x.upper()
        if x in (de, para):
            continue
        p1, p2 = direta(de, x), direta(x, para)
        if p1 is not None and p2 is not None:
            return RotaConversao(de, para, (p1, p2))
    return None


class ConversorMoeda:
    """Fator moeda de lucro -> moeda da conta a partir de cotacoes do mesmo instante.

    `cotacao(simbolo)` devolve (bid, ask, t_ms) ou None. `fator(t_ref_ms)` recusa cotacao com idade > `idade_max_s`
    em relacao a `t_ref_ms` (mesmo relogio); sem `t_ref_ms` so exige cotacao valida.
    """

    def __init__(self, rota: RotaConversao, cotacao: Callable[[str], Cotacao | None], *, idade_max_s: float = 120.0) -> None:
        self.rota = rota
        self._cotacao = cotacao
        self.idade_max_s = idade_max_s
        self.ultimo_fator: float | None = None
        self.ultimo_motivo = ""

    @property
    def de(self) -> str:
        return self.rota.de

    @property
    def para(self) -> str:
        return self.rota.para

    def fator(self, t_ref_ms: int | None = None) -> float | None:
        f = 1.0
        for p in self.rota.pernas:
            q = self._cotacao(p.simbolo)
            if q is None:
                self.ultimo_motivo = f"sem cotacao de {p.simbolo}"
                return None
            bid, ask, t_ms = float(q[0]), float(q[1]), int(q[2])
            if not (bid > 0 and ask > 0 and ask >= bid):
                self.ultimo_motivo = f"cotacao invalida de {p.simbolo} (bid={bid}, ask={ask})"
                return None
            if t_ref_ms is not None and abs(t_ref_ms - t_ms) > self.idade_max_s * 1000:
                self.ultimo_motivo = f"cotacao de {p.simbolo} velha ({abs(t_ref_ms - t_ms) / 1000:.0f} s)"
                return None
            medio = (bid + ask) / 2
            f *= (1.0 / medio) if p.inverter else medio
        self.ultimo_fator, self.ultimo_motivo = f, ""
        return f

    def converter(self, valor: float, t_ref_ms: int | None = None) -> float:
        f = self.fator(t_ref_ms)
        if f is None:
            raise SemCotacaoConversao(f"conversao {self.de}->{self.para}: {self.ultimo_motivo or 'sem cotacao'}")
        return valor * f

    def para_motor(self) -> Callable[[int], float | None]:
        """Adaptador para o motor de risco: recebe t_ns (UTC) e consulta com t_ref em ms do mesmo relogio."""
        return lambda t_ns: self.fator(t_ns // 1_000_000)


class FeedCotacoes:
    """Ultimas cotacoes por simbolo alimentadas por ticks (dados gravados/simulacao); fonte do `ConversorMoeda`."""

    def __init__(self) -> None:
        self._ult: dict[str, Cotacao] = {}

    def atualizar(self, simbolo: str, bid: float, ask: float, t_ms: int) -> None:
        self._ult[simbolo] = (float(bid), float(ask), int(t_ms))

    def __call__(self, simbolo: str) -> Cotacao | None:
        return self._ult.get(simbolo)


class ConversorHistorico:
    """Fator de conversao a partir de cotacoes GRAVADAS (backtest/replay): para cada instante usa a ULTIMA cotacao
    de cada perna com t <= t_ref (nunca olha o futuro) e recusa cotacao mais velha que `idade_max_s`.

    `series[simbolo]` = sequencia ordenada de (t_ns, bid, ask) no mesmo relogio (UTC) dos ticks do backtest."""

    def __init__(self, rota: RotaConversao, series: dict[str, Sequence[tuple[int, float, float]]], *,
                 idade_max_s: float = 120.0) -> None:
        import bisect
        self._bisect = bisect.bisect_right
        self.rota = rota
        self.idade_max_s = idade_max_s
        self._t = {s: [int(x[0]) for x in v] for s, v in series.items()}
        self._q = {s: list(v) for s, v in series.items()}

    def _cotacao(self, simbolo: str, t_ns: int) -> Cotacao | None:
        ts = self._t.get(simbolo)
        if not ts:
            return None
        i = self._bisect(ts, t_ns) - 1
        if i < 0:
            return None
        t, bid, ask = self._q[simbolo][i]
        return float(bid), float(ask), int(t) // 1_000_000

    def __call__(self, t_ns: int) -> float | None:
        conv = ConversorMoeda(self.rota, lambda s: self._cotacao(s, t_ns), idade_max_s=self.idade_max_s)
        return conv.fator(t_ns // 1_000_000)


def conversor_do_terminal(terminal: Any, de: str, para: str, *, idade_max_s: float = 120.0,
                          intermediarias: Sequence[str] = INTERMEDIARIAS_PADRAO) -> ConversorMoeda | None:
    """Conversor que le `symbol_info_tick` do terminal a cada consulta (relogio do SERVIDOR: t_ms = time_msc).
    None se a corretora nao oferece rota de conversao. O chamador usa `fator(t_servidor_ms)`."""
    simbolos = terminal.symbols_get() or ()
    rota = rota_conversao(de, para, [str(s.name) for s in simbolos], intermediarias)
    if rota is None:
        return None

    def cotacao(simbolo: str) -> Cotacao | None:
        try:
            terminal.symbol_select(simbolo, True)
            tk = terminal.symbol_info_tick(simbolo)
        except Exception:  # noqa: BLE001 - terminal caiu: sem cotacao (bloqueia)
            return None
        if tk is None:
            return None
        return float(tk.bid), float(tk.ask), int(getattr(tk, "time_msc", 0) or int(getattr(tk, "time", 0)) * 1000)

    return ConversorMoeda(rota, cotacao, idade_max_s=idade_max_s)


def motivo_sem_cotacao(de: str, para: str) -> str:
    """Motivo de veto padronizado (auditoria/painel)."""
    return f"sem_cotacao_conversao:{de.upper()}->{para.upper()}"
