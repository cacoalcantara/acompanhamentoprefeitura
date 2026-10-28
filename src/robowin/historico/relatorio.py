"""Relatorios (Markdown + JSON) da simulacao historica: um por pregao e o consolidado.

O consolidado reaproveita ``avaliacao.relatorio`` (expectativa com IC por bootstrap por pregao, taxa de acerto
com IC de Wilson ao lado do controle aleatorio, Holm, drawdown, DSR e selo) e acrescenta o SELO HONESTO dos dados
reais (lado inferido, sem livro, amostra curta), a contaminacao por pregao, os bloqueios do risco, as latencias e
os tempos de execucao.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robowin.avaliacao import relatorio as aval
from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import ConfigRisco, Trade

from .runner import CONTROLE, SELO, ResultadoDia, ResultadoHistorico, quantis, salvar_json


def _brl(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


def _linha_bracos(resumo: Mapping[str, Mapping[str, Any]]) -> list[str]:
    L = ["| braco | modo | decisoes | entradas | vetadas (risco) | bloqueadas | trades | liquido | chamadas IA | custo IA (US$) |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for b, r in resumo.items():
        L.append(f"| {b} | {r['modo']} | {r['decisoes']} | {r['entradas']} | {r['vetadas']} | {r['bloqueadas']} | "
                 f"{r['trades']} | {_brl(r['liquido_brl'])} | {r['chamadas_ia']} | {r['custo_ia_usd']:.4f} |")
    return L


def _cabecalho(res: ResultadoHistorico) -> list[str]:
    rot_ia = ("FALSO - respostas e latencias INJETADAS; nao representam nenhum modelo real"
              if res.provedor == "falso" else "REAL (SDKs oficiais; custo e latencia medidos)")
    return [f"> **{SELO}**", "",
            f"> SIMULACAO - nenhuma ordem enviada. IA: **{rot_ia}** ({res.motivo_provedor}).", "",
            f"> Corte de conhecimento dos LLMs: **{res.corte}**. Pregao posterior ao corte = `limpo` (o modelo nao "
            "pode conhecer o dia); anterior ou igual = `contaminado`.", ""]


def markdown_dia(r: ResultadoDia, res: ResultadoHistorico) -> str:
    q = r.dados.get("qualidade") or {}
    L = [f"# Historico causal - {res.ativo} {r.data} ({r.simbolo})", "", *_cabecalho(res),
         f"- Contaminacao: **{r.contaminacao}**",
         f"- Dados: {r.dados['ticks_armazem']} ticks no armazem -> {r.dados['eventos_replay']} eventos no replay "
         f"(compactacao causal por janela de {r.dados['compactar_ms']:g} ms, fator {r.dados['fator_compactacao']})"]
    if q:
        L.append(f"- Atraso de entrega injetado: {q.get('atraso_entrega_ms')} ms apos o negocio (o replay ja roda no "
                 "instante de DISPONIBILIDADE; por isso `dado_para_gatilho` sai 0 abaixo)")
        L.append(f"- Qualidade B3: {q.get('linhas_do_dia')} linhas, {q.get('negocios_validos')} negocios validos, "
                 f"{q.get('negocios_cancelados')} cancelados, horario {q.get('primeiro')} -> {q.get('ultimo')}; "
                 + "; ".join(q.get("achados") or []))
    t = r.treino
    L.append(f"- Modelo quant: **{t.get('status')}** com {t.get('amostras', 0)} amostras de "
             f"{len(t.get('dias_treino') or [])} pregao(oes) anteriores ({', '.join(t.get('dias_treino') or []) or '-'})"
             + (f"; {t['motivo']}" if t.get("motivo") else ""))
    mtd = r.maquina_do_tempo
    L.append(f"- Maquina do Tempo: {mtd['sessoes_anteriores']} sessoes completas anteriores (minimo {mtd['minimo']}) => "
             + ("disponivel" if mtd["disponivel"] else "indisponivel (evidencia nao entregue ao revisor)"))
    L.append("- Tempos (s): " + ", ".join(f"{k} {v}" for k, v in r.tempos_s.items()))
    L += ["", *_linha_bracos(r.resumo), ""]
    if r.bloqueios:
        L += ["Bloqueios e vetos por braco:", ""]
        for b, c in r.bloqueios.items():
            L.append(f"- {b}: " + ", ".join(f"{k} x{v}" for k, v in sorted(c.items(), key=lambda x: -x[1])))
        L.append("")
    if r.latencias:
        L += ["| etapa | n | p50 (ms) | p95 (ms) | max (ms) |", "|---|---|---|---|---|"]
        for e, v in r.latencias.items():
            L.append(f"| {e} | {int(v.get('n', 0))} | {v.get('p50_ms')} | {v.get('p95_ms')} | {v.get('max_ms')} |")
    L += ["", f"Banco: `{r.banco}` (painel: `robowin painel --banco <arquivo>`)"]
    return "\n".join(L) + "\n"


def consolidar(res: ResultadoHistorico, perfil: PerfilInstrumento, risco: ConfigRisco | None = None) -> dict[str, Any]:
    bracos = sorted({b for r in res.dias for b in r.trades})
    trades: dict[str, list[Trade]] = {b: [t for r in res.dias for t in r.trades.get(b, [])] for b in bracos}
    contaminado: bool | None = any(r.contaminacao == "contaminado" for r in res.dias) if res.dias else None
    rel = aval.gerar_relatorio(trades, controle=CONTROLE if CONTROLE in trades else None, sintetico=False,
                               contaminado=contaminado, tentativas=max(1, len(bracos)), n_boot=res.params.n_boot,
                               seed=res.params.seed, perfil=perfil, risco=risco,
                               titulo=f"Historico causal {res.ativo} - consolidado ({len(res.dias)} pregoes)")
    soma: dict[str, Counter[str]] = {}
    tot: dict[str, Counter[str]] = {}
    for r in res.dias:
        for b, c in r.bloqueios.items():
            soma.setdefault(b, Counter()).update(c)
        for b, x in r.resumo.items():
            tot.setdefault(b, Counter()).update({k: x[k] for k in ("decisoes", "entradas", "vetadas", "bloqueadas",
                                                                    "trades", "chamadas_ia")})
            tot[b]["custo_ia_usd_x1e6"] += int(round(x["custo_ia_usd"] * 1e6))
    lats: dict[str, list[float]] = {}
    for r in res.dias:
        for e, v in r.latencias_brutas.items():
            lats.setdefault(e, []).extend(v)
    return {"relatorio": rel, "selo": SELO, "latencias": {e: quantis(v) for e, v in sorted(lats.items())}, "provedor": res.provedor, "motivo_provedor": res.motivo_provedor,
            "corte_llm": res.corte, "ativo": res.ativo,
            "pregoes": [{"data": r.data.isoformat(), "simbolo": r.simbolo, "contaminacao": r.contaminacao,
                         "eventos": r.dados["eventos_replay"], "ticks_armazem": r.dados["ticks_armazem"],
                         "modelo": r.treino.get("status"), "tempos_s": r.tempos_s,
                         "liquido_por_braco": {b: x["liquido_brl"] for b, x in r.resumo.items()}} for r in res.dias],
            "pulados": res.pulados, "bloqueios": {b: dict(c) for b, c in soma.items()},
            "totais_por_braco": {b: dict(c) for b, c in tot.items()}, "tempos_s": res.tempos_s,
            "limpos": sum(r.contaminacao == "limpo" for r in res.dias),
            "contaminados": sum(r.contaminacao == "contaminado" for r in res.dias)}


def markdown_consolidado(c: Mapping[str, Any], res: ResultadoHistorico) -> str:
    L = [f"# Historico causal {res.ativo} - consolidado", "", *_cabecalho(res)]
    n = len(c["pregoes"])
    L += [f"**{n} pregao(oes)**: {c['limpos']} limpo(s), {c['contaminados']} contaminado(s). "
          + ("**AMOSTRA INSUFICIENTE** para qualquer conclusao (menos de 20 pregoes e longe do n pre-registrado)."
             if n < aval.MIN_PREGOES else ""), ""]
    L += ["| pregao | contrato | contaminacao | ticks -> eventos | modelo | tempo replay (s) | " + " | ".join(
        sorted({b for p in c["pregoes"] for b in p["liquido_por_braco"]})) + " |"]
    nomes = sorted({b for p in c["pregoes"] for b in p["liquido_por_braco"]})
    L.append("|" + "---|" * (6 + len(nomes)))
    for p in c["pregoes"]:
        L.append(f"| {p['data']} | {p['simbolo']} | {p['contaminacao']} | {p['ticks_armazem']} -> {p['eventos']} | "
                 f"{p['modelo']} | {p['tempos_s'].get('replay')} | "
                 + " | ".join(_brl(p["liquido_por_braco"].get(b, 0.0)) for b in nomes) + " |")
    if c["pulados"]:
        L += ["", "Pregoes pulados: " + ", ".join(f"{d} ({m})" for d, m in c["pulados"].items())]
    L += ["", "## Operacoes, vetos do risco e bloqueios (soma dos pregoes)", "",
          "| braco | decisoes | entradas | vetadas (risco) | bloqueadas | trades | chamadas IA | custo IA (US$) |",
          "|---|---|---|---|---|---|---|---|"]
    for b, t in c["totais_por_braco"].items():
        L.append(f"| {b} | {t.get('decisoes', 0)} | {t.get('entradas', 0)} | {t.get('vetadas', 0)} | "
                 f"{t.get('bloqueadas', 0)} | {t.get('trades', 0)} | {t.get('chamadas_ia', 0)} | "
                 f"{t.get('custo_ia_usd_x1e6', 0) / 1e6:.4f} |")
    if c["bloqueios"]:
        L += ["", "Motivos (risco = veto do motor de risco; bloqueio = falha segura):", ""]
        for b, m in c["bloqueios"].items():
            L.append(f"- {b}: " + ", ".join(f"{k} x{v}" for k, v in sorted(m.items(), key=lambda x: -x[1])[:8]))
    if c.get("latencias"):
        L += ["", "## Latencias (todas as etapas, todos os pregoes; IA FALSA = injetada)", "",
              "| etapa | n | p50 (ms) | p95 (ms) | max (ms) |", "|---|---|---|---|---|"]
        for e, v in c["latencias"].items():
            L.append(f"| {e} | {int(v.get('n', 0))} | {v.get('p50_ms')} | {v.get('p95_ms')} | {v.get('max_ms')} |")
    pico = c["tempos_s"].get("memoria_pico_mb")
    L += ["", f"Tempo total: {c['tempos_s'].get('total')} s" + (f"; pico de memoria {pico} MB." if pico else "."),
          "", "---", ""]
    L.append(aval.renderizar_markdown(c["relatorio"]))
    return "\n".join(L) + "\n"


def escrever(res: ResultadoHistorico, perfil: PerfilInstrumento, risco: ConfigRisco | None = None) -> dict[str, Path]:
    """Grava saida/dias/<D>/relatorio.{md,json} e saida/consolidado.{md,json}. Devolve os caminhos principais."""
    saida = Path(res.params.saida)
    for r in res.dias:
        d = saida / "dias" / r.data.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        (d / "relatorio.md").write_text(markdown_dia(r, res), encoding="utf-8")
        salvar_json(d / "relatorio.json", r.como_dict())
    c = consolidar(res, perfil, risco)
    md = markdown_consolidado(c, res)
    (saida / "consolidado.md").write_text(md, encoding="utf-8")
    salvar_json(saida / "consolidado.json", c)
    return {"consolidado_md": saida / "consolidado.md", "consolidado_json": saida / "consolidado.json"}
