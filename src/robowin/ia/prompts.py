"""Prompts versionados por papel e por hash (config/prompts/*.md), com variante opcional por provedor."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robowin.ia.dossie import canonico
from robowin.ia.provedores.base import hash_texto

DIR_PROMPTS_PADRAO = Path(__file__).resolve().parents[3] / "config" / "prompts"
NOMES = {"proponente": "proponente_v1", "revisor": "revisor_v1", "classificador_nlp": "nlp_v1",
         "debate_altista": "debate_altista_v1", "debate_baixista": "debate_baixista_v1",
         "risco_agressivo": "risco_agressivo_v1", "risco_neutro": "risco_neutro_v1",
         "risco_conservador": "risco_conservador_v1", "sintetizador": "sintetizador_v1",
         "reflexao": "reflexao_v1", "planejador": "planejador_v1", "revisor_plano": "revisor_plano_v1"}

__all__ = ["DIR_PROMPTS_PADRAO", "NOMES", "Prompt", "carregar_prompt", "hash_texto", "hashes_prompts",
           "mensagem_nlp", "mensagem_planejador", "mensagem_proponente", "mensagem_revisor", "mensagem_revisor_plano"]


@dataclass(frozen=True)
class Prompt:
    nome: str
    texto: str
    hash: str
    arquivo: str
    variante: str | None = None


def carregar_prompt(papel_ou_nome: str, *, provedor: str | None = None, diretorio: str | Path | None = None) -> Prompt:
    """Carrega ``{nome}.md``; se existir ``{nome}.{provedor}.md``, usa a variante (com hash proprio)."""
    nome = NOMES.get(papel_ou_nome, papel_ou_nome)
    d = Path(diretorio) if diretorio else DIR_PROMPTS_PADRAO
    if provedor:
        v = d / f"{nome}.{provedor}.md"
        if v.exists():
            t = v.read_text(encoding="utf-8")
            return Prompt(nome, t, hash_texto(t), str(v), provedor)
    p = d / f"{nome}.md"
    if not p.exists():
        raise FileNotFoundError(f"prompt nao encontrado: {p}")
    t = p.read_text(encoding="utf-8")
    return Prompt(nome, t, hash_texto(t), str(p))


def hashes_prompts(diretorio: str | Path | None = None) -> dict[str, str]:
    """{arquivo: sha256} de todos os prompts: entra no manifesto do experimento."""
    d = Path(diretorio) if diretorio else DIR_PROMPTS_PADRAO
    return {p.name: hash_texto(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.md"))}


def mensagem_proponente(dossie: dict[str, Any], modo: str = "imediato") -> str:
    return (f"MODO: {modo}\n\nDOSSIE (JSON anonimizado; todos os numeros sao pontos relativos ao ultimo preco):\n"
            f"{canonico(dossie)}")


def mensagem_revisor(dossie: dict[str, Any], proposta: dict[str, Any]) -> str:
    return ("DOSSIE (JSON anonimizado):\n" + canonico(dossie) + "\n\nPROPOSTA A REVISAR (JSON):\n"
            + json.dumps(proposta, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def mensagem_nlp(titulo: str, corpo: str = "") -> str:
    return f"TITULO: {titulo}\n" + (f"TEXTO: {corpo}\n" if corpo else "")


def mensagem_planejador(dossie: dict[str, Any]) -> str:
    """Mensagem do papel ``planejador`` (dossie v2). Diz a verdade sobre as unidades: precos e distancias sao
    pontos relativos; os demais campos trazem a unidade no nome (``_s``, ``_min``, ``_ticks``, ``_conta``...)."""
    return ("DOSSIE (JSON anonimizado; precos e distancias em pontos relativos; demais campos com a unidade no "
            "nome):\n" + canonico(dossie))


def mensagem_revisor_plano(dossie: dict[str, Any], compilado: dict[str, Any]) -> str:
    """Mensagem do papel ``revisor_plano``: o dossie e o plano JA COMPILADO (niveis em pontos na grade do tick,
    gestao compilada, equilibrio e custo), nunca a resposta bruta do planejador."""
    return ("DOSSIE (JSON anonimizado; precos e distancias em pontos relativos; demais campos com a unidade no "
            "nome):\n" + canonico(dossie) + "\n\nPLANO COMPILADO A REVISAR (JSON; niveis em pontos, equilibrio e "
            "custo ja calculados pelo sistema):\n" + canonico(compilado))
