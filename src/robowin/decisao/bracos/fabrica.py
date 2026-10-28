"""Fabrica de bracos de IA: papeis plugaveis e bracos extras declarados em ``[[bracos]]`` (plano 5.10).

Trocar o modelo de um papel (``[ia.papeis.proponente]``) ou declarar um braco novo muda o experimento SEM
tocar no codigo; o novo par proponente/revisor entra no manifesto, no painel e na correcao de Holm. Qualquer
papel (inclusive papeis novos) e qualquer braco aceitam QUALQUER ``provedor:modelo`` do catalogo, de qualquer
provedor do registro (config/provedores.toml: Gemini, DeepSeek, Grok, Mistral, Llama local, OpenRouter...).
Modelo fora do catalogo e recusado (falha segura); preco/corte desconhecidos so com declaracao explicita
(``robowin ia adicionar-modelo``), rotulada no manifesto.

Exemplo::

    [[bracos]]
    id = "D-gemini"
    proponente = "google:<modelo>"          # 'provedor:modelo' ou o NOME de um papel de [ia.papeis]
    revisor = "anthropic:claude-opus-5-5"   # opcional: sem revisor, o braco e do tipo B
    modo = "condicional"                    # imediato | condicional
    effort_proponente = "low"               # opcional
    effort_revisor = "medium"               # opcional

Braco EXPERIMENTAL de debate (desligado por padrao; so no modo condicional; ver ``debate.py``)::

    [[bracos]]
    id = "debate_cond"
    tipo = "debate"
    proponente = "sintetizador"             # papel (ou 'provedor:modelo') do SINTETIZADOR; faz o papel de proponente
    revisor = "revisor"                     # opcional: com revisor o braco e o analogo do C
    modo = "condicional"
    pesquisador = "pesquisador"             # papel/modelo dos pesquisadores altista e baixista (padrao: o proponente)
    debatedor_risco = "debatedor_risco"     # papel/modelo dos tres debatedores de risco (padrao: o proponente)
    rodadas = 1                             # 1 ou 2 rodadas de pesquisa
    veto_unanime_risco = true               # tres pareceres 'nao_operar' => NAO_OPERAR
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from robowin.core.tipos import Papel
from robowin.ia.catalogo import Catalogo, dividir_modelo

from ..validade import Prazos, prazos_de_config
from .base import MODOS, Geometria, Modo
from .claude import BracoProponente
from .claude_gpt import BracoProponenteRevisor
from .debate import (
    ETAPAS_PESQUISA,
    ETAPAS_RISCO,
    BracoDebate,
    ConfigDebate,
    EtapaDebate,
    ServicoPropostaDebate,
)
from .llm import BracoLLM, ServicoProposta, ServicoRevisao

TIPOS_BRACO = ("llm", "debate")


class ErroConfigBraco(Exception):
    pass


@dataclass(frozen=True)
class ConfigBracoExtra:
    id: str
    proponente: str
    revisor: str | None = None
    modo: Modo = "imediato"
    effort_proponente: str | None = None
    effort_revisor: str | None = None
    tempo_max_s: int | None = None
    tipo: str = "llm"  # "llm" (B/C) | "debate" (experimental)
    pesquisador: str | None = None  # so tipo=debate
    debatedor_risco: str | None = None  # so tipo=debate
    effort_pesquisador: str | None = None
    effort_debatedor_risco: str | None = None
    rodadas: int = 1
    veto_unanime_risco: bool = True


def extras_de_config(cfg: Mapping[str, Any]) -> list[ConfigBracoExtra]:
    """Le as entradas ``[[bracos]]`` de config/robowin.toml."""
    out: list[ConfigBracoExtra] = []
    ids: set[str] = set()
    for b in cfg.get("bracos", []) or []:
        if "id" not in b or "proponente" not in b:
            raise ErroConfigBraco(f"[[bracos]] exige 'id' e 'proponente': {b}")
        modo = b.get("modo", "imediato")
        if modo not in MODOS:
            raise ErroConfigBraco(f"braco {b['id']}: modo invalido {modo!r}")
        if b["id"] in ids:
            raise ErroConfigBraco(f"braco duplicado: {b['id']}")
        ids.add(b["id"])
        tipo = b.get("tipo", "llm")
        if tipo not in TIPOS_BRACO:
            raise ErroConfigBraco(f"braco {b['id']}: tipo invalido {tipo!r} (use {TIPOS_BRACO})")
        if tipo == "debate" and modo != "condicional":
            raise ErroConfigBraco(f"braco {b['id']}: o debate so existe no modo condicional")
        rodadas = int(b.get("rodadas", 1))
        if tipo == "debate" and not 1 <= rodadas <= 2:
            raise ErroConfigBraco(f"braco {b['id']}: rodadas deve ser 1 ou 2")
        out.append(ConfigBracoExtra(
            id=str(b["id"]), proponente=str(b["proponente"]), revisor=b.get("revisor"), modo=modo,
            effort_proponente=b.get("effort_proponente"), effort_revisor=b.get("effort_revisor"),
            tempo_max_s=b.get("tempo_max_s"), tipo=tipo, pesquisador=b.get("pesquisador"),
            debatedor_risco=b.get("debatedor_risco"), effort_pesquisador=b.get("effort_pesquisador"),
            effort_debatedor_risco=b.get("effort_debatedor_risco"), rodadas=rodadas,
            veto_unanime_risco=bool(b.get("veto_unanime_risco", True))))
    return out


class FabricaBracos:
    def __init__(self, provedores: Mapping[str, Any], *, catalogo: Catalogo | None = None,
                 papeis: Mapping[str, Papel] | None = None, diretorio_prompts: str | Path | None = None,
                 stop_max_pontos: float, prazos: Mapping[str, Prazos] | None = None,
                 geometria: Geometria, cfg: Mapping[str, Any] | None = None) -> None:
        self.provedores = dict(provedores)
        self.catalogo = catalogo
        self.papeis = dict(papeis or {})
        self.diretorio_prompts = diretorio_prompts
        self.stop_max_pontos = stop_max_pontos
        self.prazos = dict(prazos) if prazos is not None else prazos_de_config(dict(cfg or {}))
        self.geometria = geometria
        self._props: dict[tuple[str, str | None], ServicoProposta] = {}
        self._revs: dict[tuple[str, str | None], ServicoRevisao] = {}

    # --- resolucao de papeis/modelos -----------------------------------------------------------------
    def resolver(self, valor: str, effort: str | None = None) -> tuple[str, str | None]:
        """'provedor:modelo' ou nome de papel -> (ref, effort)."""
        if ":" in valor:
            ref, eff = valor, effort
        elif valor in self.papeis:
            ref, eff = self.papeis[valor].modelo, effort or self.papeis[valor].effort
        else:
            raise ErroConfigBraco(f"nao e 'provedor:modelo' nem papel configurado: {valor!r}")
        prov, _ = dividir_modelo(ref)
        if prov not in self.provedores:
            raise ErroConfigBraco(f"provedor sem adaptador disponivel: {prov!r} (para {ref})")
        if self.catalogo is not None:
            self.catalogo.obter(ref)  # ErroCatalogo: sem preco/corte/fora do catalogo => recusado
        return ref, eff

    def servico_proposta(self, valor: str, effort: str | None = None) -> ServicoProposta:
        ref, eff = self.resolver(valor, effort)
        k = (ref, eff)
        if k not in self._props:
            prov, _ = dividir_modelo(ref)
            self._props[k] = ServicoProposta(self.provedores[prov], ref, effort=eff,
                                             stop_max_pontos=self.stop_max_pontos,
                                             diretorio_prompts=self.diretorio_prompts)
        return self._props[k]

    def servico_revisao(self, valor: str, effort: str | None = None) -> ServicoRevisao:
        ref, eff = self.resolver(valor, effort)
        k = (ref, eff)
        if k not in self._revs:
            prov, _ = dividir_modelo(ref)
            self._revs[k] = ServicoRevisao(self.provedores[prov], ref, effort=eff, max_tokens=800,
                                           diretorio_prompts=self.diretorio_prompts)
        return self._revs[k]

    # --- construcao ----------------------------------------------------------------------------------
    def braco(self, id: str, proponente: str, revisor: str | None = None, modo: Modo = "imediato", *,
              effort_proponente: str | None = None, effort_revisor: str | None = None,
              tempo_max_s: int | None = None) -> BracoLLM:
        prop = self.servico_proposta(proponente, effort_proponente)
        g = self.geometria if tempo_max_s is None else Geometria(
            self.geometria.stop_pontos, self.geometria.alvo_pontos, tempo_max_s)
        pz = self.prazos[modo]
        if revisor is None:
            return BracoProponente(id, prop, modo, g, pz)
        return BracoProponenteRevisor(id, prop, self.servico_revisao(revisor, effort_revisor), modo, g, pz)

    def braco_debate(self, e: ConfigBracoExtra) -> BracoDebate:
        """Braco de debate: sintetizador (= proponente), pesquisadores e debatedores de risco pelos papeis."""
        ref_s, eff_s = self.resolver(e.proponente, e.effort_proponente)
        prov_s, _ = dividir_modelo(ref_s)
        ref_p, eff_p = self.resolver(e.pesquisador or e.proponente, e.effort_pesquisador)
        ref_r, eff_r = self.resolver(e.debatedor_risco or e.proponente, e.effort_debatedor_risco)
        etapas: dict[str, EtapaDebate] = {}
        for nome, ref, eff in ([(n, ref_p, eff_p) for n in ETAPAS_PESQUISA]
                               + [(n, ref_r, eff_r) for n in ETAPAS_RISCO]):
            prov, _ = dividir_modelo(ref)
            etapas[nome] = EtapaDebate(self.provedores[prov], ref, nome, effort=eff,
                                       diretorio_prompts=self.diretorio_prompts)
        serv = ServicoPropostaDebate(
            self.provedores[prov_s], ref_s, etapas, config=ConfigDebate(e.rodadas, e.veto_unanime_risco),
            stop_max_pontos=self.stop_max_pontos, effort=eff_s, diretorio_prompts=self.diretorio_prompts)
        g = self.geometria if e.tempo_max_s is None else Geometria(
            self.geometria.stop_pontos, self.geometria.alvo_pontos, e.tempo_max_s)
        rev = self.servico_revisao(e.revisor, e.effort_revisor) if e.revisor else None
        return BracoDebate(e.id, serv, rev, e.modo, g, self.prazos[e.modo])

    def extras(self, cfg: Mapping[str, Any]) -> list[BracoLLM]:
        out: list[BracoLLM] = []
        for e in extras_de_config(cfg):
            if e.tipo == "debate":
                out.append(self.braco_debate(e))
            else:
                out.append(self.braco(e.id, e.proponente, e.revisor, e.modo, effort_proponente=e.effort_proponente,
                                      effort_revisor=e.effort_revisor, tempo_max_s=e.tempo_max_s))
        return out

    def padrao(self, modos: tuple[Modo, ...] = ("imediato", "condicional"),
               effort_revisor_condicional: str | None = "medium") -> list[BracoLLM]:
        """B (Claude) e C (Claude + GPT) pelos papeis ``proponente``/``revisor`` de [ia.papeis].

        ids: claude, claude_gpt (imediato); claude_cond, claude_gpt_cond (condicional). O revisor do plano
        usa ``effort_revisor_condicional`` (plano 5.2: low no imediato, medium no plano)."""
        out: list[BracoLLM] = []
        for modo in modos:
            suf = "" if modo == "imediato" else "_cond"
            out.append(self.braco(f"claude{suf}", "proponente", None, modo))
            eff_rev = effort_revisor_condicional if modo == "condicional" else None
            out.append(self.braco(f"claude_gpt{suf}", "proponente", "revisor", modo, effort_revisor=eff_rev))
        return out
