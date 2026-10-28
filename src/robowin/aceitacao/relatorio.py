"""Relatorio da aceitacao: versoes, hashes, conta MASCARADA, latencias, resultado por etapa e veredito final.

Nunca grava senha, chave de API nem o login completo: tudo passa por `matriz.higienizar` (login -> ****NN, usuario do
Windows nos caminhos -> ***, `ia.segredos.redigir`) imediatamente antes de ir para o disco ou para a tela.
"""
from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import subprocess
from pathlib import Path
from typing import Any

from robowin.core.config import resolver_aprovacao, sha256_arquivo

from .etapas import Contexto
from .matriz import (
    APROVADO,
    NAO_VERIFICAVEL,
    REPROVADO,
    ResultadoEtapa,
    contagem,
    higienizar,
    mascarar_login,
    para_dict,
    veredito,
)

ARQUIVOS_HASH = (
    "config/risco.toml", "config/aprovacoes/risco-v1.toml", "config/instrumentos.toml", "config/robowin.toml",
    "config/bolsas.toml", "config/modelos.toml", "mql4/RobowinBridge.mq4",
)


def _git(raiz: Path | None, *args: str) -> str:
    if raiz is None:
        return "n/d"
    try:
        r = subprocess.run(["git", *args], cwd=raiz, capture_output=True, text=True, timeout=10, check=False)
        return r.stdout.strip() if r.returncode == 0 else "n/d"
    except (OSError, subprocess.SubprocessError):
        return "n/d"


def versoes_e_hashes(ctx: Contexto, raiz: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    try:
        ver_pkg = importlib.metadata.version("robowin")
    except importlib.metadata.PackageNotFoundError:
        ver_pkg = "n/d"
    sujo = _git(raiz, "status", "--porcelain")
    versoes = {**ctx.versoes, "robowin": ver_pkg, "git_commit": _git(raiz, "rev-parse", "HEAD"),
               "git_arvore_modificada": "n/d" if sujo == "n/d" and raiz is None else ("sim" if sujo else "nao")}
    hashes: dict[str, str] = {}
    if raiz is not None:
        for rel in ARQUIVOS_HASH:
            p = raiz / rel
            if p.is_file():
                hashes[rel] = sha256_arquivo(p)
        efetiva = resolver_aprovacao(raiz / "config/aprovacoes/risco-v1.toml", raiz / "config/risco.toml")
        if efetiva is not None and efetiva.is_file():  # versao mais recente valida (robowin risco aprovar)
            hashes[f"aprovacao efetiva ({efetiva.name})"] = sha256_arquivo(efetiva)
    hashes[f"perfil {ctx.perfil.simbolo_base}"] = ctx.perfil.hash()
    return versoes, hashes


def montar(ctx: Contexto, res: list[ResultadoEtapa], raiz: Path | None, *, gerado_em: dt.datetime | None = None) -> dict[str, Any]:
    versoes, hashes = versoes_e_hashes(ctx, raiz)
    c = ctx.conta
    conta = {
        "login_mascarado": mascarar_login(c.get("login", ctx.login)),
        "servidor": c.get("servidor", ctx.servidor or "n/d"), "modo": c.get("modo", "n/d"), "margem": c.get("margem", "n/d"),
        "moeda": c.get("moeda", "n/d"), "algotrading_conta": c.get("trade_expert"), "negociacao_permitida": c.get("trade_allowed"),
    }
    lat = {k: {kk: vv for kk, vv in v.items() if kk != "amostras"} for k, v in ctx.latencias.items()}
    cont = contagem(res)
    return {
        "gerado_em_utc": (gerado_em or dt.datetime.now(dt.UTC)).isoformat(),
        "veredito": veredito(res, duble=ctx.duble, abortada=ctx.abortar is not None),
        "duble": ctx.duble, "plataforma": ctx.plataforma, "ativo": ctx.perfil.simbolo_base,
        "perfil_a_confirmar": ctx.perfil.a_confirmar,
        "abortada": ctx.abortar,
        "confirmou_conta_demo": ctx.confirmo,
        "ordens_enviadas": ctx.envios,
        "parametros": {"minutos_coleta": ctx.minutos_coleta, "repeticoes": ctx.repeticoes, "pausa_s": ctx.pausa_s,
                       "limite_latencia_ms": ctx.limite_latencia_ms, "msgs_por_min": ctx.msgs_por_min},
        "versoes": versoes, "hashes_sha256": hashes, "conta": conta, "latencias_ms": lat,
        "contagem": cont,
        "etapas": [para_dict(r) for r in res],
    }


def _linha(r: dict[str, Any]) -> str:
    det = str(r["detalhe"]).replace("|", "/").replace("\n", " ")
    return f"| {r['etapa']} | {r['estado']} | {r['nome']} | {det} |"


def texto_md(rel: dict[str, Any]) -> str:
    c = rel["contagem"]
    L = [f"# Relatorio de aceitacao - robowin ({rel['plataforma'].upper()}, ativo {rel['ativo']})", "",
         f"**Veredito: {rel['veredito']}**", "",
         f"- Gerado em (UTC): {rel['gerado_em_utc']}",
         f"- Origem: {'DUBLE SINTETICO (nao e o terminal real)' if rel['duble'] else 'terminal real, conta DEMO'}",
         f"- --confirmo-conta-demo: {'sim' if rel['confirmou_conta_demo'] else 'NAO (somente etapas de leitura)'}",
         f"- Ordens enviadas ao terminal (order_send): {rel['ordens_enviadas']}",
         f"- Resumo: {c[APROVADO]} aprovada(s), {c[REPROVADO]} reprovada(s), {c[NAO_VERIFICAVEL]} nao verificavel(is)"]
    if rel.get("abortada"):
        L.append(f"- EXECUCAO ABORTADA: {rel['abortada']}")
    if rel.get("perfil_a_confirmar"):
        L.append("- Perfil do ativo `a_confirmar`: as etapas de ordem nao rodaram.")
    k = rel["conta"]
    L += ["", "## Conta (mascarada)", "",
          f"- Login: {k['login_mascarado']}   Servidor: {k['servidor']}   Modo: {k['modo']}   Margem: {k['margem']}   Moeda: {k['moeda']}",
          f"- Algo Trading (trade_expert): {k['algotrading_conta']}   Negociacao permitida: {k['negociacao_permitida']}",
          "- Senha, chaves de API e login completo NUNCA entram neste relatorio.",
          "", "## Versoes", ""]
    L += [f"- {a}: {b}" for a, b in rel["versoes"].items()]
    L += ["", "## Hashes (sha256)", ""] + [f"- `{a}`: `{b}`" for a, b in rel["hashes_sha256"].items()]
    L += ["", "## Parametros", ""] + [f"- {a}: {b}" for a, b in rel["parametros"].items()]
    L += ["", "## Latencias (ms)", ""]
    if rel["latencias_ms"]:
        L += ["| Medida | n | p50 | p95 | p99 | outros |", "|---|---|---|---|---|---|"]
        for nome, v in rel["latencias_ms"].items():
            outros = {kk: vv for kk, vv in v.items() if kk not in ("n", "p50", "p95", "p99")}
            L.append(f"| {nome} | {v.get('n', '')} | {v.get('p50', '')} | {v.get('p95', '')} | {v.get('p99', '')} | {outros or ''} |")
    else:
        L.append("(nenhuma latencia medida: etapas A6/A10 nao rodaram)")
    L += ["", "## Resultado por etapa", "", "| Etapa | Estado | Nome | Detalhe |", "|---|---|---|---|"]
    L += [_linha(r) for r in rel["etapas"]]
    L += ["", "## Criterios de aprovacao", ""]
    L += [f"- **{r['etapa']}** ({r['estado']}): {r['criterio']}" for r in rel["etapas"]]
    L += ["", "Aprovado aqui nao prova lucratividade. NAO VERIFICAVEL nunca conta como aprovado.", ""]
    return "\n".join(L)


def gravar(rel: dict[str, Any], saida: Path, logins: tuple[Any, ...] = (),
           carimbo: str | None = None) -> tuple[Path, Path, dict[str, Any]]:
    """Grava `aceitacao-<data>.md/.json` ja higienizados. Devolve (md, json, relatorio higienizado)."""
    saida.mkdir(parents=True, exist_ok=True)
    carimbo = carimbo or dt.datetime.fromisoformat(rel["gerado_em_utc"]).strftime("%Y-%m-%d-%H%M%S")
    j = higienizar(json.dumps(rel, ensure_ascii=False, indent=2, default=str), logins)
    rel_h = json.loads(j)
    md = higienizar(texto_md(rel_h), logins)
    p_md, p_js = saida / f"aceitacao-{carimbo}.md", saida / f"aceitacao-{carimbo}.json"
    p_md.write_text(md, encoding="utf-8")
    p_js.write_text(j, encoding="utf-8")
    return p_md, p_js, rel_h
