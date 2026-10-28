"""Maquina do Tempo: reconstroi o resto do pregao milhares de vezes e mede as chances do PLANO (stop e alvo).

Atribuicao: a IDEIA e o METODO do "Time Machine" (reamostrar barras de 5 min do proprio ativo, na mesma hora do
dia, de sessoes completas anteriores; checar o stop antes do alvo; reaplicar a regra nas sessoes passadas reais;
comparar o resultado ao ponto de equilibrio 1/(1+R)) foram inspirados no projeto open source ``day-trader-skill``
de Jake Schincariol (licenca MIT, https://github.com/jakeschincariol/day-trader-skill). Esta e uma
REIMPLEMENTACAO INDEPENDENTE (nenhum codigo foi copiado) sobre os tipos do robowin: perfil de instrumento,
grade de sessao da bolsa, ticks do armazem, custos de execucao e moeda do instrumento. Ver THIRD_PARTY_NOTICES.md.

*** SIMULACAO DE BOOTSTRAP DAS ULTIMAS SESSOES, NAO E PREVISAO. Concordancia entre simulacoes nao prova lucro. ***

Como funciona
-------------
1. ``barras_de_ticks``: ticks -> barras OHLC de ``intervalo_s`` na GRADE de sessao do perfil/bolsa. Sessao COMPLETA
   = todas as barras da grade tem ticks; meio-pregao e gaps sao descartados (com o motivo).
2. ``simular``: so usa sessoes completas ANTERIORES a data do plano (causal). Cada barra vira 4 razoes sobre o
   fechamento da barra anterior (abertura, maxima, minima, fechamento). Cada replay parte do preco de entrada e,
   para cada barra restante ate a zeragem, sorteia uma barra da MESMA hora do dia (+- ``janela`` barras) de uma
   sessao passada e a reescala ao preco corrente (arredondado ao tick do perfil). Em cada barra o STOP e checado
   ANTES do alvo: barra que toca os dois conta como stop. Tudo vetorizado em numpy.
3. ``historico``: a regra de entrada (um ``callable``) e reaplicada nas sessoes passadas reais.
4. Veredito: a fracao de alvos entre os desfechos resolvidos e comparada ao equilibrio. Sem custos o equilibrio e
   1/(1+R); com custos de ida e volta ``c`` (em R) e perda media ``L`` (em R, >= 1 com slippage de stop):
   ``(L + c) / (R + L)``. Custos e P&L ficam na moeda do instrumento (perfil).

Recusas (ErroMaquinaDoTempo): menos de 20 sessoes completas anteriores, entrada/zeragem fora da grade, plano
incoerente (stop do lado errado, distancias nao positivas), dados sem rotulo de origem (sintetico ou nao).
Nenhum indicador tecnico tradicional e usado em nenhuma parte.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from robowin.core.calendario_b3 import SP, proximo_pregao
from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import Lado, Tick
from robowin.execucao.custos import Custos

MIN_SESSOES = 20
SEED_PADRAO = 19
REPLAYS_PADRAO = 10_000
JANELA_PADRAO = 2
INTERVALO_PADRAO_S = 300
PERCENTIS = (5, 25, 50, 75, 95)
LIMIAR_PERTO = 0.05  # diferenca (em fracao) abaixo da qual o resultado e "perto do equilibrio"
LIMIAR_FORTE = 0.15  # diferenca a partir da qual a inclinacao e "forte"
AVISO = "simulacao de bootstrap das ultimas sessoes, NAO e previsao"
ROTULO_SIMULACAO = "SIMULACAO"
ROTULO_SINTETICO = "SINTETICO"

V_PERTO = "perto_do_equilibrio"
V_INCLINA_ALVO = "inclina_para_o_alvo"
V_INCLINA_STOP = "inclina_para_o_stop"
V_FORTE_ALVO = "forte_para_o_alvo"
V_FORTE_STOP = "forte_para_o_stop"
V_INDETERMINADO = "indeterminado"

# saidas de um replay
ALVO, STOP, NENHUM = 1, -1, 0


class ErroMaquinaDoTempo(ValueError):
    """Recusa clara: a simulacao nao e feita (nunca devolve numero enganoso)."""


class ErroSessoesInsuficientes(ErroMaquinaDoTempo):
    pass


class ErroPlanoIncoerente(ErroMaquinaDoTempo):
    pass


class ErroEntradaForaDaGrade(ErroMaquinaDoTempo):
    pass


class ErroSemRotulo(ErroMaquinaDoTempo):
    pass


# ==================================================================================================
# grade de sessao e barras
# ==================================================================================================
def hhmm_para_s(txt: str) -> int:
    try:
        h, m = txt.strip().split(":")
        hh, mm = int(h), int(m)
    except ValueError as e:
        raise ErroEntradaForaDaGrade(f"horario invalido (use HH:MM): {txt!r}") from e
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ErroEntradaForaDaGrade(f"horario invalido (use HH:MM): {txt!r}")
    return hh * 3600 + mm * 60


def s_para_hhmm(seg: int) -> str:
    return f"{seg // 3600:02d}:{(seg % 3600) // 60:02d}"


@dataclass(frozen=True)
class GradeSessao:
    """Grade de barras de uma sessao (segundos desde a meia-noite local da bolsa)."""
    abertura_s: int
    fechamento_s: int
    entradas_ate_s: int
    zeragem_s: int
    intervalo_s: int

    @property
    def n_barras(self) -> int:
        return (self.fechamento_s - self.abertura_s) // self.intervalo_s

    def chave(self) -> tuple[int, int, int]:
        return (self.abertura_s, self.n_barras, self.intervalo_s)

    def hora_da_barra(self, i: int) -> str:
        return s_para_hhmm(self.abertura_s + i * self.intervalo_s)


def grade_da_sessao(perfil: PerfilInstrumento, d: dt.date, intervalo_s: int = INTERVALO_PADRAO_S) -> GradeSessao:
    if intervalo_s <= 0:
        raise ErroMaquinaDoTempo("intervalo_s deve ser positivo")
    s = perfil.sessao_em(d)  # ValueError se nao ha grade vigente em `d`
    return GradeSessao(hhmm_para_s(s.abertura), hhmm_para_s(s.fechamento), hhmm_para_s(s.entradas_ate), hhmm_para_s(s.zeragem),
                       intervalo_s)


@dataclass(frozen=True)
class SessaoBarras:
    """Uma sessao COMPLETA em barras (um valor por barra da grade, na ordem do tempo)."""
    data: dt.date
    o: np.ndarray = field(repr=False)
    h: np.ndarray = field(repr=False)
    l: np.ndarray = field(repr=False)  # noqa: E741
    c: np.ndarray = field(repr=False)
    grade: GradeSessao

    @property
    def n(self) -> int:
        return int(self.c.shape[0])

    def ate(self, j: int) -> SessaoBarras:
        """Visao CAUSAL: so as barras [0, j) (as que ja fecharam antes da barra j abrir)."""
        return SessaoBarras(self.data, self.o[:j], self.h[:j], self.l[:j], self.c[:j], self.grade)


@dataclass(frozen=True)
class PiscinaSessoes:
    """Sessoes completas + as descartadas (com motivo) + o ROTULO de origem dos dados."""
    sessoes: tuple[SessaoBarras, ...]
    descartadas: Mapping[dt.date, str]
    intervalo_s: int
    sintetico: bool | None = None  # None = SEM rotulo => a simulacao recusa
    origem: str = ""

    def anteriores_a(self, d: dt.date) -> tuple[SessaoBarras, ...]:
        """Sessoes estritamente anteriores a `d` (causalidade: o dia do plano e o futuro ficam de fora)."""
        return tuple(s for s in self.sessoes if s.data < d)

    def datas(self) -> list[dt.date]:
        return [s.data for s in self.sessoes]


def _extrair(ticks: Sequence[Tick]) -> tuple[np.ndarray, np.ndarray]:
    n = len(ticks)
    t = np.fromiter((x.t_utc_ns for x in ticks), dtype=np.int64, count=n)
    bid = np.fromiter((x.bid for x in ticks), dtype=float, count=n)
    ask = np.fromiter((x.ask for x in ticks), dtype=float, count=n)
    last = np.fromiter((x.last for x in ticks), dtype=float, count=n)
    preco = np.where((bid > 0) & (ask > 0), (bid + ask) / 2.0, last)
    ok = np.isfinite(preco) & (preco > 0)
    ordem = np.argsort(t[ok], kind="stable")
    return t[ok][ordem], preco[ok][ordem]


def barras_de_ticks(ticks: Sequence[Tick], perfil: PerfilInstrumento, intervalo_s: int = INTERVALO_PADRAO_S, *,
                    sintetico: bool | None = None, origem: str = "") -> PiscinaSessoes:
    """Ticks -> barras na grade de sessao do perfil/bolsa. Preco da barra: meio do book (ou ultimo negocio).

    Sessao completa = TODAS as barras da grade com pelo menos um tick. Meio-pregao (abre tarde / fecha cedo) e gaps
    (barra sem tick) sao descartados e listados em ``descartadas`` com o motivo. ``sintetico`` e o ROTULO de origem
    (True/False); None deixa a piscina sem rotulo e a simulacao a recusa."""
    if intervalo_s <= 0:
        raise ErroMaquinaDoTempo("intervalo_s deve ser positivo")
    sessoes: list[SessaoBarras] = []
    descartadas: dict[dt.date, str] = {}
    if len(ticks) == 0:
        return PiscinaSessoes((), descartadas, intervalo_s, sintetico, origem)
    t, p = _extrair(ticks)
    if t.size == 0:
        return PiscinaSessoes((), descartadas, intervalo_s, sintetico, origem)
    meia = pd.to_datetime(t, unit="ns", utc=True).tz_convert(SP).normalize().asi8
    cortes = np.r_[0, np.flatnonzero(np.diff(meia)) + 1, t.size]
    for a, b in zip(cortes[:-1], cortes[1:], strict=True):
        m = int(meia[a])
        d = dt.datetime.fromtimestamp(m / 1e9, SP).date()
        try:
            g = grade_da_sessao(perfil, d, intervalo_s)
        except ValueError:
            descartadas[d] = "sem_grade_de_sessao"
            continue
        nb = g.n_barras
        rel = (t[a:b] - m) / 1e9 - g.abertura_s
        dentro = (rel >= 0) & (rel < nb * intervalo_s)
        if not dentro.any():
            descartadas[d] = "sem_ticks_na_sessao"
            continue
        idx = np.floor(rel[dentro] / intervalo_s).astype(np.int64)
        pr = p[a:b][dentro]
        ini = np.r_[0, np.flatnonzero(np.diff(idx)) + 1]
        pres = idx[ini]
        if pres.size < nb:
            meio_pregao = pres[0] > 0 or pres[-1] < nb - 1
            descartadas[d] = f"meio_pregao:{pres.size}/{nb}_barras" if meio_pregao else f"gap:{pres.size}/{nb}_barras"
            continue
        fim = np.r_[ini[1:], pr.size] - 1
        sessoes.append(SessaoBarras(d, pr[ini].copy(), np.maximum.reduceat(pr, ini), np.minimum.reduceat(pr, ini),
                                    pr[fim].copy(), g))
    return PiscinaSessoes(tuple(sessoes), descartadas, intervalo_s, sintetico, origem)


# ==================================================================================================
# plano
# ==================================================================================================
@dataclass(frozen=True)
class PlanoMT:
    """Plano a testar. Stop e alvo sao DISTANCIAS positivas em PONTOS a partir da entrada (convertidas em ticks pelo
    perfil). ``data`` e o dia do plano: so sessoes ANTERIORES a ele entram na piscina."""
    lado: Lado
    stop_pontos: float
    alvo_pontos: float
    data: dt.date
    entrada: str  # HH:MM, em uma fronteira de barra da grade
    zeragem: str  # HH:MM, idem
    preco_entrada: float | None = None  # nivel do ativo p/ reescalar as razoes; None = o da sessao mais recente

    @classmethod
    def de_niveis(cls, lado: Lado, preco_entrada: float, preco_stop: float, preco_alvo: float, *, data: dt.date,
                  entrada: str, zeragem: str) -> PlanoMT:
        """Plano a partir de NIVEIS de preco: recusa stop/alvo do lado errado."""
        sg = 1.0 if lado == Lado.COMPRA else -1.0
        if sg * (preco_entrada - preco_stop) <= 0:
            raise ErroPlanoIncoerente("stop do lado errado: deve ficar contra a posicao "
                                      f"({'abaixo' if sg > 0 else 'acima'} da entrada)")
        if sg * (preco_alvo - preco_entrada) <= 0:
            raise ErroPlanoIncoerente("alvo do lado errado: deve ficar a favor da posicao "
                                      f"({'acima' if sg > 0 else 'abaixo'} da entrada)")
        return cls(lado, abs(preco_entrada - preco_stop), abs(preco_alvo - preco_entrada), data, entrada, zeragem,
                   preco_entrada)


@dataclass(frozen=True)
class PlanoResolvido:
    plano: PlanoMT
    grade: GradeSessao
    j: int  # barra de entrada (a barra que ABRE na hora da entrada)
    z: int  # indice da fronteira da zeragem: a posicao vive nas barras [j, z)
    stop_ticks: int
    alvo_ticks: int
    tick: float
    sinal: float

    @property
    def stop_pontos(self) -> float:
        return self.stop_ticks * self.tick

    @property
    def alvo_pontos(self) -> float:
        return self.alvo_ticks * self.tick

    @property
    def r_alvo(self) -> float:
        return self.alvo_ticks / self.stop_ticks


def resolver_plano(plano: PlanoMT, perfil: PerfilInstrumento, intervalo_s: int = INTERVALO_PADRAO_S) -> PlanoResolvido:
    if plano.lado not in (Lado.COMPRA, Lado.VENDA):
        raise ErroPlanoIncoerente(f"lado invalido: {plano.lado!r}")
    if not (np.isfinite(plano.stop_pontos) and np.isfinite(plano.alvo_pontos)) or plano.stop_pontos <= 0 \
            or plano.alvo_pontos <= 0:
        raise ErroPlanoIncoerente("stop e alvo devem ser distancias positivas em pontos")
    st, at = int(round(perfil.ticks(plano.stop_pontos))), int(round(perfil.ticks(plano.alvo_pontos)))
    if st < 1 or at < 1:
        raise ErroPlanoIncoerente(f"stop/alvo menores que 1 tick do perfil ({perfil.tick_pontos} pontos)")
    try:
        g = grade_da_sessao(perfil, plano.data, intervalo_s)
    except ValueError as e:
        raise ErroPlanoIncoerente(str(e)) from e
    e_s, z_s = hhmm_para_s(plano.entrada), hhmm_para_s(plano.zeragem)
    for nome, v in (("entrada", e_s), ("zeragem", z_s)):
        if v < g.abertura_s or v > g.fechamento_s or (v - g.abertura_s) % intervalo_s != 0:
            raise ErroEntradaForaDaGrade(
                f"{nome} {s_para_hhmm(v)} fora da grade de {intervalo_s}s da sessao "
                f"({s_para_hhmm(g.abertura_s)}-{s_para_hhmm(g.fechamento_s)}): use o inicio de uma barra")
    if e_s >= g.fechamento_s:
        raise ErroEntradaForaDaGrade(f"entrada {plano.entrada} nao abre nenhuma barra da sessao")
    if e_s > g.entradas_ate_s:
        raise ErroPlanoIncoerente(f"entrada {plano.entrada} depois do limite de entradas ({s_para_hhmm(g.entradas_ate_s)})")
    if z_s > g.zeragem_s:
        raise ErroPlanoIncoerente(f"zeragem {plano.zeragem} depois da zeragem da sessao ({s_para_hhmm(g.zeragem_s)})")
    if z_s <= e_s:
        raise ErroPlanoIncoerente("a zeragem deve ser posterior a entrada")
    return PlanoResolvido(plano, g, (e_s - g.abertura_s) // intervalo_s, (z_s - g.abertura_s) // intervalo_s, st, at,
                          perfil.tick_pontos, 1.0 if plano.lado == Lado.COMPRA else -1.0)


def plano_padrao(perfil: PerfilInstrumento, lado: Lado, stop_pontos: float, alvo_pontos: float, data: dt.date,
                 entrada: str | None = None, zeragem: str | None = None, preco_entrada: float | None = None,
                 intervalo_s: int = INTERVALO_PADRAO_S) -> PlanoMT:
    """Entrada padrao = 1 h apos a abertura; zeragem padrao = a da sessao do perfil."""
    g = grade_da_sessao(perfil, data, intervalo_s)
    return PlanoMT(lado, stop_pontos, alvo_pontos, data, entrada or s_para_hhmm(g.abertura_s + 3600),
                   zeragem or s_para_hhmm(g.zeragem_s), preco_entrada)


# ==================================================================================================
# nucleo vetorizado: resolve caminhos (replays ou sessoes reais) em ALVO / STOP / NENHUM
# ==================================================================================================
def _resolver(o: np.ndarray, h: np.ndarray, b: np.ndarray, c: np.ndarray, entrada: np.ndarray, rp: PlanoResolvido,
              slippage_stop_ticks: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Caminhos (N, K) de barras (abertura o, maxima h, minima b, fechamento c) ja arredondadas ao tick. ``entrada``: (N, 1). Devolve (saida, R bruto, pontos a
    favor na zeragem). STOP e checado ANTES do alvo: barra que toca os dois conta como stop. O stop executa no pior
    entre o nivel e a abertura da barra (gap) e ainda paga ``slippage_stop_ticks`` contra; o alvo executa no nivel."""
    sg, tick = rp.sinal, rp.tick
    nivel_stop = entrada - sg * rp.stop_pontos
    nivel_alvo = entrada + sg * rp.alvo_pontos
    if sg > 0:
        toca_stop, toca_alvo = b <= nivel_stop, h >= nivel_alvo
    else:
        toca_stop, toca_alvo = h >= nivel_stop, b <= nivel_alvo
    k = o.shape[1]
    tem_s, tem_a = toca_stop.any(axis=1), toca_alvo.any(axis=1)
    i_s = np.where(tem_s, toca_stop.argmax(axis=1), k)
    i_a = np.where(tem_a, toca_alvo.argmax(axis=1), k)
    no_stop = tem_s & (i_s <= i_a)  # empate na mesma barra => stop
    no_alvo = tem_a & (i_a < i_s)
    linhas = np.arange(o.shape[0])
    abre = o[linhas, np.minimum(i_s, k - 1)][:, None]
    folga = slippage_stop_ticks * tick
    exec_stop = (np.minimum(nivel_stop, abre) - folga) if sg > 0 else (np.maximum(nivel_stop, abre) + folga)
    risco = rp.stop_pontos
    r_stop = sg * (exec_stop[:, 0] - entrada[:, 0]) / risco
    final = c[:, -1]
    favor = sg * (final - entrada[:, 0])
    r_nenhum = favor / risco
    saida = np.where(no_stop, STOP, np.where(no_alvo, ALVO, NENHUM)).astype(np.int8)
    r_bruto = np.where(no_stop, r_stop, np.where(no_alvo, rp.r_alvo, r_nenhum))
    return saida, r_bruto, favor


def _arred(x: np.ndarray, tick: float) -> np.ndarray:
    return np.round(x / tick) * tick


@dataclass(frozen=True)
class _Custos:
    ida_volta: float  # moeda do instrumento, 1 lote
    ida_volta_zeragem: float
    risco: float  # moeda do instrumento: distancia do stop x valor do ponto

    @property
    def r(self) -> float:
        return self.ida_volta / self.risco

    @property
    def r_zeragem(self) -> float:
        return self.ida_volta_zeragem / self.risco


def _custos(perfil: PerfilInstrumento, rp: PlanoResolvido, custos: Custos | None) -> _Custos:
    cu = custos or Custos.do_perfil(perfil)
    d = rp.plano.data
    return _Custos(cu.ida_e_volta(d, 1), cu.ida_e_volta(d, 1, zeragem=True), rp.stop_pontos * perfil.ponto_valor_brl)


def _equilibrio(r_alvo: float, perda_r: float, custo_r: float) -> tuple[float, float]:
    """(bruto, liquido) do equilibrio da fracao de alvos entre desfechos resolvidos.
    Bruto = 1/(1+R). Liquido: p*(R+L) = L + c  =>  p = (L + c)/(R + L)."""
    return 1.0 / (1.0 + r_alvo), (perda_r + custo_r) / (r_alvo + perda_r)


# ==================================================================================================
# resultados
# ==================================================================================================
@dataclass(frozen=True)
class Veredito:
    codigo: str
    texto: str


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%".replace(".", ",")


def _num(x: float, nd: int = 2) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def montar_veredito(p_res: float | None, eq_liq: float, r_alvo: float, custo_r: float, ev_liquido: float, moeda: str,
                    rotulo_amostra: str) -> Veredito:
    """Veredito em portugues pela distancia entre a fracao de alvos (entre os desfechos resolvidos) e o equilibrio
    LIQUIDO de custos. < 5 pontos: perto do equilibrio; 5 a 15: inclina; >= 15: forte."""
    if p_res is None:
        return Veredito(V_INDETERMINADO, "Indeterminado: nenhum replay resolveu em alvo nem em stop antes da zeragem. "
                        f"{AVISO}.")
    dif = p_res - eq_liq
    base = (f"O alvo veio primeiro em {_pct(p_res)} dos desfechos resolvidos ({rotulo_amostra}); o equilibrio, "
            f"descontados os custos, e {_pct(eq_liq)} (R do alvo = {_num(r_alvo)}; custo = {_num(custo_r, 3)}R; "
            f"valor esperado liquido = {_num(ev_liquido)} {moeda} por lote).")
    if abs(dif) < LIMIAR_PERTO:
        cod, ini = V_PERTO, "Perto do equilibrio: sem vantagem clara em nenhum dos lados."
    elif dif > 0:
        cod = V_FORTE_ALVO if dif >= LIMIAR_FORTE else V_INCLINA_ALVO
        ini = ("Forte inclinacao para o alvo nesta amostra." if dif >= LIMIAR_FORTE
               else "Inclina para o alvo nesta amostra.")
    else:
        cod = V_FORTE_STOP if -dif >= LIMIAR_FORTE else V_INCLINA_STOP
        ini = ("Forte inclinacao para o stop nesta amostra." if -dif >= LIMIAR_FORTE
               else "Inclina para o stop nesta amostra.")
    return Veredito(cod, f"{ini} {base} {AVISO.capitalize()}; concordancia nao prova lucro.")


@dataclass(frozen=True)
class ResultadoSimulacao:
    n_sessoes: int
    replays: int
    seed: int
    janela: int
    lado: str
    stop_pontos: float
    alvo_pontos: float
    r_alvo: float
    p_alvo: float
    p_stop: float
    p_nenhum: float
    p_verde: float
    p_alvo_entre_resolvidos: float | None
    r_bruto_medio: float
    r_liquido_medio: float
    custo_r: float
    custo_ida_volta: float
    ev_liquido: float  # moeda do instrumento, por lote
    equilibrio_bruto: float
    equilibrio_liquido: float
    percentis_pontos: Mapping[str, float]  # pontos A FAVOR do plano na zeragem (relativos; sem niveis absolutos)
    percentis_preco: Mapping[str, float]
    preco_entrada: float
    moeda: str
    slippage_stop_ticks: float
    veredito: Veredito
    sintetico: bool
    rotulos: tuple[str, ...]
    aviso: str = AVISO

    def a_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["veredito"] = {"codigo": self.veredito.codigo, "texto": self.veredito.texto}
        d["percentis_pontos"] = dict(self.percentis_pontos)
        d["percentis_preco"] = dict(self.percentis_preco)
        d["rotulos"] = list(self.rotulos)
        return d


@dataclass(frozen=True)
class ResultadoHistorico:
    n_sessoes: int
    n_disparos: int
    n_alvo: int
    n_stop: int
    n_nenhum: int
    p_alvo_entre_resolvidos: float | None
    r_bruto_medio: float | None
    r_liquido_medio: float | None
    datas_disparo: tuple[dt.date, ...] = ()

    def a_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["datas_disparo"] = [x.isoformat() for x in self.datas_disparo]
        return d


@dataclass(frozen=True)
class Relatorio:
    simulacao: ResultadoSimulacao
    historico: ResultadoHistorico

    def a_dict(self) -> dict[str, Any]:
        return {"simulacao": self.simulacao.a_dict(), "historico": self.historico.a_dict()}


# ==================================================================================================
# simulacao
# ==================================================================================================
def _piscina_valida(piscina: PiscinaSessoes, rp: PlanoResolvido) -> tuple[SessaoBarras, ...]:
    if piscina.sintetico is None:
        raise ErroSemRotulo("dados sem rotulo de origem (sintetico ou nao): recusado. Rotule a piscina com "
                            "sintetico=True/False; dados sinteticos/contaminados sem rotulo nao viram probabilidade.")
    if piscina.intervalo_s != rp.grade.intervalo_s:
        raise ErroMaquinaDoTempo("intervalo das barras da piscina difere do pedido")
    antes = piscina.anteriores_a(rp.plano.data)
    boas = tuple(s for s in antes if s.grade.chave() == rp.grade.chave())
    if len(boas) < MIN_SESSOES:
        raise ErroSessoesInsuficientes(
            f"dados insuficientes: {len(boas)} sessoes completas anteriores ao plano (minimo {MIN_SESSOES}); "
            f"descartadas por meio-pregao/gap/grade: {len(piscina.descartadas) + len(antes) - len(boas)}")
    return boas


def _razoes(sessoes: Sequence[SessaoBarras]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """(S, n) razoes de abertura, maxima, minima e fechamento sobre o fechamento da barra anterior (a 1a barra usa a
    propria abertura: nenhuma razao atravessa a fronteira entre sessoes)."""
    o = np.stack([s.o for s in sessoes])
    h = np.stack([s.h for s in sessoes])
    l = np.stack([s.l for s in sessoes])  # noqa: E741
    c = np.stack([s.c for s in sessoes])
    prev = np.concatenate([o[:, :1], c[:, :-1]], axis=1)
    return o / prev, h / prev, l / prev, c / prev


def _preco_ref(boas: Sequence[SessaoBarras], rp: PlanoResolvido) -> float:
    if rp.plano.preco_entrada is not None and rp.plano.preco_entrada > 0:
        return float(rp.plano.preco_entrada)
    return float(boas[-1].o[rp.j])  # abertura da barra de entrada na sessao anterior mais recente


def simular(piscina: PiscinaSessoes, perfil: PerfilInstrumento, plano: PlanoMT, *, seed: int = SEED_PADRAO,
            replays: int = REPLAYS_PADRAO, janela: int = JANELA_PADRAO, slippage_stop_ticks: float = 0.0,
            custos: Custos | None = None) -> ResultadoSimulacao:
    """Monte Carlo por bootstrap de barras (vetorizado). Determinístico por ``seed``. Causal: so sessoes < plano.data."""
    if replays < 1 or janela < 0 or slippage_stop_ticks < 0:
        raise ErroMaquinaDoTempo("replays >= 1, janela >= 0 e slippage_stop_ticks >= 0")
    rp = resolver_plano(plano, perfil, piscina.intervalo_s)
    boas = _piscina_valida(piscina, rp)
    ro, rh, rl, rc = _razoes(boas)
    s_n, n = ro.shape
    k = rp.z - rp.j
    rng = np.random.default_rng(seed)
    sorteio = rng.integers(0, s_n, size=(replays, k))
    desloc = rng.integers(-janela, janela + 1, size=(replays, k))
    barra = np.clip(np.arange(rp.j, rp.z)[None, :] + desloc, 0, n - 1)
    to, th, tl, tc = ro[sorteio, barra], rh[sorteio, barra], rl[sorteio, barra], rc[sorteio, barra]
    p0 = float(_arred(np.array([_preco_ref(boas, rp)]), rp.tick)[0])
    nivel = p0 * np.cumprod(np.concatenate([np.ones((replays, 1)), tc[:, :-1]], axis=1), axis=1)
    o, h, l, c = (_arred(nivel * x, rp.tick) for x in (to, th, tl, tc))  # noqa: E741
    entrada = np.full((replays, 1), p0)
    saida, r_bruto, favor = _resolver(o, h, l, c, entrada, rp, slippage_stop_ticks)
    cu = _custos(perfil, rp, custos)
    custo_r = np.where(saida == NENHUM, cu.r_zeragem, cu.r)
    r_liq = r_bruto - custo_r
    n_a, n_s = int((saida == ALVO).sum()), int((saida == STOP).sum())
    p_res = n_a / (n_a + n_s) if (n_a + n_s) else None
    perda_r = float(-r_bruto[saida == STOP].mean()) if n_s else 1.0
    eq_b, eq_l = _equilibrio(rp.r_alvo, perda_r, cu.r)
    ev_liq_r = float(r_liq.mean())
    ev_brl = ev_liq_r * cu.risco
    verde = (saida == ALVO) | ((saida == NENHUM) & (favor > 0))
    pc = np.percentile(favor, PERCENTIS)
    rotulos = [ROTULO_SIMULACAO] + ([ROTULO_SINTETICO] if piscina.sintetico else []) \
        + (["ativo_a_confirmar"] if perfil.a_confirmar else [])
    ver = montar_veredito(p_res, eq_l, rp.r_alvo, cu.r, ev_brl, perfil.moeda, f"{s_n} sessoes, {replays} replays")
    return ResultadoSimulacao(
        n_sessoes=s_n, replays=replays, seed=seed, janela=janela, lado=plano.lado.value,
        stop_pontos=rp.stop_pontos, alvo_pontos=rp.alvo_pontos, r_alvo=rp.r_alvo,
        p_alvo=n_a / replays, p_stop=n_s / replays, p_nenhum=float((saida == NENHUM).mean()),
        p_verde=float(verde.mean()), p_alvo_entre_resolvidos=p_res, r_bruto_medio=float(r_bruto.mean()),
        r_liquido_medio=ev_liq_r, custo_r=cu.r, custo_ida_volta=cu.ida_volta, ev_liquido=ev_brl,
        equilibrio_bruto=eq_b, equilibrio_liquido=eq_l,
        percentis_pontos={str(q): float(v) for q, v in zip(PERCENTIS, pc, strict=True)},
        percentis_preco={str(q): float(p0 + rp.sinal * v) for q, v in zip(PERCENTIS, pc, strict=True)},
        preco_entrada=p0, moeda=perfil.moeda, slippage_stop_ticks=float(slippage_stop_ticks), veredito=ver,
        sintetico=bool(piscina.sintetico), rotulos=tuple(rotulos))


# ==================================================================================================
# historico: a regra de entrada reaplicada nas sessoes passadas REAIS
# ==================================================================================================
Regra = Callable[[SessaoBarras], bool]


def historico(piscina: PiscinaSessoes, perfil: PerfilInstrumento, plano: PlanoMT, regra: Regra | None = None, *,
              slippage_stop_ticks: float = 0.0, custos: Custos | None = None) -> ResultadoHistorico:
    """Reaplica ``regra`` em cada sessao completa ANTERIOR ao plano. ``regra(visao)`` recebe so as barras que ja
    fecharam antes da barra de entrada (``SessaoBarras.ate(j)``: nao ha como espiar o futuro) e devolve True para
    entrar. Sem regra, entra em todas as sessoes (base de comparacao). A entrada e a abertura da barra de entrada
    daquela sessao; stop/alvo/zeragem sao os do plano."""
    rp = resolver_plano(plano, perfil, piscina.intervalo_s)
    boas = _piscina_valida(piscina, rp)
    cu = _custos(perfil, rp, custos)
    disparos = [s for s in boas if regra is None or bool(regra(s.ate(rp.j)))]
    if not disparos:
        return ResultadoHistorico(len(boas), 0, 0, 0, 0, None, None, None)
    sl = slice(rp.j, rp.z)
    o, h, l, c = (_arred(np.stack([getattr(s, a)[sl] for s in disparos]), rp.tick) for a in ("o", "h", "l", "c"))  # noqa: E741
    entrada = o[:, :1].copy()
    saida, r_bruto, _ = _resolver(o, h, l, c, entrada, rp, slippage_stop_ticks)
    r_liq = r_bruto - np.where(saida == NENHUM, cu.r_zeragem, cu.r)
    n_a, n_s = int((saida == ALVO).sum()), int((saida == STOP).sum())
    return ResultadoHistorico(
        len(boas), len(disparos), n_a, n_s, int((saida == NENHUM).sum()),
        n_a / (n_a + n_s) if (n_a + n_s) else None, float(r_bruto.mean()), float(r_liq.mean()),
        tuple(s.data for s in disparos))


def planejar(piscina: PiscinaSessoes, perfil: PerfilInstrumento, plano: PlanoMT, regra: Regra | None = None, *,
             seed: int = SEED_PADRAO, replays: int = REPLAYS_PADRAO, janela: int = JANELA_PADRAO,
             slippage_stop_ticks: float = 0.0, custos: Custos | None = None) -> Relatorio:
    """Simulacao (10 mil replays) + historico da regra + veredito, tudo com os custos do perfil."""
    sim = simular(piscina, perfil, plano, seed=seed, replays=replays, janela=janela,
                  slippage_stop_ticks=slippage_stop_ticks, custos=custos)
    his = historico(piscina, perfil, plano, regra, slippage_stop_ticks=slippage_stop_ticks, custos=custos)
    return Relatorio(sim, his)


def data_padrao_do_plano(piscina: PiscinaSessoes) -> dt.date:
    """Dia do plano quando nao informado: o proximo pregao depois da ultima sessao da piscina."""
    if not piscina.sessoes:
        raise ErroSessoesInsuficientes("dados insuficientes: nenhuma sessao completa")
    return proximo_pregao(max(piscina.datas()))
