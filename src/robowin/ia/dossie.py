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
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any

import numpy as np

from robowin.features.registro import REGISTRO_PERMITIDO, nomes_permitidos

JANELAS_S = (10, 30, 60, 300, 900)
STATUS_VALIDOS = ("nao_treinado", "em_validacao", "validado_oos", "reprovado")

# padrao = o registro de features (C28: a lista antiga tinha nomes que nao existem no registro)
FEATURES_PERMITIDAS_PADRAO = frozenset(nomes_permitidos())

# ---- dossie v2 (braco planejador; docs/16 WP4) -----------------------------------------------------------------
# Secoes que o chamador entrega prontas em ``extras`` (decisao/dossie_conta.secoes_planejador).
SECOES_V2 = ("instrumento", "custos", "envelope", "sessao", "mercado", "posicao", "plano_vigente", "retorno",
             "latencia_ia")
# `_perto` (valor coincide com nivel absoluto) so vale nas secoes que carregam distancias de preco, e nunca em chaves
# de dinheiro, conta, segundos, minutos ou contagens (C29: falso positivo com ativo barato e valor de 100 a 50.000).
SECOES_PERTO = ("movimento", "posicao", "statarb", "features", "gestao", "plano_vigente")
SUFIXOS_ISENTOS_PERTO = ("_brl", "_conta", "_s", "_min", "_restantes")
# v2: nenhum nome de limite do usuario/conta (nomes de ConfigRisco), nenhum valor em dinheiro, nenhum dado de
# arquivo/usuario/conta. O envelope derivado (pontos/unidades/segundos) e a unica visao dos limites.
CHAVES_LIMITE_V2 = frozenset({
    "limites", "contratos_max", "perda_max_operacao_brl", "perda_max_dia_brl", "perda_max_semana_brl",
    "dd_alerta_brl", "dd_max_brl", "operacoes_dia_max", "perdas_seguidas_max", "pausa_apos_perda_s",
    "stop_max_pontos", "slippage_folga_pontos", "entradas_de", "entradas_ate", "zeragem", "bloqueio_evento_min",
    "ordens_min_max", "tempo_min_livro_s", "volume_max_lotes", "nocional_max_conta", "tempo_max_posicao_s",
    "usuario", "conta", "login", "arquivo", "hash", "aprovacao", "perda_restante_dia_brl", "drawdown_brl",
})
# unidades que NAO entram no v2 (C30): retorno logaritmico ou valor relativo ao preco reconstroem o nivel absoluto
# junto com as distancias em pontos.
UNIDADES_PROIBIDAS_V2 = ("log", "relativa")

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


def _padrao_ativos() -> str:
    """Regex dos NOMES de ativo de config/instrumentos.toml (futuros com codigo de vencimento; acoes/ETF pelo ticker)."""
    from robowin.core.instrumentos import _bruto, ativos_disponiveis
    try:
        brutos = _bruto()
        nomes = ativos_disponiveis()
    except Exception:  # sem arquivo de perfis: so os nomes genericos de contrato
        return r"(\b[A-Z]{3}[FGHJKMNQUVXZ]\d{2}\b)"
    fut = [n for n in nomes if brutos[n].get("vencimento", "nenhum") != "nenhum"]
    partes = [rf"(\b(?:{'|'.join(map(re.escape, fut))})[FGHJKMNQUVXZ]\d{{2}}\b)"] if fut else []
    # nome do ativo sozinho: so em MAIUSCULAS (evita "indicador", "window"); tickers de acao/ETF idem
    partes += [rf"((?-i:\b(?:{'|'.join(map(re.escape, nomes))})\w*\b))"] if nomes else []
    return "|".join(partes)


_RE_STRING_PROIBIDA = re.compile(
    r"(\d{4}-\d{2}-\d{2})|(\d{1,2}/\d{1,2}/\d{2,4})|(\b(19|20)\d{2}\b)|" + _padrao_ativos() +
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


def valor_no_caminho(d: dict[str, Any], caminho: str) -> Any:
    """Valor da folha (ou secao) no caminho pontuado de ``caminhos_validos``; ``None`` se nao existir."""
    x: Any = d
    for parte in caminho.split("."):
        if isinstance(x, dict):
            if parte not in x:
                return None
            x = x[parte]
        elif isinstance(x, (list, tuple)) and parte.isdigit() and int(parte) < len(x):
            x = x[int(parte)]
        else:
            return None
    return x


def _isento_perto(caminho: str) -> bool:
    """C29: so secoes de distancia de preco; chaves de dinheiro/conta/segundos/minutos/contagens ficam isentas."""
    secao = re.split(r"[.\[]", caminho, maxsplit=1)[0]
    if secao not in SECOES_PERTO:
        return True
    folha = re.sub(r"\[\d+\]$", "", caminho).rsplit(".", 1)[-1]
    return folha.endswith(SUFIXOS_ISENTOS_PERTO)


def _perto(ordenados: list[float], v: float) -> bool:
    """Existe nivel a menos de 0,5 de v? (busca binaria; lista ordenada)"""
    import bisect
    i = bisect.bisect_left(ordenados, v - 0.5)
    return i < len(ordenados) and ordenados[i] < v + 0.5


def verificar_anonimizacao(d: dict[str, Any], niveis_absolutos: Iterable[float] = (), *,
                           perto_restrito: bool = False) -> list[str]:
    """Lista de violacoes (vazia = anonimizado). ``perto_restrito`` (dossie v2, C29): a coincidencia com nivel
    absoluto so e checada nas ``SECOES_PERTO`` e fora das chaves isentas. O padrao (v1) fica identico ao de antes,
    equivalente ao verificador rapido do backtest."""
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
            elif _perto(niveis, v) and not (perto_restrito and _isento_perto(caminho)):
                erros.append(f"valor coincide com nivel absoluto em {caminho}")
    return erros


# referencia fixa: o v2 nunca passa pelo verificador rapido do backtest (que troca `verificar_anonimizacao`)
_verificar_anonimizacao_completa = verificar_anonimizacao


def unidade_feature(nome: str) -> str:
    """Unidade da feature no registro: ``pontos``, ``log``, ``relativa`` ou ``adimensional`` (``desconhecida``
    fora do registro). Usa o campo ``unidade`` da definicao quando existir; senao, a descricao do registro."""
    d = REGISTRO_PERMITIDO.get(nome)
    if d is None:
        return "desconhecida"
    u = getattr(d, "unidade", None)
    if isinstance(u, str) and u:
        return u
    desc = d.descricao.lower()
    if "logaritm" in desc or nome.startswith("har_rv"):
        return "log"
    if "relativ" in desc or "percent" in desc or nome.endswith("_rel"):
        return "relativa"
    if nome.endswith("_pts") or "em pontos" in desc:
        return "pontos"
    return "adimensional"


def _limpar(x: Any) -> Any:
    """Copia JSON das secoes extras: float nao finito vira ``None`` (dado indisponivel); floats com 6 casas."""
    if isinstance(x, dict):
        return {str(k): _limpar(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_limpar(v) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if x is None or isinstance(x, str):
        return x
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return _r(float(x), 6)
    raise ErroDossie(f"valor nao serializavel no dossie: {type(x).__name__}")


def _tem_numero(x: Any) -> bool:
    if isinstance(x, dict):
        return any(_tem_numero(v) for v in x.values())
    if isinstance(x, (list, tuple)):
        return any(_tem_numero(v) for v in x)
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def chaves_proibidas_v2(d: dict[str, Any]) -> list[str]:
    """Chaves que nunca entram no dossie do planejador: nomes de limite do usuario/conta e qualquer ``*_brl``."""
    return sorted({k for k in _chaves(d) if k.lower() in CHAVES_LIMITE_V2 or k.lower().endswith("_brl")})


def _janela_pts(ticks: Sequence[tuple[int, float]], t0_ns: int, j: int) -> float | None:
    p = np.array([x for t, x in ticks if t0_ns - j * 1_000_000_000 <= t <= t0_ns], dtype=float)
    return _r(_vol(p), 4) if _vol(p) is not None else None


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
    minutos_para_zeragem: int | None = None, tick_pontos: float,
    niveis_extras_absolutos: Iterable[float] = (), versao: int = 1, extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Monta o dossie anonimizado.

    ``ticks``: (t_ns, preco absoluto), ate ``t0_ns``. ``posicao`` (se aberta): {lado, quantidade,
    preco_medio, aberta_ns, stop_pontos, alvo_pontos}. ``noticias``: [{categoria, direcao, relevancia,
    intensidade, horizonte_min, t_disponivel_ns}] - o texto NUNCA entra.

    ``versao=1`` (padrao): identico ao de sempre. ``versao=2`` (braco planejador): sem ``limites`` (nenhum valor
    em dinheiro nem nome de limite), ``movimento.vol_pts_60s``/``vol_pts_300s`` em pontos, features sem unidade
    logaritmica ou relativa (C30), as secoes de ``extras`` (``SECOES_V2``; ``sessao`` e mesclada, ``posicao``
    substitui a basica) e `_perto` restrito por caminho (C29).
    """
    if versao not in (1, 2):
        raise ErroDossie(f"versao de dossie desconhecida: {versao}")
    if versao == 1 and extras:
        raise ErroDossie("extras so existem no dossie v2")
    permitidas = frozenset(features_permitidas) if features_permitidas is not None else FEATURES_PERMITIDAS_PADRAO
    feats: dict[str, float] = {}
    for nome, val in (features or {}).items():
        if nome not in permitidas:
            raise ErroDossie(f"feature fora da lista permitida: {nome}")
        if any(t in nome.lower() for t in _TERMOS_INDICADOR):
            raise ErroDossie(f"feature parece indicador tecnico tradicional: {nome}")
        if versao == 2 and unidade_feature(nome) in UNIDADES_PROIBIDAS_V2 + ("desconhecida",):
            raise ErroDossie(f"feature com unidade {unidade_feature(nome)} fora do dossie v2: {nome}")
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
        item = {
            "acao": str(m.get("acao", "")), "resultado_pontos": _r(m.get("resultado_pontos", 0.0)),
            "duracao_s": int(m.get("duracao_s", 0)), "motivo_saida": str(m.get("motivo_saida", "")),
        }
        if m.get("reflexao"):  # licao escrita DEPOIS do resultado liquidado (ia.reflexao); ja sanitizada e anonimizada
            item["reflexao"] = str(m["reflexao"])
        d["memoria"].append(item)
    niveis = [p for _, p in ticks] + list(niveis_extras_absolutos)
    if posicao and posicao.get("preco_medio"):
        niveis.append(float(posicao["preco_medio"]))
    if versao == 2:
        return _completar_v2(d, ticks=ticks, t0_ns=t0_ns, limites=limites, extras=extras or {}, niveis=niveis)
    erros = verificar_anonimizacao(d, niveis)
    if erros:
        raise ErroDossie("dossie nao anonimizado: " + "; ".join(erros))
    return d


def _completar_v2(d: dict[str, Any], *, ticks: Sequence[tuple[int, float]], t0_ns: int,
                  limites: Mapping[str, float] | None, extras: Mapping[str, Any], niveis: list[float]) -> dict[str, Any]:
    if limites:
        raise ErroDossie("dossie v2 nao leva limites: so o envelope derivado (pontos, unidades, segundos)")
    fora = sorted(set(extras) - set(SECOES_V2))
    if fora:
        raise ErroDossie(f"secao extra desconhecida no dossie v2: {fora}")
    del d["limites"]
    d["meta"]["versao_dossie"] = 2
    d["movimento"]["vol_pts_60s"] = _janela_pts(ticks, t0_ns, 60)
    d["movimento"]["vol_pts_300s"] = _janela_pts(ticks, t0_ns, 300)
    for nome in SECOES_V2:
        if nome not in extras:
            continue
        sec = _limpar(extras[nome])
        if nome == "sessao" and isinstance(sec, dict):
            d["sessao"] = {**d["sessao"], **sec}
        else:
            d[nome] = sec
    if _tem_numero(d.get("retorno")):
        raise ErroDossie("secao retorno do dossie v2 leva so codigos, sem numeros")
    proib = chaves_proibidas_v2(d)
    if proib:
        raise ErroDossie(f"chave de limite/dinheiro no dossie v2: {proib}")
    erros = _verificar_anonimizacao_completa(d, niveis, perto_restrito=True)
    if erros:
        raise ErroDossie("dossie nao anonimizado: " + "; ".join(erros))
    return d
