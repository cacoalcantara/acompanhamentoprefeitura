"""Relatorio de avaliacao em portugues (dict + Markdown). Recusa o selo 'validado' em dados sinteticos ou
contaminados e avisa que acerto alto nao implica expectativa positiva."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from robowin.core.tipos import Trade

from . import comparacao as comp
from . import estatistica as est
from . import impostos, metricas
from .metricas import dia_pregao

CUSTOS_BASE_BRL = 0.8  # custo ida e volta por contrato (tarifas+corretagem+slippage), cenario-base
CUSTOS_ESTRESSE_BRL = 2.4  # idem, cenario de estresse


def stop_efetivo_padrao_pontos(ponto_valor_brl: float = 0.20) -> float:
    """Stop efetivo pela politica de risco padrao: min(stop_max, perda_max_op/pv - folga) = min(300, 60/0.2 - 50)."""
    from robowin.core.tipos import ConfigRisco
    from robowin.risco.politica import stop_maximo_efetivo_pontos
    return stop_maximo_efetivo_pontos(ConfigRisco(), ponto_valor_brl)


def texto_aviso_acerto(stop_pontos: float | None = None, alvo_pontos: float = 100.0, ponto_valor_brl: float = 0.20,
                       custos_brl: tuple[float, float] = (CUSTOS_BASE_BRL, CUSTOS_ESTRESSE_BRL)) -> str:
    """Aviso sobre acerto, com o acerto de equilibrio calculado dos parametros EFETIVOS (nada fixo no texto)."""
    stop = stop_efetivo_padrao_pontos(ponto_valor_brl) if stop_pontos is None else stop_pontos
    lo = est.acerto_equilibrio(stop, alvo_pontos, custos_brl[0], ponto_valor_brl)
    hi = est.acerto_equilibrio(stop, alvo_pontos, custos_brl[1], ponto_valor_brl)
    return (
        f"Taxa de acerto alta NAO implica expectativa positiva. Com stop de {stop:.0f} pontos e alvo de "
        f"{alvo_pontos:.0f} pontos, o acerto de equilibrio e de {100 * lo:.0f}% a {100 * hi:.0f}% "
        f"(custos de R$ {custos_brl[0]:.2f} a R$ {custos_brl[1]:.2f} por operacao) e uma geometria assimetrica "
        "faz entradas aleatorias acertarem muito antes dos custos. A metrica primaria e a expectativa liquida "
        "com IC; o acerto deve ser lido sempre ao lado do controle aleatorio."
    )


AVISO_ACERTO = texto_aviso_acerto()
AVISO_SIMULACAO = "SIMULACAO - sem ordens reais. Resultados simulados nao garantem resultado real."
FL_MINIMO = 1.2
DSR_MINIMO = 0.95
MIN_PREGOES = 20  # menos pregoes que isso: o bootstrap por pregao e o DSR diario nao sustentam o selo


def decidir_selo(*, sintetico: bool | None, contaminado: bool | None, n: int, n_alvo: int | None, exp_ic_inf: float,
                 fator_lucro: float, dsr: float, pregoes: int = 0, tentativas: int = 1,
                 var_sr_tentativas: float | None = None) -> dict:
    """Selo 'validado' so se TODOS os criterios passam. Sintetico/contaminado DESCONHECIDO (None) recusa."""
    motivos = []
    if sintetico is None:
        motivos.append("origem dos dados desconhecida (nao afirmada como nao sintetica)")
    elif sintetico:
        motivos.append("dados sinteticos nao podem validar uma estrategia")
    if contaminado is None:
        motivos.append("contaminacao desconhecida (periodo nao afirmado como posterior ao corte dos LLMs)")
    elif contaminado:
        motivos.append("dados contaminados (periodo anterior ao corte de conhecimento dos LLMs)")
    if motivos:
        return {"selo": "recusado", "motivos": motivos}
    if n_alvo is None:
        motivos.append("n pre-registrado nao informado")
    elif n < n_alvo:
        motivos.append(f"amostra insuficiente (n={n} < n pre-registrado={n_alvo})")
    if pregoes < MIN_PREGOES:
        motivos.append(f"poucos pregoes ({pregoes} < {MIN_PREGOES})")
    if tentativas < 1:
        motivos.append("numero de tentativas invalido")
    elif tentativas > 1 and var_sr_tentativas is None:
        motivos.append("variancia do SR entre as tentativas nao informada (DSR sem deflator confiavel)")
    if not (exp_ic_inf > 0):
        motivos.append("IC inferior da expectativa nao e positivo")
    if not (fator_lucro > FL_MINIMO):
        motivos.append(f"fator de lucro nao excede {FL_MINIMO}")
    if not (dsr > DSR_MINIMO):
        motivos.append(f"DSR nao excede {DSR_MINIMO}")
    return {"selo": "nao_validado" if motivos else "validado", "motivos": motivos}


def _relatorio_braco(nome: str, trades: Sequence[Trade], *, n_alvo: int | None, tentativas: int,
                     sintetico: bool | None, contaminado: bool | None, cfg_ir: impostos.ConfigImpostos | None,
                     n_boot: int, seed: int, var_sr_tentativas: float | None = None) -> dict:
    r = metricas.resumo(trades)
    x = [t.liquido_brl for t in metricas.ordenar(trades)]
    dias = [dia_pregao(t) for t in metricas.ordenar(trades)]
    wil = est.ic_wilson(r.acertos, r.n)
    cp = est.ic_clopper_pearson(r.acertos, r.n)
    if r.n:
        bs = est.bootstrap_por_pregao(x, dias, n_boot=n_boot, seed=seed)
        bs.pop("replicas_expectativa")
        pnl_dia = list(metricas.pnl_por_dia(trades).values())
        dsr = est.deflated_sharpe(pnl_dia, tentativas, var_sr_tentativas)
    else:
        bs = {"expectativa_ic": (math.nan, math.nan), "fator_lucro_ic": (math.nan, math.nan), "pregoes": 0}
        dsr = {"sr": math.nan, "sr0": math.nan, "dsr": math.nan, "n": 0, "n_tentativas": tentativas}
    selo = decidir_selo(sintetico=sintetico, contaminado=contaminado, n=r.n, n_alvo=n_alvo,
                        exp_ic_inf=bs["expectativa_ic"][0], fator_lucro=r.fator_lucro,
                        dsr=dsr["dsr"] if dsr["dsr"] == dsr["dsr"] else 0.0, pregoes=int(bs.get("pregoes", 0)),
                        tentativas=tentativas, var_sr_tentativas=var_sr_tentativas)
    return {
        "braco": nome, "metricas": r.como_dict(),
        "acerto": {"n": r.n, "acertos": r.acertos, "taxa": r.taxa_acerto, "ic_wilson_95": wil,
                   "ic_clopper_pearson_95": cp},
        "bootstrap_pregao": bs, "dsr": dsr, "distribuicao_diaria": metricas.distribuicao_diaria(trades),
        "impostos": [a.como_dict() for a in impostos.apurar(trades, cfg_ir)],
        "resultado_apos_ir": impostos.resultado_apos_ir(trades, cfg_ir),
        "amostra": {"n": r.n, "n_alvo": n_alvo,
                    "suficiente": None if n_alvo is None else not comp.amostra_insuficiente(r.n, n_alvo)},
        "selo": selo,
    }


def gerar_relatorio(trades_por_braco: Mapping[str, Sequence[Trade]], *, tentativas: int,
                    controle: str | None = None, sintetico: bool | None = None, contaminado: bool | None = None,
                    n_alvo: int | None = None, var_sr_tentativas: float | None = None,
                    stop_pontos: float | None = None, alvo_pontos: float = 100.0,
                    cfg_impostos: impostos.ConfigImpostos | None = None,
                    n_boot: int = 2000, seed: int = 0, titulo: str = "Relatorio de avaliacao",
                    ids_vetados: Mapping[str, set[str]] | None = None,
                    n_preregistrado_comparacoes: int | None = None) -> dict[str, Any]:
    bracos = {nome: _relatorio_braco(nome, ts, n_alvo=n_alvo, tentativas=tentativas, sintetico=sintetico,
                                     contaminado=contaminado, cfg_ir=cfg_impostos, n_boot=n_boot, seed=seed,
                                     var_sr_tentativas=var_sr_tentativas)
              for nome, ts in trades_por_braco.items()}
    comparacoes: dict[str, Any] = {}
    if controle and controle in trades_por_braco:
        c = bracos[controle]["acerto"]
        for nome, b in bracos.items():
            if nome == controle:
                continue
            comparacoes[nome] = comp.comparar_com_controle(
                b["acerto"]["acertos"], b["acerto"]["n"], c["acertos"], c["n"],
                n_preregistrado=n_preregistrado_comparacoes if n_preregistrado_comparacoes is not None else n_alvo)
        ps = {n: v["p_valor_braco_maior"] for n, v in comparacoes.items() if "p_valor_braco_maior" in v}
        for n, h in comp.holm(ps).items():
            comparacoes[n]["holm"] = h
    vetos = {}
    for nome, ids in (ids_vetados or {}).items():
        if nome in trades_por_braco:
            vetos[nome] = comp.valor_do_veto(trades_por_braco[nome], ids, n_boot=n_boot, seed=seed)
    todos = [t for ts in trades_por_braco.values() for t in ts]
    dias = sorted({dia_pregao(t) for t in todos})
    return {
        "titulo": titulo, "aviso_simulacao": AVISO_SIMULACAO,
        "aviso_acerto": texto_aviso_acerto(stop_pontos, alvo_pontos),
        "periodo": {"inicio": dias[0] if dias else None, "fim": dias[-1] if dias else None, "pregoes": len(dias)},
        "dados": {"sintetico": sintetico, "contaminado": contaminado}, "controle": controle,
        "tentativas": tentativas, "var_sr_tentativas": var_sr_tentativas, "bracos": bracos, "comparacoes_com_controle": comparacoes,
        "valor_do_veto": vetos,
    }


# ---------------------------------------------------------------- Markdown
def _f(v: Any, casas: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if math.isnan(v):
            return "n/d"
        if math.isinf(v):
            return "infinito"
        return f"{v:.{casas}f}".replace(".", ",")
    return str(v)


def _pct(v: float) -> str:
    return "n/d" if v != v else f"{100 * v:.1f}%".replace(".", ",")


def _ic(par: Sequence[float], casas: int = 2) -> str:
    return f"[{_f(float(par[0]), casas)}; {_f(float(par[1]), casas)}]"


def renderizar_markdown(rel: Mapping[str, Any]) -> str:
    L: list[str] = [f"# {rel['titulo']}", "", f"> {rel['aviso_simulacao']}", ""]
    if rel["dados"]["sintetico"]:
        L += ["> **DADOS SINTETICOS** - resultados nao validam a estrategia.", ""]
    if rel["dados"]["sintetico"] is None or rel["dados"]["contaminado"] is None:
        L += ["> **ORIGEM/CONTAMINACAO DOS DADOS NAO AFIRMADA** - o selo 'validado' fica recusado.", ""]
    if rel["dados"]["contaminado"]:
        L += ["> **DADOS CONTAMINADOS** - periodo anterior ao corte de conhecimento dos LLMs.", ""]
    p = rel["periodo"]
    L += [f"Periodo: {p['inicio'] or 'n/d'} a {p['fim'] or 'n/d'} ({p['pregoes']} pregoes). "
          f"Tentativas contadas no DSR: {rel['tentativas']}.", "", f"**Aviso:** {rel['aviso_acerto']}", ""]
    ctrl = rel.get("controle")
    for nome, b in rel["bracos"].items():
        m, a, bs, d = b["metricas"], b["acerto"], b["bootstrap_pregao"], b["dsr"]
        L += [f"## Braco {nome}" + (" (controle aleatorio)" if nome == ctrl else ""), "",
              f"- Selo: **{b['selo']['selo']}**" + (f" - {'; '.join(b['selo']['motivos'])}" if b["selo"]["motivos"] else ""),
              f"- n = {m['n']} operacoes em {m['dias']} pregoes",
              f"- Taxa de acerto (liquido > 0): {_pct(a['taxa'])}; IC de Wilson 95%: "
              f"[{_pct(a['ic_wilson_95'][0])}; {_pct(a['ic_wilson_95'][1])}]",
              f"- Lucro liquido: R$ {_f(m['lucro_liquido_brl'])}; apos IR estimado: R$ "
              f"{_f(b['resultado_apos_ir']['depois_ir_brl'])}",
              f"- Expectativa por operacao: R$ {_f(m['expectativa_brl'])}; IC 95% (bootstrap por pregao): "
              f"{_ic(bs['expectativa_ic'])}",
              f"- Fator de lucro: {_f(m['fator_lucro'])}; IC 95%: {_ic(bs['fator_lucro_ic'])}",
              f"- Ganho medio R$ {_f(m['ganho_medio_brl'])} / perda media R$ {_f(m['perda_media_brl'])}",
              f"- Drawdown maximo: R$ {_f(m['drawdown_max_brl'])} ({m['drawdown_duracao_operacoes']} operacoes, "
              f"{_f(m['drawdown_duracao_dias'], 1)} dias)",
              f"- Slippage medio: R$ {_f(m['slippage_medio_brl'])}; custos (tarifas+corretagem): R$ {_f(m['custos_brl'])}",
              f"- DSR: {_f(d['dsr'], 3)} (SR {_f(d['sr'], 3)}, SR0 {_f(d['sr0'], 3)}, tentativas {d['n_tentativas']})"]
        am = b["amostra"]
        if am["suficiente"] is False:
            L.append(f"- **Amostra insuficiente**: n={am['n']} < n pre-registrado={am['n_alvo']}; sem conclusao.")
        if nome in rel["comparacoes_com_controle"]:
            c = rel["comparacoes_com_controle"][nome]
            if "p_controle" in c:
                L.append(f"- Controle aleatorio: {_pct(c['p_controle'])} (IC de Wilson "
                         f"[{_pct(c['ic_wilson_controle'][0])}; {_pct(c['ic_wilson_controle'][1])}]); "
                         f"p-valor (braco > controle): {_f(c['p_valor_braco_maior'], 4)}"
                         + (f"; ajustado por Holm: {_f(c['holm']['p_ajustado'], 4)}" if "holm" in c else ""))
        if nome in rel["valor_do_veto"]:
            v = rel["valor_do_veto"][nome]
            if "valor_veto_total_brl" in v:
                L.append(f"- Valor do veto do revisor: R$ {_f(v['valor_veto_total_brl'])} em "
                         f"{v['n_rejeitadas']} operacoes rejeitadas (expectativa das rejeitadas R$ "
                         f"{_f(v['expectativa_rejeitadas_brl'])})")
        if b["impostos"]:
            L += ["", "| Mes | Resultado (R$) | IRRF (R$) | Base (R$) | DARF 6015 (R$) | Vencimento |",
                  "|---|---|---|---|---|---|"]
            for i in b["impostos"]:
                L.append(f"| {i['mes']} | {_f(i['resultado_liquido_brl'])} | {_f(i['irrf_brl'])} | "
                         f"{_f(i['base_calculo_brl'])} | {_f(i['darf_brl'])} | {i['vencimento']} |")
        L.append("")
    return "\n".join(L)


def para_json_seguro(obj: Any) -> Any:
    """Converte NaN/inf em None e numpy/tuplas em tipos JSON."""
    if isinstance(obj, dict):
        return {str(k): para_json_seguro(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [para_json_seguro(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
