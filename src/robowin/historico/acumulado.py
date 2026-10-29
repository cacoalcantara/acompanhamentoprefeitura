"""Amostra ACUMULADA entre execucoes do historico (docs/16 secao 4.3, item 7: "Dados acumulados").

A B3 so guarda uns 20-30 pregoes online e o CI roda todo dia sobre a janela recente: sem acumular, o n do
experimento nunca passaria da janela. Aqui cada pregao simulado vira um arquivo JSON em
``<dir>/<assinatura>/dias/<AAAA-MM-DD>.json`` (trades completos, episodios, gestao, tentativas, modelos servidos,
bloqueios, latencias). A proxima execucao:

* pula a SIMULACAO dos pregoes ja acumulados com a mesma assinatura (eles continuam no treino walk-forward e na
  Maquina do Tempo dos pregoes seguintes, porque os ticks sao recarregados);
* consolida a uniao (acumulados + novos), sempre ordenada por data.

A ``assinatura`` junta o que torna dois pregoes comparaveis: ativo, provedor, seed, bracos, parametros do replay, os
hashes das variantes pre-registradas (codigo, prompts, esquemas, configuracao do planejador) e o sha dos arquivos de
configuracao e risco. Mudou qualquer um: assinatura nova, amostra nova (nunca se mistura codigo diferente).
Nada aqui le ou altera limites de risco.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from robowin.core.tipos import Trade

from .runner import ParamsHistorico, ResultadoDia, ResultadoHistorico, salvar_json

VERSAO = 1


def _sha_arquivo(p: Path | None) -> str:
    if p is None or not Path(p).is_file():
        return "ausente"
    return hashlib.sha256(Path(p).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def assinatura(p: ParamsHistorico, ativo: str, provedor: str,
               hashes_variantes: Mapping[str, str] | None = None) -> str:
    """Identidade da amostra: so pregoes com a MESMA assinatura sao consolidados juntos."""
    corpo = {
        "versao": VERSAO, "ativo": ativo, "provedor": provedor, "seed": p.seed,
        "bracos": sorted(p.bracos) if p.bracos else None, "compactar_ms": p.compactar_ms,
        "dias_treino": p.dias_treino, "tipo_modelo": p.tipo_modelo, "periodo_ia_s": p.periodo_ia_s,
        "idade_max_dado_s": p.idade_max_dado_s, "com_condicional": p.com_condicional, "p_timeout": p.p_timeout,
        "p_http": p.p_http, "contrafactuais": p.contrafactuais, "usuario": p.usuario,
        "conta": p.conta_negociacao, "variantes": dict(sorted((hashes_variantes or {}).items())),
        "config": _sha_arquivo(p.config), "catalogo": _sha_arquivo(p.catalogo), "risco": _sha_arquivo(p.risco),
        "aprovacao": _sha_arquivo(p.aprovacao),
    }
    txt = json.dumps(corpo, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()[:24]


def _pasta(raiz: Path, assin: str) -> Path:
    return Path(raiz) / assin / "dias"


def _dia_para_json(r: ResultadoDia) -> dict[str, Any]:
    d = {f.name: getattr(r, f.name) for f in dataclasses.fields(r)}
    d["data"] = r.data.isoformat()
    d["trades"] = {b: [t.model_dump(mode="json") for t in ts] for b, ts in r.trades.items()}
    d["geometrias"] = {b: [list(g) for g in gs] for b, gs in r.geometrias.items()}
    return {"versao": VERSAO, "dia": d}


def _dia_de_json(obj: Mapping[str, Any]) -> ResultadoDia:
    d = dict(obj["dia"])
    d["data"] = dt.date.fromisoformat(d["data"])
    d["trades"] = {b: [Trade.model_validate(t) for t in ts] for b, ts in (d.get("trades") or {}).items()}
    d["geometrias"] = {b: [(float(g[0]), float(g[1])) for g in gs] for b, gs in (d.get("geometrias") or {}).items()}
    nomes = {f.name for f in dataclasses.fields(ResultadoDia)}
    return ResultadoDia(**{k: v for k, v in d.items() if k in nomes})


def datas_acumuladas(raiz: Path | None, assin: str) -> set[dt.date]:
    if raiz is None or not _pasta(raiz, assin).is_dir():
        return set()
    out: set[dt.date] = set()
    for f in _pasta(raiz, assin).glob("*.json"):
        try:
            out.add(dt.date.fromisoformat(f.stem))
        except ValueError:
            continue
    return out


def carregar(raiz: Path, assin: str) -> list[ResultadoDia]:
    """Pregoes acumulados com a assinatura ``assin`` (arquivo ilegivel ou de outra versao: ignorado)."""
    out: list[ResultadoDia] = []
    if not _pasta(raiz, assin).is_dir():
        return out
    for f in sorted(_pasta(raiz, assin).glob("*.json")):
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
            if obj.get("versao") != VERSAO:
                continue
            out.append(_dia_de_json(obj))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def salvar(raiz: Path, assin: str, dias: list[ResultadoDia]) -> list[Path]:
    """Grava cada pregao novo (nunca sobrescreve um pregao ja acumulado com a mesma assinatura)."""
    pasta = _pasta(raiz, assin)
    pasta.mkdir(parents=True, exist_ok=True)
    gravados: list[Path] = []
    for r in dias:
        arq = pasta / f"{r.data.isoformat()}.json"
        if arq.exists():
            continue
        salvar_json(arq, _dia_para_json(r))
        gravados.append(arq)
    return gravados


def mesclar(res: ResultadoHistorico, anteriores: list[ResultadoDia]) -> ResultadoHistorico:
    """Resultado com a UNIAO dos pregoes (os desta execucao prevalecem), ordenada por data."""
    por_data = {r.data: r for r in anteriores}
    por_data.update({r.data: r for r in res.dias})
    return dataclasses.replace(res, dias=[por_data[d] for d in sorted(por_data)])


__all__ = ["assinatura", "carregar", "datas_acumuladas", "mesclar", "salvar"]
