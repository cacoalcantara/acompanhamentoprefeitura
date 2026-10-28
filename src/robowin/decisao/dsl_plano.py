"""DSL PERMITIDA do plano condicional (plano 5.2): avaliacao deterministica das condicoes.

Condicoes (core.tipos.Condicao.tipo): ``preco_toca``, ``preco_rompe_acima``, ``preco_rompe_abaixo``,
``spread_max_pontos`` e ``janela_tempo``. Nao existe construcao de indicador, formula ou calculo: o unico
valor numerico e um deslocamento em pontos RELATIVO a referencia (preco no instante t0 da proposta), um
spread maximo em pontos ou uma duracao em segundos.

Semantica (todas puras: mesma entrada, mesma saida):

* ``preco_toca`` (valor = deslocamento ``d``): nivel = ref + d. Verdadeira quando o preco chegou ao nivel
  ou o ultrapassou no sentido do deslocamento (d < 0: preco <= nivel; d > 0: preco >= nivel; d == 0:
  |preco - ref| < meio tick);
* ``preco_rompe_acima``: preco > ref + d; ``preco_rompe_abaixo``: preco < ref + d;
* ``spread_max_pontos``: spread <= valor;
* ``janela_tempo``: como GATILHO e verdadeira enquanto ``t <= limite`` (permite a entrada so dentro da
  janela); como INVALIDACAO e verdadeira quando ``t > limite`` (a janela acabou). O limite e o menor entre
  ``t_armado + valor`` segundos e ``ate_ns`` (instante absoluto, ja convertido por ``plano_para_absoluto``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from robowin.core.tipos import Condicao, PlanoCondicional
from robowin.ia.validadores import TIPOS_CONDICAO, validar_plano

NS = 1_000_000_000
CONDICOES_PRECO = ("preco_toca", "preco_rompe_acima", "preco_rompe_abaixo")


@dataclass(frozen=True)
class EstadoPlano:
    """Instantaneo de mercado usado para avaliar condicoes."""
    t_ns: int
    preco: float
    spread_pts: float = 0.0


def nivel(c: Condicao, ref: float) -> float | None:
    """Preco absoluto da condicao de preco (ref + deslocamento) ou None se nao for de preco."""
    if c.tipo in CONDICOES_PRECO:
        return ref + float(c.valor or 0.0)
    return None


def limite_janela_ns(c: Condicao, t_armado_ns: int) -> int | None:
    lims: list[int] = []
    if c.valor is not None and c.tipo == "janela_tempo":
        lims.append(t_armado_ns + int(c.valor * NS))
    if c.ate_ns is not None:
        lims.append(c.ate_ns)
    return min(lims) if lims else None


def condicao_satisfeita(c: Condicao, *, ref: float, est: EstadoPlano, t_armado_ns: int,
                        tick_pontos: float, papel: Literal["gatilho", "invalidacao"] = "gatilho") -> bool:
    if c.tipo not in TIPOS_CONDICAO:
        return False  # fora da DSL nunca dispara (nem invalida: o plano nem seria aceito)
    if c.tipo == "preco_toca":
        d = float(c.valor or 0.0)
        alvo = ref + d
        if d < 0:
            return est.preco <= alvo
        if d > 0:
            return est.preco >= alvo
        return abs(est.preco - ref) < tick_pontos / 2
    if c.tipo == "preco_rompe_acima":
        return est.preco > ref + float(c.valor or 0.0)
    if c.tipo == "preco_rompe_abaixo":
        return est.preco < ref + float(c.valor or 0.0)
    if c.tipo == "spread_max_pontos":
        return est.spread_pts <= float(c.valor if c.valor is not None else 0.0)
    # janela_tempo
    lim = limite_janela_ns(c, t_armado_ns)
    if lim is None:
        return papel == "gatilho"
    return est.t_ns <= lim if papel == "gatilho" else est.t_ns > lim


def gatilhos_satisfeitos(plano: PlanoCondicional, *, ref: float, est: EstadoPlano, t_armado_ns: int,
                         tick_pontos: float) -> bool:
    """TODOS os gatilhos precisam ocorrer (o validador exige ao menos um)."""
    return bool(plano.gatilhos) and all(
        condicao_satisfeita(c, ref=ref, est=est, t_armado_ns=t_armado_ns, tick_pontos=tick_pontos)
        for c in plano.gatilhos)


def invalidacao_ativa(plano: PlanoCondicional, *, ref: float, est: EstadoPlano, t_armado_ns: int,
                      tick_pontos: float) -> Condicao | None:
    """Primeira invalidacao verdadeira (QUALQUER uma cancela) ou None."""
    for c in plano.invalidacoes:
        if condicao_satisfeita(c, ref=ref, est=est, t_armado_ns=t_armado_ns, tick_pontos=tick_pontos,
                               papel="invalidacao"):
            return c
    return None


def descrever(c: Condicao) -> str:
    base = f"{c.tipo}({c.valor})"
    return base + (f"@ate_ns={c.ate_ns}" if c.ate_ns is not None else "")


def validar_dsl(plano: PlanoCondicional | None) -> list[str]:
    """Validacao estrutural (DSL permitida, <= 4 gatilhos, expiracao <= 10 min). Para planos RELATIVOS."""
    return validar_plano(plano)


def validar_dsl_absoluto(plano: PlanoCondicional, t0_ns: int) -> list[str]:
    """Para planos ja convertidos: expira_ns/ate_ns sao instantes e devem ser posteriores a t0."""
    v: list[str] = []
    for c in (*plano.gatilhos, *plano.invalidacoes):
        if c.tipo not in TIPOS_CONDICAO:
            v.append(f"condicao fora da DSL permitida: {c.tipo}")
        if c.ate_ns is not None and c.ate_ns <= t0_ns:
            v.append("ate_ns anterior a t0")
    if plano.expira_ns <= t0_ns:
        v.append("expira_ns anterior a t0")
    return v
