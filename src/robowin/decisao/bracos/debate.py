"""Braco EXPERIMENTAL ``debate`` (desligado por padrao; so entra por ``[[bracos]]`` com ``tipo = "debate"``).

Inspirado (so no conceito) na arquitetura multiagente do TradingAgents (Apache-2.0): pesquisador altista e
pesquisador baixista debatem, tres debatedores de risco (agressivo, neutro, conservador) pesam a tese e um
sintetizador decide. Adaptado as regras do robowin:

* os agentes veem SO o dossie anonimizado (mesmo texto que o proponente padrao): nenhuma ferramenta, nenhum
  indicador tecnico, nenhum acesso a internet (``EnvelopeAgente`` declara e ``verificar_envelope`` confere);
* toda saida e estruturada (Pydantic) e passa por validacao deterministica: lado/perfil coerentes, evidencias
  que citam campos EXISTENTES do dossie, texto sem indicador tecnico e com tamanho limitado;
* [v1.1] (docs/16 C16) a saida de uma etapa so segue para a etapa seguinte depois de ``sanitizar_textos``: texto que
  falhe em ``ia.validadores.texto_seguro`` (marcacao, termo de instrucao, nivel absoluto) vira um marcador;
* cada etapa tem prazo (``asyncio.wait_for`` + prazo do provedor) tirado do orcamento do modo condicional;
  qualquer falha de etapa BLOQUEIA a entrada (falha segura: nunca cai em texto livre);
* o sintetizador cumpre o papel ``proponente``: a saida e uma ``PropostaIA`` comum, que passa pelos MESMOS
  validadores, pelo revisor (se o braco for do tipo C) e pelo veto final do motor de risco. O debate nao conhece
  o motor de risco e nao tem poder de ordem;
* so no modo ``condicional`` (latencia: 2 a 3 fases em serie + sintese nao cabem na validade de 60 s do imediato);
* orcamento, cache de gravacao/repeticao e Holm/custo/latencia valem como em qualquer braco: as chamadas das
  etapas entram em ``chamadas_llm`` (papeis ``debate_altista``, ``risco_neutro``...) e o resumo do debate vai no
  ``proposta_json`` da decisao.

Consenso nao e prova: nenhum numero aqui sugere que debater melhora o resultado. Isso e uma HIPOTESE (H4) medida
contra o braco B/C no mesmo experimento, com a mesma correcao de Holm.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from robowin.core.tipos import Acao, ChamadaLLM, PropostaIA
from robowin.ia.dossie import canonico
from robowin.ia.esquemas import ArgumentoDebate, ParecerRisco
from robowin.ia.prompts import carregar_prompt
from robowin.ia.validadores import validar_evidencias, validar_textos_livres

from ..validade import RESPOSTA_INVALIDA, RESPOSTA_TARDIA, Prazos
from .base import ContextoDecisao, Geometria, Modo
from .llm import BracoLLM, PropostaObtida, ServicoLLM, ServicoProposta, ServicoRevisao, sanitizar_textos

MARGEM_ESPERA_S = 1.0  # folga do wait_for externo sobre o prazo que o provedor ja respeita
FRACAO_RESERVA_SINTESE = 0.4  # parte do orcamento do modo reservada ao sintetizador
FRACAO_FASE_PESQUISA = 0.3
FRACAO_FASE_RISCO = 0.25
MAX_CHARS_CAMPO = 600
MOTIVO_FORA_DO_MODO = "debate_so_no_modo_condicional"
RODADAS_MAX = 2

ETAPAS_PESQUISA = ("debate_altista", "debate_baixista")
ETAPAS_RISCO = ("risco_agressivo", "risco_neutro", "risco_conservador")
LADO_DA_ETAPA = {"debate_altista": "altista", "debate_baixista": "baixista"}
PERFIL_DA_ETAPA = {"risco_agressivo": "agressivo", "risco_neutro": "neutro", "risco_conservador": "conservador"}


class ErroEnvelope(Exception):
    """Um agente do debate foi declarado com ferramenta ou acesso a internet (proibido)."""


# --------------------------------------------------------------------------------------------------
# envelope de governanca (ideia do Aurum: politica explicita do que cada agente pode usar)
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class EnvelopeAgente:
    """O que um agente do debate PODE usar. Aqui: so o dossie anonimizado; nada mais."""
    etapa: str
    esquema: type[BaseModel]
    ferramentas: tuple[str, ...] = ()
    acesso_internet: bool = False
    entrada: str = "dossie_anonimizado"


ENVELOPES: dict[str, EnvelopeAgente] = {
    **{e: EnvelopeAgente(e, ArgumentoDebate) for e in ETAPAS_PESQUISA},
    **{e: EnvelopeAgente(e, ParecerRisco) for e in ETAPAS_RISCO},
}


def verificar_envelope(env: EnvelopeAgente) -> None:
    if env.ferramentas:
        raise ErroEnvelope(f"{env.etapa}: ferramentas nao permitidas no debate: {env.ferramentas}")
    if env.acesso_internet:
        raise ErroEnvelope(f"{env.etapa}: acesso a internet nao permitido no debate")
    if env.entrada != "dossie_anonimizado":
        raise ErroEnvelope(f"{env.etapa}: a unica entrada permitida e o dossie anonimizado")


# --------------------------------------------------------------------------------------------------
# mensagens (so o dossie anonimizado e as saidas das etapas anteriores)
# --------------------------------------------------------------------------------------------------
def _bloco_dossie(dossie: dict[str, Any]) -> str:
    return ("DOSSIE (JSON anonimizado; todos os numeros de preco sao pontos relativos ao ultimo preco):\n"
            + canonico(dossie))


def mensagem_pesquisador(dossie: dict[str, Any], modo: str, lado: str, rodada: int,
                         oponente: dict[str, Any] | None) -> str:
    m = f"MODO: {modo}\nLADO: {lado}\nRODADA: {rodada}\n\n" + _bloco_dossie(dossie)
    if oponente is not None:
        m += "\n\nARGUMENTO ANTERIOR DO OPONENTE (JSON):\n" + canonico(oponente)
    return m


def mensagem_risco(dossie: dict[str, Any], modo: str, perfil: str, argumentos: dict[str, Any]) -> str:
    return (f"MODO: {modo}\nPERFIL: {perfil}\n\n" + _bloco_dossie(dossie)
            + "\n\nARGUMENTOS DOS PESQUISADORES (JSON):\n" + canonico(argumentos))


def mensagem_sintetizador(dossie: dict[str, Any], modo: str, debate: dict[str, Any] | None) -> str:
    cab = f"MODO: {modo}\n\n" + _bloco_dossie(dossie)
    if debate is None:
        return cab + "\n\nDEBATE: ausente (posicao aberta; decida so pelo dossie)"
    return cab + "\n\nDEBATE (JSON):\n" + canonico(debate)


# --------------------------------------------------------------------------------------------------
# validacao deterministica da saida de cada etapa
# --------------------------------------------------------------------------------------------------
def validar_saida_etapa(etapa: str, obj: BaseModel, dossie: dict[str, Any],
                        max_chars: int = MAX_CHARS_CAMPO) -> list[str]:
    v: list[str] = []
    if isinstance(obj, ArgumentoDebate):
        if LADO_DA_ETAPA.get(etapa) != obj.lado:
            v.append(f"lado {obj.lado!r} incoerente com a etapa {etapa}")
        if not obj.tese.strip():
            v.append("tese vazia")
        if not obj.evidencias:
            v.append("argumento sem evidencias")
        v += validar_evidencias(obj.evidencias, dossie)
        v += validar_textos_livres((obj.tese, *obj.evidencias, *obj.pontos_fracos_do_oponente), max_chars=max_chars)
    elif isinstance(obj, ParecerRisco):
        if PERFIL_DA_ETAPA.get(etapa) != obj.perfil:
            v.append(f"perfil {obj.perfil!r} incoerente com a etapa {etapa}")
        if obj.recomendacao == "operar" and not obj.evidencias:
            v.append("parecer 'operar' sem evidencias")
        if obj.evidencias:
            v += validar_evidencias(obj.evidencias, dossie)
        v += validar_textos_livres((*obj.riscos, *obj.evidencias, obj.justificativa_curta), max_chars=max_chars)
    else:
        v.append(f"esquema inesperado na etapa {etapa}: {type(obj).__name__}")
    return v


# --------------------------------------------------------------------------------------------------
# etapas
# --------------------------------------------------------------------------------------------------
class EtapaDebate(ServicoLLM):
    """Uma etapa do debate ligada a ``provedor:modelo`` (papel proprio, prompt proprio, esquema proprio)."""

    def __init__(self, provedor: Any, ref: str, etapa: str, *, effort: str | None = None, max_tokens: int = 700,
                 diretorio_prompts: str | Path | None = None) -> None:
        env = ENVELOPES[etapa]
        verificar_envelope(env)
        super().__init__(provedor, ref, etapa, env.esquema, effort=effort, max_tokens=max_tokens,
                         diretorio_prompts=diretorio_prompts)
        self.envelope = env


@dataclass
class ResultadoEtapa:
    etapa: str
    ref: str
    saida: BaseModel | None
    chamada: ChamadaLLM
    motivo: str | None
    violacoes: tuple[str, ...] = ()

    def resumo(self) -> dict[str, Any]:
        return {"etapa": self.etapa, "modelo": self.ref, "valida": self.motivo is None, "motivo": self.motivo,
                "violacoes": list(self.violacoes),
                "saida": None if self.saida is None else self.saida.model_dump(mode="json")}


async def rodar_etapa(et: EtapaDebate, mensagem: str, prazo_s: float, ctx: ContextoDecisao) -> ResultadoEtapa:
    """Chama a etapa com prazo duplo (provedor + ``asyncio.wait_for``) e valida a saida. Nunca levanta (salvo
    cancelamento): a falha vira ``motivo`` e BLOQUEIA a entrada."""
    try:
        obj, ch, motivo = await asyncio.wait_for(et.chamar(mensagem, prazo_s, ctx.hash_dossie),
                                                 timeout=prazo_s + MARGEM_ESPERA_S)
    except TimeoutError:
        ch = ChamadaLLM(papel=et.papel, modelo_pedido=et.ref, hash_dossie=ctx.hash_dossie, erro="timeout",
                        total_ms=prazo_s * 1000.0)
        return ResultadoEtapa(et.papel, et.ref, None, ch, RESPOSTA_TARDIA)
    if motivo or obj is None:
        return ResultadoEtapa(et.papel, et.ref, None, ch, motivo or RESPOSTA_INVALIDA)
    viol = validar_saida_etapa(et.papel, obj, ctx.dossie)
    if viol:
        ch = ch.model_copy(update={"valida": False, "erro": "etapa_invalida: " + "; ".join(viol)[:300]})
        return ResultadoEtapa(et.papel, et.ref, None, ch, RESPOSTA_INVALIDA, tuple(viol))
    return ResultadoEtapa(et.papel, et.ref, obj, ch, None)


@dataclass(frozen=True)
class ConfigDebate:
    rodadas: int = 1
    veto_unanime_risco: bool = True  # tres pareceres 'nao_operar' => NAO_OPERAR sem chamar o sintetizador

    def __post_init__(self) -> None:
        if not 1 <= self.rodadas <= RODADAS_MAX:
            raise ValueError(f"rodadas deve estar em [1, {RODADAS_MAX}]")


class ServicoPropostaDebate(ServicoProposta):
    """Servico do papel ``proponente`` cuja proposta nasce do debate. Substitui o ``ServicoProposta`` comum."""

    def __init__(self, provedor: Any, ref: str, etapas: dict[str, EtapaDebate], *, config: ConfigDebate | None = None,
                 dono: str = "", stop_max_pontos: float, effort: str | None = None,
                 diretorio_prompts: str | Path | None = None) -> None:
        faltam = [e for e in (*ETAPAS_PESQUISA, *ETAPAS_RISCO) if e not in etapas]
        if faltam:
            raise ValueError(f"etapas do debate ausentes: {faltam}")
        prompt = carregar_prompt("sintetizador", provedor=ref.split(":", 1)[0], diretorio=diretorio_prompts)
        super().__init__(provedor, ref, dono=dono, stop_max_pontos=stop_max_pontos, effort=effort, prompt=prompt)
        self.etapas = etapas
        self.config = config or ConfigDebate()

    @property
    def chave(self) -> str:
        parte = ",".join(f"{n}={e.ref}|{e.effort}|{e.prompt.hash[:8]}" for n, e in sorted(self.etapas.items()))
        return f"debate:{super().chave}:{parte}:r{self.config.rodadas}"

    # -- orcamento de prazo -------------------------------------------------------------------------------
    @staticmethod
    def _prazo_fase(total_s: float, gasto_s: float, fracao: float) -> float:
        reserva = total_s * FRACAO_RESERVA_SINTESE
        return min(total_s * fracao, total_s - gasto_s - reserva)

    async def _buscar(self, ctx: ContextoDecisao, modo: Modo, prazos: Prazos) -> PropostaObtida:
        dono = self.dono_de(modo)
        if modo != "condicional":
            ch = ChamadaLLM(papel=self.papel, modelo_pedido=self.ref, hash_dossie=ctx.hash_dossie,
                            erro=MOTIVO_FORA_DO_MODO)
            return PropostaObtida(None, ch, MOTIVO_FORA_DO_MODO, (), False, dono)
        total_s = prazos.prazo_proponente()
        posicao_aberta = (ctx.posicao.quantidade or 0) > 0 or int((ctx.dossie.get("posicao") or {})
                                                                  .get("quantidade") or 0) > 0
        if posicao_aberta:  # gestao de posicao aberta: sem debate (saidas nao esperam o debate)
            msg = mensagem_sintetizador(ctx.dossie, modo, None)
            obt = await self._propor(ctx, modo, prazos, msg, total_s)
            obt.extras_proposta = {"debate": {"executado": False, "motivo": "posicao_aberta"}}
            return obt

        extras: list[ChamadaLLM] = []
        resumos: list[dict[str, Any]] = []
        gasto_s = 0.0

        def falha(r: ResultadoEtapa) -> PropostaObtida:
            outras = tuple(c for c in extras if c is not r.chamada)
            return PropostaObtida(None, r.chamada, r.motivo, (f"debate:{r.etapa}:{r.motivo}", *r.violacoes),
                                  False, dono, chamadas_extras=outras, latencia_ms=gasto_s * 1000.0,
                                  extras_proposta={"debate": {"executado": True, "falhou_em": r.etapa,
                                                              "etapas": list(resumos)}})

        # --- fase 1: pesquisadores (em paralelo; N rodadas) --------------------------------------------------------
        niveis = (ctx.preco_ref, ctx.bid, ctx.ask)
        omitidos = 0

        def seguro(obj: BaseModel) -> dict[str, Any]:
            """Saida da etapa como DADO para a etapa seguinte (texto inseguro vira marcador)."""
            nonlocal omitidos
            d, n = sanitizar_textos(obj.model_dump(mode="json"), niveis)
            omitidos += n
            return dict(d)

        argumentos: dict[str, dict[str, Any] | None] = {"altista": None, "baixista": None}
        for rodada in range(1, self.config.rodadas + 1):
            prazo = self._prazo_fase(total_s, gasto_s, FRACAO_FASE_PESQUISA / self.config.rodadas)
            if prazo <= 0:
                ch = ChamadaLLM(papel="debate_altista", modelo_pedido=self.ref, erro="timeout",
                                hash_dossie=ctx.hash_dossie)
                return falha(ResultadoEtapa("debate_altista", self.ref, None, ch, RESPOSTA_TARDIA))
            anterior = dict(argumentos)
            res = await asyncio.gather(*(
                rodar_etapa(self.etapas[n], mensagem_pesquisador(
                    ctx.dossie, modo, LADO_DA_ETAPA[n], rodada,
                    anterior["baixista" if LADO_DA_ETAPA[n] == "altista" else "altista"]), prazo, ctx)
                for n in ETAPAS_PESQUISA))
            gasto_s += max(r.chamada.total_ms for r in res) / 1000.0
            for r in res:
                extras.append(r.chamada)
                resumos.append({**r.resumo(), "rodada": rodada})
            ruim = next((r for r in res if r.motivo), None)
            if ruim is not None:
                return falha(ruim)
            for r in res:
                assert isinstance(r.saida, ArgumentoDebate)
                argumentos[r.saida.lado] = seguro(r.saida)

        # --- fase 2: debate de risco (3 perfis em paralelo) ---------------------------------------------------------
        prazo = self._prazo_fase(total_s, gasto_s, FRACAO_FASE_RISCO)
        if prazo <= 0:
            ch = ChamadaLLM(papel="risco_neutro", modelo_pedido=self.ref, erro="timeout", hash_dossie=ctx.hash_dossie)
            return falha(ResultadoEtapa("risco_neutro", self.ref, None, ch, RESPOSTA_TARDIA))
        res = await asyncio.gather(*(
            rodar_etapa(self.etapas[n], mensagem_risco(ctx.dossie, modo, PERFIL_DA_ETAPA[n], argumentos), prazo, ctx)
            for n in ETAPAS_RISCO))
        gasto_s += max(r.chamada.total_ms for r in res) / 1000.0
        for r in res:
            extras.append(r.chamada)
            resumos.append(r.resumo())
        ruim = next((r for r in res if r.motivo), None)
        if ruim is not None:
            return falha(ruim)
        pareceres = {PERFIL_DA_ETAPA[r.etapa]: seguro(r.saida)
                     for r in res if isinstance(r.saida, ParecerRisco)}
        debate = {"altista": argumentos["altista"], "baixista": argumentos["baixista"], "risco": pareceres}
        resumo_final: dict[str, Any] = {"executado": True, "rodadas": self.config.rodadas, "etapas": list(resumos)}
        if omitidos:
            resumo_final["textos_omitidos"] = omitidos

        # --- veto unanime do debate de risco: NAO_OPERAR sem custo de sintese ----------------------------------------
        if self.config.veto_unanime_risco and all(p["recomendacao"] == "nao_operar" for p in pareceres.values()):
            principal = res[-1].chamada
            p = PropostaIA(acao=Acao.NAO_OPERAR, incerteza=1.0,
                           justificativa_curta="debate: os tres pareceres de risco recomendam nao operar")
            resumo_final["veto_unanime_risco"] = True
            return PropostaObtida(p, principal, None, (), False, dono,
                                  chamadas_extras=tuple(c for c in extras if c is not principal),
                                  latencia_ms=gasto_s * 1000.0, extras_proposta={"debate": resumo_final})

        # --- fase 3: sintetizador (papel proponente) ------------------------------------------------------------------
        prazo = total_s - gasto_s
        if prazo <= 0:
            ch = ChamadaLLM(papel=self.papel, modelo_pedido=self.ref, erro="timeout", hash_dossie=ctx.hash_dossie)
            return falha(ResultadoEtapa("sintetizador", self.ref, None, ch, RESPOSTA_TARDIA))
        obt = await self._propor(ctx, modo, prazos, mensagem_sintetizador(ctx.dossie, modo, debate), prazo)
        lat_ms = gasto_s * 1000.0 + obt.chamada.total_ms
        obt.chamadas_extras = tuple(extras)
        obt.latencia_ms = lat_ms
        obt.extras_proposta = {"debate": resumo_final}
        if obt.proposta is not None and lat_ms / 1000.0 > total_s:
            return PropostaObtida(None, obt.chamada, RESPOSTA_TARDIA, (), False, dono, chamadas_extras=obt.chamadas_extras,
                                  latencia_ms=lat_ms, extras_proposta=obt.extras_proposta)
        return obt


class BracoDebate(BracoLLM):
    """Braco ``debate``: so no modo condicional. Com ``revisor`` vira o analogo do braco C (debate + revisor)."""

    familia = "debate"

    def __init__(self, id: str, proponente: ServicoPropostaDebate, revisor: ServicoRevisao | None = None,
                 modo: Modo = "condicional", geometria: Geometria | None = None,
                 prazos: Prazos | None = None) -> None:
        if modo != "condicional":
            raise ValueError("o braco debate so existe no modo condicional (latencia das etapas em serie)")
        super().__init__(id, proponente, revisor, modo, geometria, prazos)
        self.servico_debate = proponente

    def manifesto(self) -> dict[str, Any]:
        m = super().manifesto()
        m["debate"] = {
            "rodadas": self.servico_debate.config.rodadas,
            "veto_unanime_risco": self.servico_debate.config.veto_unanime_risco,
            "etapas": {n: {"modelo": e.ref, "effort": e.effort, "prompt_hash": e.prompt.hash,
                           "ferramentas": list(e.envelope.ferramentas), "internet": e.envelope.acesso_internet}
                       for n, e in sorted(self.servico_debate.etapas.items())},
        }
        return m
