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
from robowin.core.tipos import ConfigRisco, PreRegistro, Trade

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
    falsos = aval.bracos_falsos(r.bloqueios, r.modelos_servidos, provedor_falso=res.provedor == "falso")
    injetados: dict[str, dict[str, int]] = {}
    if r.bloqueios:
        L += ["Bloqueios e vetos por braco:", ""]
        for b, c in r.bloqueios.items():
            reais = {k: v for k, v in c.items() if b not in falsos or not aval.eh_motivo_injetado(k)}
            inj = {k: v for k, v in c.items() if k not in reais}
            if inj:
                injetados[b] = inj
            if reais:
                L.append(f"- {b}: " + ", ".join(f"{k} x{v}" for k, v in sorted(reais.items(), key=lambda x: -x[1])))
        L.append("")
    if injetados:
        L += ["Injetado (FALSO; falhas sorteadas por seed, fora de qualquer comparacao):", ""]
        L += [f"- {b}: " + ", ".join(f"{k} x{v}" for k, v in sorted(c.items())) for b, c in injetados.items()]
        L.append("")
    if r.posicoes:
        L += ["| braco | posicoes | parciais | ajustes IA | ajustes auto | gestao nao aplicada | liquido | motivos |",
              "|---|---|---|---|---|---|---|---|"]
        for b, x in r.posicoes.items():
            L.append(f"| {b} | {x['episodios']} | {x['parciais']} | {x['ajustes_ia']} | {x['ajustes_auto']} | "
                     f"{x['gestao_nao_aplicada']} | {_brl(x['liquido_total'])} | "
                     + ", ".join(f"{k} x{v}" for k, v in sorted(x["motivos_final"].items())) + " |")
        L.append("")
    if r.latencias:
        L += ["| etapa | n | p50 (ms) | p95 (ms) | max (ms) |", "|---|---|---|---|---|"]
        for e, v in r.latencias.items():
            L.append(f"| {e} | {int(v.get('n', 0))} | {v.get('p50_ms')} | {v.get('p95_ms')} | {v.get('max_ms')} |")
    L += ["", f"Banco: `{r.banco}` (painel: `robowin painel --banco <arquivo>`)"]
    return "\n".join(L) + "\n"


def _servidos(res: ResultadoHistorico) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {}
    for r in res.dias:
        for b, ms in r.modelos_servidos.items():
            out.setdefault(b, set()).update(ms)
    return {b: sorted(ms) for b, ms in sorted(out.items())}


def dias_perdas_seguidas(res: ResultadoHistorico) -> dict[str, list[str]]:
    """Pregoes em que o braco ficou bloqueado por perdas seguidas (o dia "encerrou" para ele)."""
    out: dict[str, list[str]] = {}
    for r in res.dias:
        for b, c in r.bloqueios.items():
            if any("perdas_seguidas" in k for k in c):
                out.setdefault(b, []).append(r.data.isoformat())
    return out


def consolidar(res: ResultadoHistorico, perfil: PerfilInstrumento, risco: ConfigRisco | None = None, *,
               preregistro: PreRegistro | None = None, dir_preregistro: Path | None = None,
               hashes_variantes: Mapping[str, str] | None = None) -> dict[str, Any]:
    bracos = sorted({b for r in res.dias for b in r.trades})
    trades: dict[str, list[Trade]] = {b: [t for r in res.dias for t in r.trades.get(b, [])] for b in bracos}
    contaminado: bool | None = any(r.contaminacao == "contaminado" for r in res.dias) if res.dias else None
    geometrias: dict[str, tuple[float, float]] = {}
    for b in bracos:
        g = aval.geometria_mediana([par for r in res.dias for par in r.geometrias.get(b, [])])
        if g is not None:
            geometrias[b] = g
    soma: dict[str, Counter[str]] = {}
    for r in res.dias:
        for b, c in r.bloqueios.items():
            soma.setdefault(b, Counter()).update(c)
    servidos = _servidos(res)
    tentativas: Counter[str] = Counter()
    for r in res.dias:
        tentativas.update(r.tentativas_entrada)
    rel = aval.gerar_relatorio(trades, controle=CONTROLE if CONTROLE in trades else None, sintetico=False,
                               contaminado=contaminado, tentativas=max(1, len(bracos)), n_boot=res.params.n_boot,
                               seed=res.params.seed, perfil=perfil, risco=risco,
                               titulo=f"Historico causal {res.ativo} - consolidado ({len(res.dias)} pregoes)",
                               geometrias=geometrias, preregistro=preregistro, dir_preregistro=dir_preregistro,
                               datas_pregoes=[r.data.isoformat() for r in res.dias],
                               provedor_falso=res.provedor == "falso", hashes_variantes=hashes_variantes,
                               injetados={b: dict(c) for b, c in soma.items()}, modelos_servidos=servidos,
                               tentativas_entrada=dict(tentativas),
                               episodios=[e for r in res.dias for e in r.episodios],
                               eventos_gestao=[e for r in res.dias for e in r.eventos_gestao],
                               dias_perdas_seguidas=dias_perdas_seguidas(res))
    falsos = set(rel["injetados"]["falsos"])
    tot: dict[str, Counter[str]] = {}
    for b in list(soma):  # falhas INJETADAS pelo FALSO saem dos bloqueios (ficam so na secao "injetado (FALSO)")
        if b in falsos:
            soma[b] = Counter({k: v for k, v in soma[b].items() if not aval.eh_motivo_injetado(k)})
    for r in res.dias:
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
            "contaminados": sum(r.contaminacao == "contaminado" for r in res.dias),
            "injetados": rel["injetados"],
            "posicoes_por_dia": {r.data.isoformat(): r.posicoes for r in res.dias if r.posicoes},
            "previsoes_ia": sum(len(r.previsoes_ia) for r in res.dias),
            "contrafactuais": sum(len(r.contrafactuais) for r in res.dias)}


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
    if c.get("previsoes_ia") or c.get("contrafactuais"):
        L += ["", f"Previsoes da IA registradas: {c.get('previsoes_ia', 0)}; contrafactuais (so diagnostico): "
                  f"{c.get('contrafactuais', 0)} (arquivos por pregao em dias/<pregao>/)."]
    pico = c["tempos_s"].get("memoria_pico_mb")
    L += ["", f"Tempo total: {c['tempos_s'].get('total')} s" + (f"; pico de memoria {pico} MB." if pico else "."),
          "", "---", ""]
    L.append(aval.renderizar_markdown(c["relatorio"]))
    return "\n".join(L) + "\n"


def escrever(res: ResultadoHistorico, perfil: PerfilInstrumento, risco: ConfigRisco | None = None, *,
             preregistro: PreRegistro | None = None, dir_preregistro: Path | None = None,
             hashes_variantes: Mapping[str, str] | None = None) -> dict[str, Path]:
    """Grava saida/dias/<D>/relatorio.{md,json} (+ ``previsoes_ia.json`` e ``contrafactuais.json`` quando houver) e
    saida/consolidado.{md,json}. Devolve os caminhos principais."""
    saida = Path(res.params.saida)
    for r in res.dias:
        d = saida / "dias" / r.data.isoformat()
        d.mkdir(parents=True, exist_ok=True)
        (d / "relatorio.md").write_text(markdown_dia(r, res), encoding="utf-8")
        salvar_json(d / "relatorio.json", r.como_dict())
        if r.previsoes_ia:
            salvar_json(d / "previsoes_ia.json", r.previsoes_ia)
        if r.contrafactuais:
            salvar_json(d / "contrafactuais.json", r.contrafactuais)
    c = consolidar(res, perfil, risco, preregistro=preregistro, dir_preregistro=dir_preregistro,
                   hashes_variantes=hashes_variantes)
    md = markdown_consolidado(c, res)
    (saida / "consolidado.md").write_text(md, encoding="utf-8")
    salvar_json(saida / "consolidado.json", c)
    return {"consolidado_md": saida / "consolidado.md", "consolidado_json": saida / "consolidado.json"}
