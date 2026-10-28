"""Validacao DETERMINISTICA das saidas do LLM (§5.2 passo 4). Nenhuma decisao de IA passa sem isto."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from robowin.core.tipos import Acao, Condicao, PlanoCondicional, PropostaIA, RevisaoIA
from robowin.ia.dossie import caminhos_validos

TIPOS_CONDICAO = ("preco_toca", "preco_rompe_acima", "preco_rompe_abaixo", "spread_max_pontos", "janela_tempo")
EXPIRA_MAX_NS = 10 * 60 * 1_000_000_000
MAX_GATILHOS = 4
  # lista-de-proibicao (proib)
_RE_INDICADOR = re.compile(  # proib
    r"\b(rsi|macd|ifr|adx|ema|sma|atr|obv|cci|vwap)\b|bollinger|estoc[aá]stic|stochastic|ichimoku|fibonacci"  # proib
    r"|m[eé]dias?\s+m[oó]ve(l|is)|moving\s+average|cruzamento\s+de\s+m[eé]dias|golden\s+cross|death\s+cross"  # proib
    r"|bandas?\s+de\s+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResultadoValidacao:
    ok: bool
    violacoes: tuple[str, ...] = ()


def _resultado(v: list[str]) -> ResultadoValidacao:
    return ResultadoValidacao(not v, tuple(v))


def referencias_no_texto(texto: str, secoes: set[str]) -> list[str]:
    if not secoes:
        return []
    alt = "|".join(sorted(map(re.escape, secoes), key=len, reverse=True))
    return [m.group(0).rstrip(".") for m in re.finditer(rf"\b(?:{alt})(?:\.[A-Za-z0-9_]+)+", texto)]


def validar_evidencias(textos: tuple[str, ...], dossie: dict[str, Any]) -> list[str]:
    caminhos = caminhos_validos(dossie)
    secoes = {k for k in dossie}
    v: list[str] = []
    for t in textos:
        refs = referencias_no_texto(t, secoes)
        if not refs:
            v.append(f"evidencia sem referencia a campo do dossie: {t[:60]!r}")
        for r in refs:
            if r not in caminhos:
                v.append(f"evidencia cita campo inexistente no dossie: {r}")
    return v


def validar_textos_livres(textos: tuple[str, ...], *, max_chars: int = 600) -> list[str]:
    """Texto livre de um agente auxiliar (debate, reflexao): sem indicador tradicional e com tamanho limitado."""
    v: list[str] = []
    for t in textos:
        if len(t) > max_chars:
            v.append(f"texto acima de {max_chars} caracteres: {t[:40]!r}")
        if (m := _RE_INDICADOR.search(t)):
            v.append(f"indicador tecnico tradicional citado: {m.group(0)}")
    return v


def validar_texto_sem_indicadores(p: PropostaIA) -> list[str]:
    textos = [*p.hipoteses, *p.evidencias_a_favor, *p.evidencias_contra, *p.riscos, *p.invalidacoes,
              p.justificativa_curta]
    return [f"indicador tecnico tradicional citado: {m.group(0)}" for t in textos
            if (m := _RE_INDICADOR.search(t))]


def validar_coerencia_posicao(acao: Acao, dossie: dict[str, Any]) -> list[str]:
    pos = dossie.get("posicao") or {}
    qtd = int(pos.get("quantidade") or 0)
    lado = pos.get("lado")
    if acao in (Acao.COMPRAR, Acao.VENDER):
        if qtd > 0:
            return [f"{acao.value} com posicao aberta ({lado}): nunca aumentar nem inverter"]
    elif acao in (Acao.ENCERRAR, Acao.MANTER) and qtd == 0:
        return [f"{acao.value} sem posicao aberta"]
    return []


def validar_condicao(c: Condicao, relativa: bool = True) -> list[str]:
    v: list[str] = []
    if c.tipo not in TIPOS_CONDICAO:
        return [f"condicao fora da DSL permitida: {c.tipo}"]
    if c.valor is None:
        v.append(f"condicao {c.tipo} sem valor")
    elif c.tipo == "spread_max_pontos" and c.valor < 0:
        v.append("spread_max_pontos negativo")
    elif c.tipo == "janela_tempo" and c.valor <= 0:
        v.append("janela_tempo deve ser positiva")
    if c.ate_ns is not None and c.ate_ns <= 0:
        v.append("ate_ns deve ser positivo")
    return v


def validar_plano(plano: PlanoCondicional | None) -> list[str]:
    """DSL permitida: so condicoes de preco relativo, spread e tempo. Durações RELATIVAS a t0."""
    if plano is None:
        return []
    v: list[str] = []
    if not plano.gatilhos:
        v.append("plano sem gatilhos")
    if len(plano.gatilhos) > MAX_GATILHOS:
        v.append(f"plano com gatilhos demais (max {MAX_GATILHOS})")
    for c in (*plano.gatilhos, *plano.invalidacoes):
        v += validar_condicao(c)
    if not 0 < plano.expira_ns <= EXPIRA_MAX_NS:
        v.append("expira_ns fora de (0, 10 min]")
    return v


def validar_plano_bruto(d: dict[str, Any]) -> list[str]:
    """Para JSON bruto (antes do Pydantic): rejeita tipos desconhecidos (ex.: construcao de indicador)."""
    v: list[str] = []
    for lista in ("gatilhos", "invalidacoes"):
        for c in d.get(lista, []) or []:
            if not isinstance(c, dict) or c.get("tipo") not in TIPOS_CONDICAO:
                v.append(f"condicao fora da DSL permitida: {c.get('tipo') if isinstance(c, dict) else c!r}")
            elif set(c) - {"tipo", "valor", "ate_ns"}:
                v.append(f"campos extras na condicao: {sorted(set(c) - {'tipo', 'valor', 'ate_ns'})}")
    return v


def plano_para_absoluto(plano: PlanoCondicional, t0_ns: int) -> PlanoCondicional:
    """O LLM nao conhece o horario: expira_ns/ate_ns chegam como duracoes; aqui viram instantes."""
    def conv(c: Condicao) -> Condicao:
        return c.model_copy(update={"ate_ns": None if c.ate_ns is None else t0_ns + c.ate_ns})
    return PlanoCondicional(
        gatilhos=tuple(conv(c) for c in plano.gatilhos), invalidacoes=tuple(conv(c) for c in plano.invalidacoes),
        expira_ns=t0_ns + plano.expira_ns)


def validar_proposta(
    p: PropostaIA, dossie: dict[str, Any], *, modo: str = "imediato", validade_max_s: int = 60,
    stop_max_pontos: float,
) -> ResultadoValidacao:
    v: list[str] = []
    entrada = p.acao in (Acao.COMPRAR, Acao.VENDER)
    v += validar_evidencias((*p.evidencias_a_favor, *p.evidencias_contra), dossie)
    if entrada and not p.evidencias_a_favor:
        v.append("entrada sem evidencias a favor")
    v += validar_coerencia_posicao(p.acao, dossie)
    v += validar_texto_sem_indicadores(p)
    if entrada:
        if p.stop_pontos is None or p.stop_pontos <= 0:
            v.append("entrada sem stop_pontos positivo")
        elif p.stop_pontos > stop_max_pontos:
            v.append(f"stop_pontos acima do maximo ({stop_max_pontos})")
        if p.alvo_pontos is None or p.alvo_pontos <= 0:
            v.append("entrada sem alvo_pontos positivo")
    if not 0 < p.validade_s <= validade_max_s:
        v.append(f"validade_s fora de (0, {validade_max_s}]")
    if p.plano is not None:
        if modo != "condicional":
            v.append("plano condicional fora do modo condicional")
        v += validar_plano(p.plano)
    return _resultado(v)


def validar_frescor(*, t0_ns: int, agora_ns: int, validade_s: int, preco_ref: float, preco_atual: float,
                    desvio_max_pts: float) -> ResultadoValidacao:
    v: list[str] = []
    if agora_ns - t0_ns > validade_s * 1_000_000_000:
        v.append("proposta vencida (agora - t0 > validade)")
    if abs(preco_atual - preco_ref) > desvio_max_pts:
        v.append("desvio de preco acima da tolerancia")
    return _resultado(v)


def revisao_aprova(r: RevisaoIA | None) -> bool:
    """Aprovacao so se o veredito for APROVAR e todos os controles internos forem verdadeiros."""
    return (r is not None and r.veredito == "APROVAR" and r.coerente and r.dados_ok and r.regras_ok
            and not r.afirmacoes_sem_suporte)
