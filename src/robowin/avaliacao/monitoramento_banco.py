"""Secao "Monitoramento" lida do banco: deriva de features (PSI), custo de execucao real x modelo, MinTRL e VaR/ES.

*** Tudo aqui e INFORMATIVO e NAO altera o veto de risco. *** Quem veta e o ``MotorRiscoPadrao`` com os limites
fixos de ``config/risco.toml``. Este modulo so LE o banco (e, no ``robowin avaliar``, grava um incidente de aviso
``deriva_features`` quando o PSI passa do limite alto); nada daqui reduz/aumenta posicao nem bloqueia entrada.
Usado pelo ``robowin avaliar`` (relatorio Markdown/JSON) e pelo painel (pagina de Desempenho).

* **PSI por feature** (``monitoramento.psi_amostras``): referencia = janela de treino do modelo ativo (``modelos.
  treinado_ate_ns``) quando existir dado dos dois lados do corte; senao, as PRIMEIRAS sessoes contra as ULTIMAS
  (metade/metade). Limiares: PSI < 0,10 estavel; 0,10 a 0,25 aviso; >= 0,25 alerta (revalidar o modelo com novo
  OOS; nunca reajustar parametro sozinho). As features vem do proprio banco: variacao do preco de referencia entre
  decisoes e as saidas numericas gravadas na proposta de cada braco (ex.: E[R$] e P(stop) do modelo quant); com
  ``--ticks`` o ``avaliar`` acrescenta o vetor do ``PipelineFeatures`` recalculado do armazem de ticks.
* **Custo de execucao real x modelo**: entradas A MERCADO; custo real = lado x (preco executado - preco de
  referencia da intencao) em ticks; esperado = metade do slippage-base do modelo de custos do relatorio (o modelo
  cobra ``SLIPPAGE_BASE_TICKS`` por operacao ida e volta). Entradas de plano condicional ficam de fora (a
  referencia e a do armar do plano, nao a do disparo).
* **MinTRL** por braco (Bailey & Lopez de Prado), por OPERACAO: quantas operacoes seriam necessarias para o
  Sharpe observado ser significativo (> 0 com 95%); "faltam N operacoes".
* **VaR/ES diario historico** (``risco.cauda``): so descreve a cauda passada.

Amostra pequena => "amostra insuficiente" (nunca um numero inventado).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from scipy import stats

from robowin.core.esquema_banco import DDL
from robowin.risco import cauda
from robowin.risco.tempo import dia_sp

from . import monitoramento as mon
from .relatorio import SLIPPAGE_BASE_TICKS, SLIPPAGE_ESTRESSE_TICKS
from .robustez import min_track_record_length

ROTULO = "informativo; nao altera o veto de risco"
ORIGEM_INCIDENTE_DERIVA = "deriva_features"
SEVERIDADE_INCIDENTE_DERIVA = "aviso"
PSI_N_BINS = 10
MIN_SESSOES_PSI = 2  # precisa de ao menos uma sessao de referencia e uma atual
MIN_AMOSTRA_ATUAL_PSI = 20  # observacoes da janela atual por feature (a referencia exige n_bins x 5)
MIN_EXECUCOES_CUSTO = 10
MIN_OPERACOES_MINTRL = 30
CONFIANCA_MINTRL = 0.95
PASSO_FEATURES_TICKS_S = 60  # amostragem do vetor de features recalculado do armazem de ticks
NS = 1_000_000_000


# ------------------------------------------------------------------------------------------- leitura ----
def _consultar(banco: str | Path, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    p = Path(banco)
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return list(con.execute(sql, args))
    except sqlite3.Error:  # tabela ausente ou banco de outra versao: monitoramento indisponivel, nunca quebra
        return []
    finally:
        con.close()


def _json(s: Any) -> Any:
    try:
        return json.loads(s) if s else None
    except (TypeError, ValueError):
        return None


def _numero(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def ler_decisoes(banco: str | Path) -> list[dict[str, Any]]:
    """Decisoes com intencao/proposta decodificadas (ordem cronologica)."""
    out = []
    for t_ns, braco, modo, resultado, it, pr in _consultar(
            banco, "SELECT t_ns, braco, modo, resultado, intencao_json, proposta_json FROM decisoes ORDER BY t_ns, id"):
        if t_ns is None:
            continue
        it_d, pr_d = _json(it), _json(pr)
        out.append({"t_ns": int(t_ns), "braco": braco or "?", "modo": modo or "", "resultado": resultado or "",
                    "intencao": it_d if isinstance(it_d, dict) else {},
                    "proposta": pr_d if isinstance(pr_d, dict) else {}})
    return out


def corte_de_treino_ns(banco: str | Path) -> int | None:
    """``treinado_ate_ns`` mais recente entre os modelos ativos (None se nao houver)."""
    r = _consultar(banco, "SELECT MAX(treinado_ate_ns) FROM modelos WHERE ativo=1 AND treinado_ate_ns > 0")
    return int(r[0][0]) if r and r[0][0] else None


# --------------------------------------------------------------------------------------------- PSI ----
Serie = list[tuple[int, float]]


def series_de_decisoes(decisoes: Sequence[Mapping[str, Any]]) -> dict[str, Serie]:
    """Features observaveis no banco: variacao do preco de referencia entre ciclos de decisao (em PONTOS
    do ativo: multiplos exatos do tick, sem depender do nivel de preco, o que manteria os valores discretos
    alinhados aos bins da referencia) e cada saida numerica da proposta, por braco (``<braco>.<campo>``)."""
    series: dict[str, Serie] = {}
    precos: dict[int, float] = {}
    for d in decisoes:
        p = _numero((d.get("intencao") or {}).get("preco_referencia"))
        if p is not None and p > 0:
            precos.setdefault(int(d["t_ns"]), p)
        for k, v in sorted((d.get("proposta") or {}).items()):
            x = _numero(v)
            if x is not None:
                series.setdefault(f"{d['braco']}.{k}", []).append((int(d["t_ns"]), x))
    anterior: tuple[int, float] | None = None
    for t in sorted(precos):
        p = precos[t]
        if anterior is not None and dia_sp(anterior[0]) == dia_sp(t):  # nunca atravessa a noite
            var = p - anterior[1]
            series.setdefault("variacao_preco_referencia_pts", []).append((t, var))
            series.setdefault("variacao_abs_preco_referencia_pts", []).append((t, abs(var)))
        anterior = (t, p)
    return series


def series_de_ticks(dir_ticks: str | Path, simbolo: str, passo_s: int = PASSO_FEATURES_TICKS_S) -> dict[str, Serie]:
    """Vetor do ``PipelineFeatures`` recalculado (causal) do armazem de ticks, amostrado a cada ``passo_s``."""
    from robowin.dados.ticks import ArmazenamentoTicks
    from robowin.features.pipeline import AQUECIMENTO_PADRAO_NS, calcular_offline

    raiz = Path(dir_ticks) / "ticks" / simbolo
    if not raiz.is_dir():
        return {}
    arm = ArmazenamentoTicks(dir_ticks)
    series: dict[str, Serie] = {}
    for pasta in sorted(raiz.glob("data=*")):
        try:
            dia = dt.date.fromisoformat(pasta.name.split("=", 1)[1])
        except ValueError:
            continue
        ticks = arm.ler_dia(simbolo, dia)
        if len(ticks) < 2:
            continue
        ini, fim = ticks[0].t_utc_ns + AQUECIMENTO_PADRAO_NS, ticks[-1].t_utc_ns
        if fim <= ini:
            continue
        livros = arm.ler_livros(simbolo, ticks[0].t_utc_ns, fim + 1)
        for v in calcular_offline(ticks, livros, range(ini, fim + 1, passo_s * NS)):
            for k, x in v.valores.items():
                f = _numero(x)
                if f is not None:
                    series.setdefault(k, []).append((v.t_ns, f))
    return series


def deriva_features(series: Mapping[str, Serie], corte_treino_ns: int | None = None,
                    n_bins: int = PSI_N_BINS) -> dict[str, Any]:
    """PSI por feature entre a janela de referencia e a atual (ver docstring do modulo)."""
    base: dict[str, Any] = {"limiares": {"aviso": mon.PSI_AVISO, "alerta": mon.PSI_ALERTA}, "rotulo": ROTULO,
                            "psi": {}, "niveis": {}, "pior": "indisponivel", "omitidas": []}
    dias = sorted({dia_sp(t) for s in series.values() for t, _ in s})
    if corte_treino_ns is not None and any(t <= corte_treino_ns for s in series.values() for t, _ in s) \
            and any(t > corte_treino_ns for s in series.values() for t, _ in s):
        def eh_ref(t: int) -> bool:
            return t <= corte_treino_ns
        criterio = f"janela de treino do modelo ativo (ate {dia_sp(corte_treino_ns).isoformat()}) x depois dela"
    elif len(dias) >= MIN_SESSOES_PSI:
        ref_dias = set(dias[: len(dias) // 2])

        def eh_ref(t: int) -> bool:
            return dia_sp(t) in ref_dias
        criterio = (f"primeiras {len(ref_dias)} sessoes x ultimas {len(dias) - len(ref_dias)} sessoes "
                    f"(sem janela de treino registrada)")
    else:
        return {**base, "status": "amostra insuficiente",
                "motivo": f"{len(dias)} sessao(oes) com dados; o PSI exige ao menos {MIN_SESSOES_PSI}"}
    ref: dict[str, list[float]] = {}
    atual: dict[str, list[float]] = {}
    pequenas: list[str] = []
    for nome in sorted(series):
        r = [v for t, v in series[nome] if eh_ref(t)]
        a = [v for t, v in series[nome] if not eh_ref(t)]
        if len(set(r)) < 2:  # constante (parametro gravado na proposta) ou ausente na referencia: sem distribuicao
            base["omitidas"].append(nome)
            continue
        if len(r) < n_bins * 5 or len(a) < MIN_AMOSTRA_ATUAL_PSI:
            pequenas.append(nome)
            continue
        ref[nome], atual[nome] = r, a
    d = mon.deriva_de_features(ref, atual, n_bins)
    out = {**base, "criterio": criterio, "psi": d["psi"], "niveis": d["niveis"], "pior": d["pior"],
           "amostra_insuficiente": pequenas,
           "n": {k: {"referencia": len(ref[k]), "atual": len(atual[k])} for k in ref}}
    validos = [v for v in d["psi"].values() if math.isfinite(v)]
    out["status"] = "ok" if validos else "amostra insuficiente"
    if not validos:
        out["motivo"] = "nenhuma feature com amostra suficiente nas duas janelas"
    out["features_em_alerta"] = sorted(k for k, n in d["niveis"].items() if n == "alerta")
    return out


def registrar_incidente_deriva(banco: str | Path, psi: Mapping[str, Any], t_ns: int | None = None) -> bool:
    """PSI >= limite alto => incidente ``deriva_features`` (severidade aviso). NAO bloqueia nada.

    Idempotente: o mesmo detalhe nao e gravado duas vezes (rodar o ``avaliar`` de novo nao duplica)."""
    alertas = list(psi.get("features_em_alerta") or [])
    if not alertas:
        return False
    vals = psi.get("psi") or {}
    lista = ", ".join(f"{k}={vals[k]:.3f}" for k in alertas[:12]) + (" ..." if len(alertas) > 12 else "")
    detalhe = (f"PSI >= {mon.PSI_ALERTA:.2f} em {len(alertas)} feature(s): {lista}; criterio: "
               f"{psi.get('criterio', '?')}. Revalidar o modelo (novo OOS); {ROTULO}.")[:900]
    con = sqlite3.connect(Path(banco))
    try:
        for d in DDL:
            con.execute(d)
        if con.execute("SELECT 1 FROM incidentes WHERE origem=? AND detalhe=? LIMIT 1",
                       (ORIGEM_INCIDENTE_DERIVA, detalhe)).fetchone():
            return False
        con.execute("INSERT INTO incidentes(t_ns, origem, severidade, detalhe) VALUES (?,?,?,?)",
                    (t_ns if t_ns is not None else time.time_ns(), ORIGEM_INCIDENTE_DERIVA,
                     SEVERIDADE_INCIDENTE_DERIVA, detalhe))
        con.commit()
        return True
    finally:
        con.close()


# ---------------------------------------------------------------------------------- custo de execucao ----
_SQL_EXEC = ("SELECT o.id, o.braco, o.lado, o.intencao_id, o.simulada, e.preco, e.t_ns FROM ordens o "
             "JOIN execucoes e ON e.ordem_id = o.id WHERE o.tipo = 'mercado' ORDER BY e.t_ns, e.id")


def custo_execucao(banco: str | Path, decisoes: Sequence[Mapping[str, Any]], tick_pontos: float | None,
                   esperado_ticks: float = SLIPPAGE_BASE_TICKS / 2) -> dict[str, Any]:
    """Custo real das entradas a mercado (ticks, por execucao) contra o esperado pelo modelo de custos."""
    out: dict[str, Any] = {"rotulo": ROTULO, "esperado_ticks": esperado_ticks,
                           "esperado_estresse_ticks": SLIPPAGE_ESTRESSE_TICKS / 2,
                           "modelo": (f"modelo de custos do relatorio: {SLIPPAGE_BASE_TICKS:g} tick(s) de slippage por "
                                      "operacao ida e volta (metade por execucao)")}
    if not tick_pontos or tick_pontos <= 0:
        return {**out, "status": "indisponivel", "motivo": "tamanho do tick do ativo desconhecido", "n": 0}
    refs: dict[str, float] = {}
    for d in decisoes:
        it = d.get("intencao") or {}
        p = _numero(it.get("preco_referencia"))
        if it.get("id") and p is not None and p > 0 and str(it.get("modo", d.get("modo"))) != "condicional":
            refs.setdefault(str(it["id"]), p)
    rast = mon.RastreadorSlippage(janela=1_000_000, min_execucoes=MIN_EXECUCOES_CUSTO)
    vistos: set[str] = set()
    por_braco: dict[str, list[float]] = {}
    simuladas = {0: 0, 1: 0}
    for oid, braco, lado, iid, sim, preco, _t in _consultar(banco, _SQL_EXEC):
        if oid in vistos or iid not in refs or preco is None or str(lado).lower() not in ("compra", "venda"):
            continue
        vistos.add(oid)  # so a primeira execucao de cada ordem (parciais seguintes nao recontam a referencia)
        r = rast.registrar(1 if str(lado).lower() == "compra" else -1, float(preco), refs[iid], tick_pontos,
                           esperado_ticks)
        por_braco.setdefault(braco or "?", []).append(r.custo_real_ticks)
        simuladas[1 if sim else 0] += 1
    s = rast.resumo()
    n = int(s["n"])
    fonte = ("conta DEMO" if simuladas[1] == 0 and n else "simulador" if simuladas[0] == 0 else "misto")
    out.update(n=n, fonte=fonte if n else "-",
               por_braco={b: {"n": len(v), "real_ticks": float(np.mean(v))} for b, v in sorted(por_braco.items())})
    if n:
        out.update(real_ticks=s["real_ticks"], razao=s["razao"])
    if n < MIN_EXECUCOES_CUSTO:
        out.update(status="amostra insuficiente",
                   motivo=f"{n} entrada(s) a mercado com referencia; minimo {MIN_EXECUCOES_CUSTO}")
    else:
        out.update(status="ok", alerta=rast.alerta())
    return out


# ------------------------------------------------------------------------------------------- MinTRL ----
def mintrl_braco(liquidos: Sequence[float], confianca: float = CONFIANCA_MINTRL) -> dict[str, Any]:
    """MinTRL por operacao: n necessario para o Sharpe observado (por operacao) superar 0 com ``confianca``."""
    x = np.asarray([v for v in liquidos if v is not None and math.isfinite(v)], dtype=float)
    n = int(x.size)
    out: dict[str, Any] = {"n": n, "confianca": confianca}
    if n < MIN_OPERACOES_MINTRL:
        return {**out, "status": "amostra insuficiente", "motivo": f"n={n} < {MIN_OPERACOES_MINTRL} operacoes"}
    sd = float(x.std(ddof=1))
    if sd <= 0:
        return {**out, "status": "amostra insuficiente", "motivo": "resultado sem variacao"}
    sr = float(x.mean()) / sd
    assim = float(stats.skew(x, bias=False))
    curt = float(stats.kurtosis(x, fisher=False, bias=False))
    out.update(sharpe_operacao=sr, assimetria=assim, curtose=curt)
    if sr <= 0:
        return {**out, "status": "sem vantagem", "mintrl": math.inf, "faltam": None,
                "motivo": "Sharpe observado <= 0: nenhum tamanho de amostra o torna significativo"}
    m = min_track_record_length(sr, 0.0, assim, curt, confianca)
    if not math.isfinite(m):
        return {**out, "status": "sem vantagem", "mintrl": m, "faltam": None,
                "motivo": "momentos da distribuicao tornam o MinTRL infinito"}
    falta = max(0, math.ceil(m) - n)
    return {**out, "status": "ok", "mintrl": m, "faltam": falta, "significativo": falta == 0}


# ------------------------------------------------------------------------------------------- VaR/ES ----
def cauda_braco(pnl_diario: Sequence[float]) -> dict[str, Any]:
    rc = cauda.relatorio_cauda(list(pnl_diario))
    c = rc.como_dict()
    c["amostra_insuficiente"] = rc.dias < cauda.MIN_DIAS_VAR_95
    c["min_dias"] = cauda.MIN_DIAS_VAR_95
    return c


# --------------------------------------------------------------------------------------------- tudo ----
def _resultados_do_banco(banco: str | Path) -> tuple[dict[str, list[float]], dict[str, dict[str, float]]]:
    liq: dict[str, list[float]] = {}
    dia: dict[str, dict[str, float]] = {}
    for braco, v, d, t in _consultar(banco, "SELECT braco, liquido_brl, dia, t_saida_ns FROM trades "
                                            "ORDER BY t_saida_ns, id"):
        x = _numero(v)
        if x is None:
            continue
        liq.setdefault(braco, []).append(x)
        chave = d or (dia_sp(int(t)).isoformat() if t else "?")
        dia.setdefault(braco, {})[chave] = dia.get(braco, {}).get(chave, 0.0) + x
    return liq, dia


def montar_monitoramento(banco: str | Path, *, tick_pontos: float | None,
                         liquidos_por_braco: Mapping[str, Sequence[float]] | None = None,
                         pnl_diario_por_braco: Mapping[str, Sequence[float]] | None = None,
                         series_extra: Mapping[str, Serie] | None = None) -> dict[str, Any]:
    """Monta a secao inteira. Sem ``liquidos``/``pnl_diario`` explicitos, le a tabela ``trades``."""
    decisoes = ler_decisoes(banco)
    if liquidos_por_braco is None or pnl_diario_por_braco is None:
        liq, dia = _resultados_do_banco(banco)
        liquidos_por_braco = liquidos_por_braco if liquidos_por_braco is not None else liq
        pnl_diario_por_braco = (pnl_diario_por_braco if pnl_diario_por_braco is not None
                                else {b: list(v.values()) for b, v in dia.items()})
    series = series_de_decisoes(decisoes)
    for k, v in (series_extra or {}).items():
        series[f"tick.{k}"] = list(v)
    return {
        "rotulo": ROTULO,
        "psi": deriva_features(series, corte_de_treino_ns(banco)),
        "custo_execucao": custo_execucao(banco, decisoes, tick_pontos),
        "mintrl": {b: mintrl_braco(v) for b, v in sorted(liquidos_por_braco.items()) if len(v)},
        "cauda": {b: cauda_braco(v) for b, v in sorted(pnl_diario_por_braco.items()) if len(v)},
    }


# ------------------------------------------------------------------------------------------ Markdown ----
def _n(v: Any, casas: int = 2) -> str:
    if v is None:
        return "n/d"
    f = float(v)
    if math.isnan(f):
        return "n/d"
    if math.isinf(f):
        return "infinito"
    return f"{f:.{casas}f}".replace(".", ",")


def renderizar_markdown(m: Mapping[str, Any], pbo: Mapping[str, Any] | None = None, moeda: str = "BRL") -> str:
    L = ["", f"## Monitoramento ({ROTULO})", "",
         "> Metricas de observacao: nao sao veto, nao mudam limites e nao provam lucro. Quem veta e o motor de risco "
         "com os limites fixos aprovados.", ""]
    p = m.get("psi") or {}
    lim = p.get("limiares") or {"aviso": mon.PSI_AVISO, "alerta": mon.PSI_ALERTA}
    L += ["### Deriva de features (PSI)", "",
          f"Limiares: PSI < {_n(lim['aviso'])} estavel; {_n(lim['aviso'])} a {_n(lim['alerta'])} aviso; "
          f">= {_n(lim['alerta'])} alerta (revalidar o modelo com novo OOS; nunca reajustar sozinho).", ""]
    if p.get("status") != "ok":
        L.append(f"- **Amostra insuficiente**: {p.get('motivo', 'sem dados')}.")
    else:
        L += [f"Referencia x atual: {p['criterio']}. Pior nivel: **{p['pior']}**.", "",
              "| Feature | PSI | Nivel | n ref. | n atual |", "|---|---|---|---|---|"]
        for k, v in p["psi"].items():
            nn = (p.get("n") or {}).get(k, {})
            L.append(f"| {k} | {_n(v, 3)} | {p['niveis'][k]} | {nn.get('referencia', '-')} | {nn.get('atual', '-')} |")
        if p.get("amostra_insuficiente"):
            L.append(f"\n- Amostra insuficiente (fora da tabela): {', '.join(p['amostra_insuficiente'])}.")
        if p.get("features_em_alerta"):
            L.append(f"- Incidente `{ORIGEM_INCIDENTE_DERIVA}` (aviso) registrado no banco: "
                     f"{', '.join(p['features_em_alerta'])}. Nada foi bloqueado.")
    c = m.get("custo_execucao") or {}
    L += ["", "### Custo de execucao real x modelo (ticks por execucao)", ""]
    if c.get("status") == "indisponivel":
        L.append(f"- Indisponivel: {c.get('motivo')}.")
    else:
        pref = "**Amostra insuficiente**" if c.get("status") != "ok" else f"Fonte: {c.get('fonte')}"
        L.append(f"- {pref}: {c.get('n', 0)} entrada(s) a mercado; real medio {_n(c.get('real_ticks'))} ticks x "
                 f"esperado {_n(c.get('esperado_ticks'))} (estresse {_n(c.get('esperado_estresse_ticks'))}); "
                 f"razao {_n(c.get('razao'))}. {c.get('modelo', '')}.")
        for b, v in (c.get("por_braco") or {}).items():
            L.append(f"  - {b}: n={v['n']}, real medio {_n(v['real_ticks'])} ticks")
        if c.get("alerta"):
            L.append(f"- **Alerta (informativo)**: {c['alerta']}.")
    L += ["", "### MinTRL por braco (operacoes para o Sharpe observado ser significativo, 95%)", ""]
    for b, v in (m.get("mintrl") or {}).items():
        if v["status"] == "amostra insuficiente":
            L.append(f"- {b}: amostra insuficiente ({v['motivo']}).")
        elif v["status"] == "sem vantagem":
            L.append(f"- {b}: n={v['n']}; {v['motivo']}.")
        else:
            txt = ("ja significativo" if v["significativo"] else f"faltam {v['faltam']} operacoes")
            L.append(f"- {b}: n={v['n']}, Sharpe/operacao {_n(v['sharpe_operacao'], 3)}, MinTRL "
                     f"{_n(v['mintrl'], 0)} operacoes: {txt}.")
    if not m.get("mintrl"):
        L.append("- Sem operacoes.")
    L += ["", "### VaR/ES diario historico (cauda de perdas)", ""]
    for b, v in (m.get("cauda") or {}).items():
        txt = "; ".join(f"VaR {k} = R$ {_n(x['var_brl'])}, ES = R$ {_n(x['es_brl'])}" for k, x in v["niveis"].items())
        aviso = (f"**amostra insuficiente** ({v['dias']} dias < {v['min_dias']}; estimativa instavel): "
                 if v["amostra_insuficiente"] else f"{v['dias']} dias: ")
        L.append(f"- {b}: {aviso}{txt}; pior dia R$ -{_n(v['pior_dia_brl'])}.")
    if not m.get("cauda"):
        L.append("- Sem dias com resultado.")
    if pbo is not None:
        L += ["", "### Robustez da escolha do melhor braco (PBO)", ""]
        if pbo.get("pbo") is None:
            L.append(f"- PBO entre bracos: indisponivel ({pbo.get('motivo', 'amostra insuficiente')}).")
        else:
            L.append(f"- PBO entre bracos (CSCV, {pbo['n_combinacoes']} combinacoes, {pbo['n_bracos']} bracos, "
                     f"{pbo['n_periodos']} pregoes): {_n(pbo['pbo'])}. {pbo['aviso']}")
    L.append("")
    L.append("VaR/ES e PSI descrevem o passado observado; nao limitam nem preveem perdas futuras e nao sao veto.")
    texto = "\n".join(L) + "\n"
    return texto if moeda == "BRL" else texto.replace("R$", moeda)
