"""Dossie ANONIMIZADO entregue ao LLM (§5.2).

O dossie contem apenas: pontos relativos ao ultimo preco, volatilidade normalizada, codigos de categoria,
features da lista permitida, saidas de modelos com status, StatArb, regime, posicao e limites.
Nao contem: datas, horarios absolutos, niveis absolutos de preco, niveis do WDO, texto de noticia,
nome de simbolo ou qualquer coisa que identifique o periodo (objetivo: reduzir contaminacao por memoria
do modelo sobre mercados passados).
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Sequence
from typing import Any

import numpy as np

JANELAS_S = (10, 30, 60, 300, 900)
STATUS_VALIDOS = ("nao_treinado", "em_validacao", "validado_oos", "reprovado")

FEATURES_PERMITIDAS_PADRAO = frozenset({
    "desbalanco_book", "spread_ticks", "agressao_liquida", "intensidade_fluxo", "taxa_ticks",
    "profundidade_rel", "vol_norm", "ret_pts_10s", "ret_pts_60s", "ret_pts_300s",
    "dist_extremo_300s_pts", "z_statarb", "volume_rel",
})

CODIGOS_CATEGORIA = {
    "juros": "C01", "cambio": "C02", "inflacao": "C03", "emprego": "C04", "politica_monetaria": "C05",
    "fiscal": "C06", "exterior": "C07", "commodities": "C08", "corporativo": "C09", "politica": "C10",
    "geopolitica": "C11", "atividade": "C12",
}
CATEGORIA_DESCONHECIDA = "C99"

_TERMOS_INDICADOR = ("rsi", "macd", "media_movel", "mm_", "ema", "sma", "bollinger", "estocastico",  # lista-de-proibicao (proib)
                     "stochastic", "cruzamento", "ifr", "atr", "adx", "ichimoku", "fibonacci")  # lista-de-proibicao (proib)
_CHAVES_PROIBIDAS = {"data", "datas", "date", "timestamp", "t_ns", "t0_ns", "hora", "simbolo", "symbol",
                     "bid", "ask", "last", "preco", "preco_absoluto", "ultimo", "wdo", "titulo", "texto",
                     "manchete", "url", "fonte"}
_RE_STRING_PROIBIDA = re.compile(
    r"(\d{4}-\d{2}-\d{2})|(\d{1,2}/\d{1,2}/\d{2,4})|(\b(19|20)\d{2}\b)|(\bWDO\w*)|(\bWIN[FGHJKMNQUVXZ]\d{2}\b)"
    r"|(\b(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro|january"
    r"|february|march|april|june|july|august|september|october|november|december)\b)"
    r"|(\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)/\d{2,4})"
    r"|(\b(segunda|terca|quarta|quinta|sexta|monday|tuesday|wednesday|thursday|friday)\b)"
    r"|(\bibov\w*)|(\bcopom\b)|(\bfomc\b)|(\bpayroll\b)|(\bcovid\w*)",
    re.IGNORECASE,
)
_LIMITE_NIVEL_ABS = 50_000.0
_LIMITE_TIMESTAMP = 1e8


class ErroDossie(ValueError):
    pass


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), nd)


def canonico(d: Any) -> str:
    return json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def hash_dossie(d: dict[str, Any]) -> str:
    return hashlib.sha256(canonico(d).encode("utf-8")).hexdigest()


def _percorrer(d: Any, caminho: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _percorrer(v, f"{caminho}.{k}" if caminho else str(k))
    elif isinstance(d, (list, tuple)):
        for i, v in enumerate(d):
            yield from _percorrer(v, f"{caminho}[{i}]")
    else:
        yield caminho, d


def _chaves(d: Any) -> Iterable[str]:
    if isinstance(d, dict):
        for k, v in d.items():
            yield str(k)
            yield from _chaves(v)
    elif isinstance(d, (list, tuple)):
        for v in d:
            yield from _chaves(v)


def caminhos_validos(d: dict[str, Any]) -> set[str]:
    """Todos os caminhos pontuados do dossie (secoes e folhas), usados para checar evidencias."""
    out: set[str] = set()

    def rec(x: Any, pref: str) -> None:
        if pref:
            out.add(pref)
        if isinstance(x, dict):
            for k, v in x.items():
                rec(v, f"{pref}.{k}" if pref else str(k))
        elif isinstance(x, (list, tuple)):
            for i, v in enumerate(x):
                rec(v, f"{pref}.{i}")
    rec(d, "")
    return out


def _perto(ordenados: list[float], v: float) -> bool:
    """Existe nivel a menos de 0,5 de v? (busca binaria; lista ordenada)"""
    import bisect
    i = bisect.bisect_left(ordenados, v - 0.5)
    return i < len(ordenados) and ordenados[i] < v + 0.5


def verificar_anonimizacao(d: dict[str, Any], niveis_absolutos: Iterable[float] = ()) -> list[str]:
    """Lista de violacoes (vazia = anonimizado)."""
    erros: list[str] = []
    for k in _chaves(d):
        if k.lower() in _CHAVES_PROIBIDAS:
            erros.append(f"chave proibida: {k}")
    niveis = sorted(float(n) for n in niveis_absolutos if abs(float(n)) > 100)
    for caminho, v in _percorrer(d):
        if isinstance(v, str):
            if _RE_STRING_PROIBIDA.search(v):
                erros.append(f"texto identifica periodo/ativo em {caminho}: {v[:40]!r}")
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            if abs(v) >= _LIMITE_TIMESTAMP:
                erros.append(f"valor parece timestamp em {caminho}")
            elif abs(v) >= _LIMITE_NIVEL_ABS:
                erros.append(f"valor parece nivel absoluto em {caminho}")
            elif _perto(niveis, v):
                erros.append(f"valor coincide com nivel absoluto em {caminho}")
    return erros


def _preco_em(ticks: Sequence[tuple[int, float]], t_ns: int) -> float | None:
    """Ultimo preco com t <= t_ns (ticks em ordem cronologica)."""
    achado = None
    for t, p in ticks:
        if t <= t_ns:
            achado = p
        else:
            break
    return achado


def _vol(precos: np.ndarray) -> float | None:
    if len(precos) < 3:
        return None
    return float(np.std(np.diff(precos)))


def secao_preco(ticks: Sequence[tuple[int, float]], t0_ns: int) -> dict[str, Any]:
    """Pontos relativos ao ULTIMO preco; nenhuma data nem nivel absoluto."""
    if not ticks:
        raise ErroDossie("sem ticks para montar o dossie")
    ticks = [(t, p) for t, p in ticks if t <= t0_ns]
    if not ticks:
        raise ErroDossie("todos os ticks sao posteriores a t0 (vazamento)")
    ultimo = ticks[-1][1]
    out: dict[str, Any] = {}
    for j in JANELAS_S:
        ref = _preco_em(ticks, t0_ns - j * 1_000_000_000)
        out[f"ret_{j}s_pts"] = None if ref is None else _r(ultimo - ref)
    def janela(j: int) -> np.ndarray:
        return np.array([p for t, p in ticks if t >= t0_ns - j * 1_000_000_000], dtype=float)
    j5 = janela(300)
    out["amplitude_300s_pts"] = _r(float(j5.max() - j5.min())) if len(j5) else None
    out["dist_maxima_300s_pts"] = _r(float(j5.max() - ultimo)) if len(j5) else None
    out["dist_minima_300s_pts"] = _r(float(ultimo - j5.min())) if len(j5) else None
    v_curta, v_longa = _vol(janela(60)), _vol(janela(900))
    out["vol_norm"] = _r(v_curta / v_longa, 3) if v_curta is not None and v_longa else None
    out["ticks_60s"] = int(len(janela(60)))
    return out


def montar_dossie(
    *, t0_ns: int, ticks: Sequence[tuple[int, float]], spread_pontos: float | None = None,
    features: dict[str, float] | None = None, features_permitidas: Collection[str] | None = None,
    modelos: dict[str, dict[str, Any]] | None = None, statarb: dict[str, float] | None = None,
    noticias: Sequence[dict[str, Any]] = (), regime: str = "indefinido",
    posicao: dict[str, Any] | None = None, limites: dict[str, float] | None = None,
    memoria: Sequence[dict[str, Any]] = (), minutos_desde_abertura: int | None = None,
    minutos_para_zeragem: int | None = None, tick_pontos: float = 5.0,
    niveis_extras_absolutos: Iterable[float] = (),
) -> dict[str, Any]:
    """Monta o dossie anonimizado.

    ``ticks``: (t_ns, preco absoluto), ate ``t0_ns``. ``posicao`` (se aberta): {lado, quantidade,
    preco_medio, aberta_ns, stop_pontos, alvo_pontos}. ``noticias``: [{categoria, direcao, relevancia,
    intensidade, horizonte_min, t_disponivel_ns}] - o texto NUNCA entra.
    """
    permitidas = frozenset(features_permitidas) if features_permitidas is not None else FEATURES_PERMITIDAS_PADRAO
    feats: dict[str, float] = {}
    for nome, val in (features or {}).items():
        if nome not in permitidas:
            raise ErroDossie(f"feature fora da lista permitida: {nome}")
        if any(t in nome.lower() for t in _TERMOS_INDICADOR):
            raise ErroDossie(f"feature parece indicador tecnico tradicional: {nome}")
        feats[nome] = _r(val, 4)  # type: ignore[assignment]

    ultimo = [p for t, p in ticks if t <= t0_ns][-1] if any(t <= t0_ns for t, _ in ticks) else None
    d: dict[str, Any] = {
        "meta": {"versao_dossie": 1},
        "movimento": secao_preco(ticks, t0_ns),
        "microestrutura": {"spread_ticks": _r(spread_pontos / tick_pontos, 2) if spread_pontos is not None else None},
        "features": feats,
        "modelos": {},
        "statarb": {k: _r(v, 3) for k, v in (statarb or {}).items()},
        "nlp": [],
        "regime": regime,
        "sessao": {"min_desde_abertura": minutos_desde_abertura, "min_para_zeragem": minutos_para_zeragem},
        "posicao": {"lado": None, "quantidade": 0},
        "limites": {k: _r(v, 2) for k, v in (limites or {}).items()},
        "memoria": [],
    }
    for nome, m in (modelos or {}).items():
        status = m.get("status", "nao_treinado")
        if status not in STATUS_VALIDOS:
            raise ErroDossie(f"status de modelo invalido: {status}")
        d["modelos"][nome] = {"status": status,
                              "saida": {k: _r(v, 4) for k, v in (m.get("saida") or {}).items()}}
    for n in noticias:
        idade = max(0, (t0_ns - int(n.get("t_disponivel_ns", t0_ns))) // 60_000_000_000)
        if n.get("t_disponivel_ns", t0_ns) > t0_ns:
            continue  # ainda nao disponivel em t0: nao entra (sem vazamento)
        d["nlp"].append({
            "idade_min": int(idade), "categoria": CODIGOS_CATEGORIA.get(str(n.get("categoria", "")), CATEGORIA_DESCONHECIDA),
            "direcao": n.get("direcao", "neutra"), "relevancia": _r(n.get("relevancia", 0.0), 2),
            "intensidade": _r(n.get("intensidade", 0.0), 2), "horizonte_min": int(n.get("horizonte_min", 0)),
        })
    if posicao and posicao.get("quantidade", 0) > 0 and ultimo is not None:
        sinal = 1.0 if str(posicao["lado"]) in ("compra", "Lado.COMPRA") else -1.0
        d["posicao"] = {
            "lado": "compra" if sinal > 0 else "venda", "quantidade": int(posicao["quantidade"]),
            "resultado_pontos": _r(sinal * (ultimo - float(posicao["preco_medio"]))),
            "tempo_aberta_s": int(max(0, (t0_ns - int(posicao.get("aberta_ns", t0_ns))) // 1_000_000_000)),
            "stop_pontos": posicao.get("stop_pontos"), "alvo_pontos": posicao.get("alvo_pontos"),
        }
    for m in memoria:
        d["memoria"].append({
            "acao": str(m.get("acao", "")), "resultado_pontos": _r(m.get("resultado_pontos", 0.0)),
            "duracao_s": int(m.get("duracao_s", 0)), "motivo_saida": str(m.get("motivo_saida", "")),
        })
    niveis = [p for _, p in ticks] + list(niveis_extras_absolutos)
    if posicao and posicao.get("preco_medio"):
        niveis.append(float(posicao["preco_medio"]))
    erros = verificar_anonimizacao(d, niveis)
    if erros:
        raise ErroDossie("dossie nao anonimizado: " + "; ".join(erros))
    return d
