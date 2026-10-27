"""Leitura do SQLite (esquema em robowin.core.esquema_banco). Tolerante a banco ausente/vazio."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from robowin.core.esquema_banco import DDL, TABELAS

from .metricas import CONTROLE, metricas_braco, percentis

# Um incidente com esta origem marca o banco inteiro como sintetico (faixa "DADOS SINTÉTICOS").
ORIGEM_SINTETICO = "dados_sinteticos"


class Banco:
    def __init__(self, caminho: str | Path | None):
        self.caminho = Path(caminho) if caminho else None

    def existe(self) -> bool:
        return bool(self.caminho and self.caminho.exists())

    def consultar(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        if not self.existe():
            return []
        con = sqlite3.connect(f"file:{self.caminho}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, args)]
        except sqlite3.OperationalError:  # tabela ausente
            return []
        finally:
            con.close()

    def escrever(self, sql: str, args: tuple = ()) -> None:
        if self.caminho is None:
            raise RuntimeError("sem banco configurado")
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.caminho)
        try:
            for d in DDL:
                con.execute(d)
            con.execute(sql, args)
            con.commit()
        finally:
            con.close()

    def contagens(self) -> dict[str, int]:
        return {t: (self.consultar(f"SELECT COUNT(*) AS c FROM {t}") or [{"c": 0}])[0]["c"] for t in TABELAS}

    def sintetico(self) -> bool:
        return bool(self.consultar("SELECT 1 FROM incidentes WHERE origem=? LIMIT 1", (ORIGEM_SINTETICO,)))


def _json(s: str | None) -> Any:
    try:
        return json.loads(s) if s else None
    except (ValueError, TypeError):
        return None


def ns_para_hora(t_ns: int | None) -> str:
    if not t_ns:
        return "—"
    return time.strftime("%d/%m/%Y %H:%M:%S", time.gmtime(t_ns / 1e9)) + " UTC"


def visao_geral(b: Banco, dia: str | None = None) -> dict[str, Any]:
    posicoes = b.consultar(
        "SELECT p.* FROM posicoes p JOIN (SELECT braco, MAX(t_ns) m FROM posicoes GROUP BY braco) u "
        "ON p.braco=u.braco AND p.t_ns=u.m WHERE p.quantidade>0 ORDER BY p.braco")
    ult = b.consultar("SELECT MAX(t_ns) AS t FROM equity")
    ultimo = ult[0]["t"] if ult else None
    if dia is None:
        d = b.consultar("SELECT MAX(dia) AS d FROM trades")
        dia = d[0]["d"] if d and d[0]["d"] else None
    pnl = b.consultar(
        "SELECT braco, SUM(liquido_brl) AS pnl, COUNT(*) AS n FROM trades WHERE dia=? GROUP BY braco ORDER BY braco",
        (dia,)) if dia else []
    orc = b.consultar("SELECT * FROM orcamento_ia WHERE dia=? ORDER BY braco", (dia,)) if dia else []
    eventos = b.consultar("SELECT * FROM eventos_risco ORDER BY t_ns DESC LIMIT 15")
    for e in eventos:
        e["hora"] = ns_para_hora(e["t_ns"])
    incid = b.consultar("SELECT * FROM incidentes WHERE origem<>? ORDER BY t_ns DESC LIMIT 10", (ORIGEM_SINTETICO,))
    for i in incid:
        i["hora"] = ns_para_hora(i["t_ns"])
    limites = b.consultar(
        "SELECT tipo, COUNT(*) AS n, SUM(critico) AS criticos FROM eventos_risco GROUP BY tipo ORDER BY n DESC")
    return {"posicoes": posicoes, "ultimo_dado": ns_para_hora(ultimo) if ultimo else None, "dia": dia,
            "pnl": pnl, "orcamento": orc, "eventos": eventos, "incidentes": incid, "limites": limites}


def decisoes(b: Banco, limite: int = 100) -> list[dict[str, Any]]:
    rows = b.consultar("SELECT * FROM decisoes ORDER BY t_ns DESC LIMIT ?", (limite,))
    for r in rows:
        r["hora"] = ns_para_hora(r["t_ns"])
        r["proposta"] = _json(r["proposta_json"])
        r["revisao"] = _json(r["revisao_json"])
        r["risco"] = _json(r["risco_json"])
        r["intencao"] = _json(r["intencao_json"])
        r["chamadas"] = b.consultar(
            "SELECT papel, modelo_servido, total_ms, custo_usd, valida, erro FROM chamadas_llm "
            "WHERE hash_dossie=? AND braco=? ORDER BY t_ns", (r["hash_dossie"], r["braco"])) if r["hash_dossie"] else []
        r["custo_usd"] = sum(c["custo_usd"] or 0 for c in r["chamadas"])
        r["latencia_ms"] = sum(c["total_ms"] or 0 for c in r["chamadas"])
    return rows


def operacoes(b: Banco, limite: int = 100) -> dict[str, Any]:
    ordens = b.consultar("SELECT * FROM ordens ORDER BY t_ns DESC LIMIT ?", (limite,))
    execs = b.consultar("SELECT * FROM execucoes ORDER BY t_ns DESC LIMIT ?", (limite,))
    trades = b.consultar("SELECT * FROM trades ORDER BY t_saida_ns DESC LIMIT ?", (limite,))
    for lst, campo in ((ordens, "t_ns"), (execs, "t_ns"), (trades, "t_saida_ns")):
        for r in lst:
            r["hora"] = ns_para_hora(r[campo])
    return {"ordens": ordens, "execucoes": execs, "trades": trades}


def metricas_locais(b: Banco) -> dict[str, dict[str, Any]]:
    rows = b.consultar("SELECT braco, liquido_brl FROM trades ORDER BY t_saida_ns")
    por: dict[str, list[float]] = {}
    for r in rows:
        por.setdefault(r["braco"], []).append(r["liquido_brl"])
    return {k: metricas_braco(v) for k, v in por.items()}


def curvas_capital(b: Banco) -> dict[str, dict[str, list[float]]]:
    out: dict[str, dict[str, list[float]]] = {}
    for r in b.consultar("SELECT braco, t_ns, equity_brl FROM equity ORDER BY braco, t_ns"):
        c = out.setdefault(r["braco"], {"t": [], "y": []})
        c["t"].append(r["t_ns"] / 1e9)
        c["y"].append(r["equity_brl"])
    return out


def latencia_por_etapa(b: Banco) -> list[dict[str, Any]]:
    por: dict[tuple[str, str], list[float]] = {}
    for r in b.consultar("SELECT braco, etapa, dur_ms FROM latencias"):
        por.setdefault((r["braco"], r["etapa"]), []).append(r["dur_ms"])
    return [{"braco": k[0], "etapa": k[1], "n": len(v), **percentis(v)} for k, v in sorted(por.items())]


def desempenho(b: Banco, provider=None) -> dict[str, Any]:
    m = (provider or metricas_locais)(b)
    return {"metricas": m, "controle": m.get(CONTROLE), "latencia": latencia_por_etapa(b)}


def modelos(b: Banco) -> dict[str, Any]:
    mods = b.consultar("SELECT * FROM modelos ORDER BY nome, versao")
    for m in mods:
        m["metricas"] = _json(m["metricas_json"]) or {}
        m["treinado_ate"] = ns_para_hora(m["treinado_ate_ns"])
    exps = b.consultar("SELECT * FROM experimentos ORDER BY criado_ns DESC")
    for e in exps:
        e["manifesto"] = _json(e["manifesto_json"]) or {}
        e["criado"] = ns_para_hora(e["criado_ns"])
    noticias = b.consultar("SELECT COUNT(*) AS n, SUM(verificado) AS v FROM noticias")
    qual = {
        "contagens": b.contagens(),
        "noticias": noticias[0] if noticias else {"n": 0, "v": 0},
        "chamadas_invalidas": (b.consultar("SELECT COUNT(*) AS c FROM chamadas_llm WHERE valida=0")
                               or [{"c": 0}])[0]["c"],
        "modelo_servido_divergente": (b.consultar(
            "SELECT COUNT(*) AS c FROM chamadas_llm WHERE modelo_servido<>'' AND modelo_servido<>modelo_pedido")
            or [{"c": 0}])[0]["c"],
    }
    return {"modelos": mods, "experimentos": exps, "qualidade": qual}
