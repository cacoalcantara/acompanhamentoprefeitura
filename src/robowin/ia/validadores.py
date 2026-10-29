"""Validacao DETERMINISTICA das saidas do LLM (§5.2 passo 4). Nenhuma decisao de IA passa sem isto."""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from robowin.core.tipos import (
    Acao,
    AjustePosicao,
    Condicao,
    ConfigPlanejador,
    EnvelopeDecisao,
    PlanoCondicional,
    PlanoTradeIA,
    PropostaIA,
    RegraGestao,
    RevisaoIA,
    RevisaoPlanoIA,
)
from robowin.ia.dossie import caminhos_validos, valor_no_caminho, verificar_anonimizacao

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
# texto do LLM e DADO, nunca instrucao: sem chaves/colchetes/crases/sinais de marcacao e sem termos de instrucao
# (antes em ia/reflexao.py; agora fonte unica para planejador, proponente->revisor, debate e resumos)
_RE_PERIGOSO = re.compile(r"[{}\[\]`<>\\]|ignore|desconsidere|system|instruc", re.IGNORECASE)
_RE_NUMERO = re.compile(r"-?\d+(?:[.,]\d+)?")


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


def _numero(txt: str) -> float:
    return float(txt.replace(",", "."))


def _confere_valor(ref: str, citado: str, dossie: dict[str, Any], tol_rel: float, tol_abs: float) -> list[str]:
    real = valor_no_caminho(dossie, ref)
    if isinstance(real, bool) or not isinstance(real, (int, float)):
        return [f"evidencia cita valor numerico para campo nao numerico: {ref}"]
    v = _numero(citado)
    if not math.isfinite(v) or abs(v - float(real)) > max(tol_abs, tol_rel * abs(float(real))):
        return [f"evidencia cita valor divergente do dossie: {ref} = {citado} (dossie: {real})"]
    return []


def validar_evidencias(textos: Sequence[str], dossie: dict[str, Any], *, conferir_valores: bool = False,
                       tol_rel: float = 0.01, tol_abs: float = 0.01) -> list[str]:
    """Cada evidencia cita ao menos um campo existente do dossie. Com ``conferir_valores`` (planejador), o valor
    escrito em ``campo = valor`` precisa bater com o dossie (tolerancia ``max(tol_abs, tol_rel*|real|)``) e citar
    campo ``null`` (dado indisponivel) e recusado."""
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
            elif conferir_valores and valor_no_caminho(dossie, r) is None:
                v.append(f"evidencia cita campo nulo (dado indisponivel): {r}")
        if conferir_valores:
            for m in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)\s*=\s*(-?\d+(?:[.,]\d+)?)", t):
                ref = m.group(1).rstrip(".")
                if ref in caminhos and valor_no_caminho(dossie, ref) is not None:
                    v += _confere_valor(ref, m.group(2), dossie, tol_rel, tol_abs)
    return v


def texto_seguro(texto: str, niveis_absolutos: Iterable[float] = ()) -> list[str]:
    """Violacoes de um texto livre do LLM antes de ele seguir para outro papel, para o registro ou para o
    resumo: marcacao/termo de instrucao (injecao), periodo/ativo/nivel absoluto (anonimizacao) e indicador
    tecnico tradicional. Lista vazia = texto seguro."""
    v: list[str] = []
    if (m := _RE_PERIGOSO.search(texto)):
        v.append(f"texto com caractere/termo de instrucao ou marcacao: {m.group(0)!r}")
    v += [f"texto livre: {e}" for e in verificar_anonimizacao({"livre": [texto]})]
    niveis = sorted(float(n) for n in niveis_absolutos if abs(float(n)) > 100)
    if niveis:
        import bisect
        for m in _RE_NUMERO.finditer(texto):
            x = _numero(m.group(0))
            i = bisect.bisect_left(niveis, x - 0.5)
            if i < len(niveis) and niveis[i] < x + 0.5:
                v.append(f"texto cita numero que coincide com nivel absoluto: {m.group(0)}")
                break
    if (m := _RE_INDICADOR.search(texto)):
        v.append(f"indicador tecnico tradicional citado: {m.group(0)}")
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


# ================================================================================ braco planejador (docs/16 WP4)
# O validador so RESTRINGE a IA ao envelope DERIVADO (pontos/unidades/segundos) e aos parametros pre-registrados
# de [planejador]; nunca le limites, arquivos, usuario ou conta. O veto final continua no motor de risco.
TIPOS_REGRA_GESTAO = ("breakeven", "trailing", "parcial", "sem_progresso", "apertar_apos", "ao_vencer_tempo")
MAX_CHARS_JUSTIFICATIVA = 600
_EPS = 1e-9


def _nao_finitos(x: Any, caminho: str = "") -> Iterable[str]:
    if isinstance(x, dict):
        for k, v in x.items():
            yield from _nao_finitos(v, f"{caminho}.{k}" if caminho else str(k))
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            yield from _nao_finitos(v, f"{caminho}.{i}")
    elif isinstance(x, float) and not math.isfinite(x):
        yield caminho


def _textos_plano(p: PlanoTradeIA) -> tuple[str, ...]:
    return (*p.hipoteses, *p.evidencias_a_favor, *p.evidencias_contra, *p.riscos, *p.invalidacoes,
            p.justificativa_curta)


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        return None
    return float(x)


def validar_regras_gestao(regras: Sequence[RegraGestao], *, quantidade: int, env: EnvelopeDecisao,
                          cfg: ConfigPlanejador, tempo_max_s: int | None = None) -> list[str]:
    """Estrutura das regras de gestao (o motor refaz uma checagem independente). Distancias >= dist_min."""
    v: list[str] = []
    if len(regras) > cfg.max_regras:
        v.append(f"gestao_regras_demais: {len(regras)} > {cfg.max_regras}")
    dmin = env.dist_min_ajuste_pontos
    for i, r in enumerate(regras):
        nome = f"gestao.{i}:{r.tipo}"
        if r.tipo not in TIPOS_REGRA_GESTAO:
            v.append(f"gestao_tipo_desconhecido: {nome}")
            continue
        for campo in ("mfe_pontos", "distancia_pontos"):
            x = getattr(r, campo)
            if x is not None and (not math.isfinite(x) or x < 0):
                v.append(f"gestao_valor_invalido: {nome}.{campo}")
        for campo in ("apos_s", "folga_ticks", "quantidade", "extensao_s"):
            x = getattr(r, campo)
            if x is not None and x < 0:
                v.append(f"gestao_valor_invalido: {nome}.{campo}")
        if r.distancia_pontos is not None and r.distancia_pontos + _EPS < dmin:
            v.append(f"gestao_distancia_abaixo_do_minimo: {nome} ({r.distancia_pontos} < {dmin})")
        if r.tipo in ("breakeven", "trailing", "parcial", "sem_progresso") and (
                r.mfe_pontos is None or r.mfe_pontos <= 0):
            v.append(f"gestao_sem_mfe: {nome}")
        if r.tipo in ("trailing", "apertar_apos") and r.distancia_pontos is None:
            v.append(f"gestao_sem_distancia: {nome}")
        if r.tipo in ("sem_progresso", "apertar_apos"):
            if r.apos_s is None or r.apos_s <= 0:
                v.append(f"gestao_sem_apos_s: {nome}")
            elif tempo_max_s is not None and r.apos_s >= tempo_max_s:
                v.append(f"gestao_apos_s_alem_do_tempo: {nome}")
        if r.tipo == "parcial":
            if quantidade < 2:
                v.append(f"gestao_parcial_com_q1: {nome}")
            elif r.quantidade is None or not 1 <= r.quantidade < quantidade:
                v.append(f"gestao_parcial_quantidade_invalida: {nome}")
        if r.tipo == "ao_vencer_tempo":
            if r.acao_vencimento is None:
                v.append(f"gestao_sem_acao_vencimento: {nome}")
            elif r.acao_vencimento == "proteger_e_estender" and (r.extensao_s is None or r.extensao_s <= 0):
                v.append(f"gestao_sem_extensao: {nome}")
    return v


def _validar_gatilhos(gatilhos: Sequence[Condicao]) -> list[str]:
    v: list[str] = []
    if len(gatilhos) > MAX_GATILHOS:
        v.append(f"gatilhos_demais: max {MAX_GATILHOS}")
    for c in gatilhos:
        v += [f"gatilho_invalido: {e}" for e in validar_condicao(c)]
        if c.ate_ns is not None:
            v.append(f"gatilho_com_ate_ns: use janela_tempo com valor ({c.tipo})")
    return v


def _validar_entrada(p: PlanoTradeIA, *, env: EnvelopeDecisao, cfg: ConfigPlanejador, tick_pontos: float,
                     custo_ida_volta_pontos: float, slip_stop_medio_pontos: float) -> list[str]:
    v: list[str] = []
    if not env.entradas_possiveis:
        v.append("entrada_impossivel: " + (",".join(env.motivos_sem_entrada) or "envelope"))
    faltando = [c for c in ("stop_pontos", "alvo_pontos", "tempo_max_s", "faixa_contra_pontos", "faixa_favor_pontos")
                if getattr(p, c) is None]
    if cfg.exigir_p_alvo and p.p_alvo is None:
        faltando.append("p_alvo")
    if faltando:
        v.append("entrada_sem_campo: " + ",".join(faltando))
    if not p.invalidacoes:
        v.append("entrada_sem_invalidacoes")
    if not p.evidencias_a_favor:
        v.append("entrada_sem_evidencias_a_favor")
    if not p.evidencias_contra:
        v.append("entrada_sem_evidencias_contra")
    if p.ajuste is not None:
        v.append("ajuste_fora_de_manter")
    q = p.quantidade
    q_ok = 1 <= q <= env.unidades_max and q <= len(env.stop_max_pontos_por_q)
    if not q_ok:
        v.append(f"quantidade_fora_do_envelope: {q} (1..{env.unidades_max})")
    stop, alvo, contra, favor = p.stop_pontos, p.alvo_pontos, p.faixa_contra_pontos, p.faixa_favor_pontos
    if contra is not None and contra < 0:
        v.append("faixa_contra_negativa")
    if favor is not None and favor < 0:
        v.append("faixa_favor_negativa")
    fav = max(favor or 0.0, 0.0)
    if stop is not None:
        if stop + _EPS < env.stop_min_pontos or stop <= 0:
            v.append(f"stop_abaixo_do_minimo: {stop} < {env.stop_min_pontos}")
        if q_ok and stop > env.stop_max_pontos_por_q[q - 1] - fav + _EPS:
            v.append(f"stop_acima_do_envelope: {stop} > {env.stop_max_pontos_por_q[q - 1]} - faixa_favor {fav}")
        if contra is not None and contra > stop - env.stop_min_pontos + _EPS:
            v.append("faixa_contra_acima_do_aceito: faixa_contra > stop - stop_min")
        if favor is not None and favor > min(cfg.tolerancia_max_fracao_stop * stop,
                                             cfg.tolerancia_max_ticks * tick_pontos) + _EPS:
            v.append("faixa_favor_acima_do_aceito")
    if alvo is not None:
        minimo = cfg.k_custo * custo_ida_volta_pontos + tick_pontos
        if alvo - fav <= minimo + _EPS:
            v.append(f"alvo_nao_cobre_custo: alvo - faixa_favor <= {minimo:g}")
    if p.tempo_max_s is not None and not cfg.tempo_min_s <= p.tempo_max_s <= env.tempo_max_s:
        v.append(f"tempo_fora_do_envelope: {p.tempo_max_s} fora de [{cfg.tempo_min_s}, {env.tempo_max_s}]")
    if not 0 < p.expira_s <= cfg.expira_max_s:
        v.append(f"expira_fora_do_aceito: {p.expira_s} fora de (0, {cfg.expira_max_s}]")
    v += _validar_gatilhos(p.gatilhos)
    v += validar_regras_gestao(p.gestao, quantidade=q, env=env, cfg=cfg, tempo_max_s=p.tempo_max_s)
    if p.reavaliar_em_s is not None:
        teto = p.tempo_max_s if p.tempo_max_s is not None else env.tempo_max_s
        if not cfg.reavaliar_min_s <= p.reavaliar_em_s <= teto:
            v.append(f"reavaliar_em_s_fora: {p.reavaliar_em_s} fora de [{cfg.reavaliar_min_s}, {teto}]")
    if cfg.razao_liquida_min > 0 and stop is not None and alvo is not None:
        c = custo_ida_volta_pontos
        den = stop + c + slip_stop_medio_pontos
        if den <= 0 or (alvo - c) / den + _EPS < cfg.razao_liquida_min:
            v.append(f"razao_liquida_abaixo_do_minimo: {cfg.razao_liquida_min}")
    return v


def _validar_ajuste(a: AjustePosicao, dossie: dict[str, Any], *, env: EnvelopeDecisao,
                    cfg: ConfigPlanejador) -> list[str]:
    """MANTER com ajuste: niveis em PONTOS DE RESULTADO desde o preco medio (-30 perde 30; 0 empate; +10 trava 10).
    Le do dossie os valores ATUAIS da posicao: ``resultado_pontos``, ``stop_resultado_pontos``, ``tempo_aberta_s``."""
    v: list[str] = []
    bruto = dossie.get("posicao")
    pos: dict[str, Any] = bruto if isinstance(bruto, dict) else {}
    q = int(pos.get("quantidade") or 0)
    if q <= 0:
        return ["ajuste_sem_posicao"]
    resultado = _num(pos.get("resultado_pontos"))
    dmin = env.dist_min_ajuste_pontos
    if (a.stop_resultado_pontos is not None or a.alvo_resultado_pontos is not None) and resultado is None:
        v.append("ajuste_sem_resultado_atual")
    if a.stop_resultado_pontos is not None:
        s = a.stop_resultado_pontos
        atual = _num(pos.get("stop_resultado_pontos"))
        if atual is None:
            v.append("ajuste_stop_sem_stop_atual")
        elif s + _EPS < atual:
            v.append(f"ajuste_stop_afrouxaria: {s} < atual {atual}")
        if resultado is not None and s > resultado - dmin + _EPS:
            v.append(f"ajuste_stop_colado_no_preco: {s} > resultado {resultado} - {dmin}")
    if a.alvo_resultado_pontos is not None and resultado is not None \
            and a.alvo_resultado_pontos <= resultado + dmin + _EPS:
        v.append(f"ajuste_alvo_colado_ou_atras: {a.alvo_resultado_pontos} <= resultado {resultado} + {dmin}")
    if a.tempo_max_total_s is not None:
        aberta = _num(pos.get("tempo_aberta_s"))
        if aberta is None:
            v.append("ajuste_sem_tempo_aberta")
        elif not aberta < a.tempo_max_total_s <= env.tempo_max_s:
            v.append(f"ajuste_tempo_fora: {a.tempo_max_total_s} fora de ({aberta:g}, {env.tempo_max_s}]")
    if a.reduzir_quantidade is not None and not 1 <= a.reduzir_quantidade < q:
        v.append(f"ajuste_reducao_invalida: {a.reduzir_quantidade} (1..{q - 1})")
    if a.substituir_gestao is not None:
        v += validar_regras_gestao(a.substituir_gestao, quantidade=q, env=env, cfg=cfg,
                                   tempo_max_s=a.tempo_max_total_s)
    return v


def validar_plano_trade(
    p: PlanoTradeIA, dossie: dict[str, Any], *, env: EnvelopeDecisao | None, cfg: ConfigPlanejador,
    tick_pontos: float, custo_ida_volta_pontos: float, slip_stop_medio_pontos: float,
    niveis_absolutos: Iterable[float] = (),
) -> ResultadoValidacao:
    """Validacao deterministica do ``PlanoTradeIA`` (docs/16 WP4) contra o dossie e o envelope DERIVADO.

    Recusa todo valor nao finito, entrada fora do envelope (quantidade, stop por quantidade descontada a faixa a
    favor, tempo), alvo que nao cobre o custo, faixa larga demais, gatilhos fora da DSL, gestao invalida, ajuste
    que afrouxaria o stop e todo texto livre inseguro. ``env`` ausente = entrada e ajuste recusados (fail-closed).
    """
    v: list[str] = [f"valor_nao_finito: {c}" for c in _nao_finitos(p.model_dump(mode="python"))]
    for nome, x in (("tick_pontos", tick_pontos), ("custo_ida_volta_pontos", custo_ida_volta_pontos),
                    ("slip_stop_medio_pontos", slip_stop_medio_pontos)):
        if not math.isfinite(x) or x < 0 or (nome == "tick_pontos" and x <= 0):
            v.append(f"parametro_invalido: {nome}")
    if v:
        return _resultado(v)  # nada de comparar NaN: recusa direta
    niveis = tuple(niveis_absolutos)
    for t in _textos_plano(p):
        v += texto_seguro(t, niveis)
    if len(p.justificativa_curta) > MAX_CHARS_JUSTIFICATIVA:
        v.append(f"justificativa_acima_de_{MAX_CHARS_JUSTIFICATIVA}_caracteres")
    v += validar_coerencia_posicao(p.acao, dossie)
    v += validar_evidencias((*p.evidencias_a_favor, *p.evidencias_contra), dossie, conferir_valores=True)
    if p.acao in (Acao.COMPRAR, Acao.VENDER):
        if env is None:
            v.append("sem_envelope")
        else:
            v += _validar_entrada(p, env=env, cfg=cfg, tick_pontos=tick_pontos,
                                  custo_ida_volta_pontos=custo_ida_volta_pontos,
                                  slip_stop_medio_pontos=slip_stop_medio_pontos)
    elif p.ajuste is not None:
        if p.acao != Acao.MANTER:
            v.append("ajuste_fora_de_manter")
        elif env is None:
            v.append("sem_envelope")
        else:
            v += _validar_ajuste(p.ajuste, dossie, env=env, cfg=cfg)
    elif p.acao not in (Acao.MANTER, Acao.ENCERRAR, Acao.NAO_OPERAR):
        v.append(f"acao_desconhecida: {p.acao}")
    return _resultado(v)


def revisao_plano_aprova(r: RevisaoPlanoIA | None) -> bool:
    """Aprovacao do revisor do plano: veredito APROVAR e os tres booleanos (sem padrao) verdadeiros."""
    return (r is not None and r.veredito == "APROVAR" and r.coerente is True and r.dados_ok is True
            and r.regras_ok is True)
