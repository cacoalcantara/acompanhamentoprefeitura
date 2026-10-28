"""Sessao de negociacao POR INSTRUMENTO: fuso, grade da bolsa, forex 24x5, cripto 24x7, pausas, rollover e feriados.

A janela de entradas/zeragem do risco (ConfigRisco.entradas_de/entradas_ate/zeragem) e lida NO FUSO DA SESSAO do
instrumento (B3: America/Sao_Paulo; forex: o da convencao, America/New_York), nunca fixa em horario de Brasilia.

- grade (bolsas): aberto se o dia e pregao (calendario da bolsa) e abertura <= hora < fechamento;
- 24x5 (forex): aberto de `abertura_semanal` (ex.: domingo 17:00 NY) a `fechamento_semanal` (sexta 17:00 NY), fora
  das `pausas` diarias (rollover) e dos `dias_fechados` (MM-DD, a confirmar com a corretora);
- 24x7 (cripto): aberto todos os dias, fora das pausas e dos dias fechados.
Em 24x5/24x7 o `rollover` encerra o dia de negociacao: entradas param `entradas_antes_fechamento_min` antes de
qualquer fechamento/pausa/rollover e a zeragem e forcada `zeragem_antes_fechamento_min` antes (sem swap no day trade).
Posicao que atravessa o rollover paga/recebe swap (`rollovers_entre`, triplo no dia do perfil).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from .bolsas import PerfilBolsa
from .tipos import SessaoPregao

DIAS = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6,
        "seg": 0, "ter": 1, "qua": 2, "qui": 3, "sex": 4, "sab": 5, "dom": 6}


def _hhmm(s: str) -> dt.time:
    h, m = s.strip().split(":")[:2]
    return dt.time(int(h), int(m))


def _seg(t: dt.time) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def _dia_hora(texto: str) -> tuple[int, dt.time]:
    """'domingo 17:00' -> (6, 17:00)."""
    partes = texto.strip().lower().split()
    if len(partes) != 2 or partes[0] not in DIAS:
        raise ValueError(f"horario semanal invalido '{texto}': use '<dia> HH:MM' (ex.: 'domingo 17:00')")
    return DIAS[partes[0]], _hhmm(partes[1])


@dataclass(frozen=True)
class EstadoSessao:
    aberto: bool
    entradas: bool
    zerar: bool
    motivo: str  # "" | fora_do_pregao | fim_de_semana | dia_fechado | pausa | fora_da_janela_de_entradas | zeragem | proximo_do_fechamento
    dia: dt.date  # dia de negociacao


class SessaoInstrumento:
    """Relogio de sessao de um instrumento (ver docstring do modulo)."""

    def __init__(self, bolsa: PerfilBolsa, grade: Callable[[dt.date], SessaoPregao] | None = None,
                 eh_pregao: Callable[[dt.date], bool] | None = None) -> None:
        self.bolsa = bolsa
        self.tipo = bolsa.sessao_tipo
        self.tz = ZoneInfo(bolsa.fuso)
        self._grade = grade or bolsa.sessao_em
        self._eh_pregao = eh_pregao
        self._rollover = _hhmm(bolsa.rollover) if bolsa.rollover else None
        self._pausas = tuple((_hhmm(a), _hhmm(b)) for a, b in bolsa.pausas)
        self._fechados = frozenset(bolsa.dias_fechados)
        self._abre = _dia_hora(bolsa.abertura_semanal) if self.tipo == "24x5" else None
        self._fecha = _dia_hora(bolsa.fechamento_semanal) if self.tipo == "24x5" else None
        if self.tipo == "24x5" and (self._abre is None or self._fecha is None):
            raise ValueError(f"sessao 24x5 de {bolsa.codigo} exige abertura_semanal e fechamento_semanal")

    # ------------------------------------------------------------------ tempo local
    def local(self, t_ns: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(t_ns / 1e9, tz=self.tz)

    def _instante(self, d: dt.date, h: dt.time) -> dt.datetime:
        return dt.datetime.combine(d, h, tzinfo=self.tz)

    def dia_negociacao(self, t_ns: int) -> dt.date:
        """Dia de negociacao: data local (bolsa) ou, em 24h com rollover, a data em que o dia TERMINA."""
        loc = self.local(t_ns)
        if (self.tipo != "grade" and self._rollover is not None and self._rollover != dt.time(0, 0)
                and loc.time() >= self._rollover):
            return loc.date() + dt.timedelta(days=1)
        return loc.date()

    def _pregao(self, d: dt.date) -> bool:
        if self._eh_pregao is not None:
            return bool(self._eh_pregao(d))
        if self.bolsa.calendario:
            from .calendario_b3 import eh_pregao
            return eh_pregao(d, self.bolsa.calendario)
        return d.weekday() < 5

    # ------------------------------------------------------------------ aberto/fechado
    def _na_pausa(self, h: dt.time) -> bool:
        for a, b in self._pausas:
            if (a <= h < b) if a <= b else (h >= a or h < b):
                return True
        return False

    def _fim_de_semana(self, loc: dt.datetime) -> bool:
        assert self._abre is not None and self._fecha is not None
        m = loc.weekday() * 1440 + loc.hour * 60 + loc.minute
        fecha = self._fecha[0] * 1440 + self._fecha[1].hour * 60 + self._fecha[1].minute
        abre = self._abre[0] * 1440 + self._abre[1].hour * 60 + self._abre[1].minute
        return (fecha <= m < abre) if fecha <= abre else (m >= fecha or m < abre)

    def aberto(self, t_ns: int) -> tuple[bool, str]:
        loc = self.local(t_ns)
        if loc.strftime("%m-%d") in self._fechados:
            return False, "dia_fechado"
        if self.tipo == "grade":
            if not self._pregao(loc.date()):
                return False, "fora_do_pregao"
            g = self._grade(loc.date())
            s = _seg(loc.time())
            if not _seg(_hhmm(g.abertura)) <= s < _seg(_hhmm(g.fechamento)):
                return False, "fora_do_pregao"
            return True, ""
        if self.tipo == "24x5" and self._fim_de_semana(loc):
            return False, "fim_de_semana"
        if self._na_pausa(loc.time()):
            return False, "pausa"
        return True, ""

    def proximo_fechamento(self, t_ns: int) -> dt.datetime | None:
        """Proximo instante em que o mercado fecha OU o dia de negociacao termina (rollover). None em grade."""
        if self.tipo == "grade":
            return None
        loc = self.local(t_ns)
        cands: list[dt.datetime] = []
        for k in range(0, 9):
            d = loc.date() + dt.timedelta(days=k)
            for a, _b in self._pausas:
                cands.append(self._instante(d, a))
            if self._rollover is not None:
                cands.append(self._instante(d, self._rollover))
            if self._fecha is not None and d.weekday() == self._fecha[0]:
                cands.append(self._instante(d, self._fecha[1]))
            if d.strftime("%m-%d") in self._fechados:
                cands.append(self._instante(d, dt.time(0, 0)))
        futuros = [c for c in cands if c > loc]
        return min(futuros) if futuros else None

    # ------------------------------------------------------------------ janela do risco
    def estado(self, t_ns: int, entradas_de: str, entradas_ate: str, zeragem: str) -> EstadoSessao:
        """Entradas/zeragem combinando a janela do risco (no fuso da sessao) com a grade e os fechamentos."""
        dia = self.dia_negociacao(t_ns)
        ok, motivo = self.aberto(t_ns)
        if not ok:
            return EstadoSessao(False, False, True, motivo, dia)
        loc = self.local(t_ns)
        g = self._grade(loc.date())
        s = _seg(loc.time())
        de = max(_seg(_hhmm(entradas_de)), _seg(_hhmm(g.abertura)))
        ate = min(_seg(_hhmm(entradas_ate)), _seg(_hhmm(g.entradas_ate)))
        zer = min(_seg(_hhmm(zeragem)), _seg(_hhmm(g.zeragem)))
        zerar = s >= zer
        entradas = de <= s < ate and not zerar
        motivo = "" if entradas else ("zeragem" if zerar else "fora_da_janela_de_entradas")
        prox = self.proximo_fechamento(t_ns)
        if prox is not None:
            falta = (prox - loc).total_seconds()
            if falta <= self.bolsa.zeragem_antes_fechamento_min * 60:
                return EstadoSessao(True, False, True, "proximo_do_fechamento", dia)
            if falta <= self.bolsa.entradas_antes_fechamento_min * 60 and entradas:
                return EstadoSessao(True, False, zerar, "proximo_do_fechamento", dia)
        return EstadoSessao(True, entradas, zerar, motivo, dia)

    # ------------------------------------------------------------------ swap
    def rollovers_entre(self, t0_ns: int, t1_ns: int, dia_triplo: str = "") -> list[tuple[dt.datetime, int]]:
        """Rollovers atravessados em (t0, t1], com o multiplicador do swap (3 no `dia_triplo`, 1 nos demais).
        24x5: so de segunda a sexta (o triplo cobre o fim de semana); 24x7: todos os dias; grade: nenhum."""
        if self.tipo == "grade" or self._rollover is None or t1_ns <= t0_ns:
            return []
        triplo = DIAS.get(dia_triplo.strip().lower(), -1) if dia_triplo else -1
        a, b = self.local(t0_ns), self.local(t1_ns)
        out: list[tuple[dt.datetime, int]] = []
        d = a.date() - dt.timedelta(days=1)
        while d <= b.date():
            r = self._instante(d, self._rollover)
            if a < r <= b and (self.tipo == "24x7" or d.weekday() < 5):
                out.append((r, 3 if d.weekday() == triplo else 1))
            d += dt.timedelta(days=1)
        return out


def sessao_do_perfil(perfil: Any) -> SessaoInstrumento:
    """Sessao do instrumento a partir do perfil (bolsa/corretora do perfil; grade = sessoes do perfil)."""
    return SessaoInstrumento(perfil.bolsa_perfil(), grade=perfil.sessao_em)


def sessao_especifica(perfil: Any) -> SessaoInstrumento | None:
    """Sessao do perfil quando ela DIFERE do relogio legado da B3 (fuso de Sao Paulo + grade): 24x5/24x7 ou outro
    fuso. None = o caminho legado (calendario_b3) continua valendo, sem nenhuma mudanca de comportamento."""
    b = perfil.bolsa_perfil()
    if b.sessao_tipo == "grade" and b.fuso == "America/Sao_Paulo":
        return None
    return sessao_do_perfil(perfil)
