"""Servicos de LLM (papeis plugaveis) e logica comum dos bracos B e C.

* ``ServicoProposta``: chama o papel ``proponente`` (qualquer ``provedor:modelo`` do catalogo) e VALIDA a
  resposta de forma deterministica (ia.validadores.validar_proposta). A chamada e COMPARTILHADA entre os
  bracos que apresentarem o MESMO dossie no mesmo ciclo (B e C: comparacao pareada) - uma so chamada, um so
  custo, atribuido ao dono do servico.
* ``ServicoRevisao``: chama o papel ``revisor`` (so ENTRADAS) e aplica ``revisao_aprova``.
* ``BracoLLM``: fluxo comum. ENCERRAR, MANTER e NAO_OPERAR NUNCA esperam revisao. Qualquer falha de IA
  (atraso, erro HTTP/429, recusa, JSON invalido, modelo servido fora da lista, orcamento esgotado, evidencia
  inexistente, divergencia) resulta em ``bloqueada``: nenhuma entrada; saidas e protecao seguem.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from robowin.core.tipos import Acao, CarimboLatencia, ChamadaLLM, Lado, PropostaIA, RevisaoIA
from robowin.ia.catalogo import dividir_modelo
from robowin.ia.dossie import hash_dossie, verificar_anonimizacao
from robowin.ia.prompts import Prompt, carregar_prompt, mensagem_proponente, mensagem_revisor
from robowin.ia.provedores.base import modelo_servido_aceito
from robowin.ia.validadores import revisao_aprova, validar_proposta

from ..plano_condicional import converter_plano
from ..validade import (
    DIVERGENCIA,
    PRAZOS_CONDICIONAL,
    PRAZOS_IMEDIATO,
    RESPOSTA_INVALIDA,
    RESPOSTA_TARDIA,
    SEM_TEMPO_REVISAO,
    Prazos,
    classificar_chamada,
)
from .base import NS, BracoBase, ContextoDecisao, Geometria, Modo, RegistroChamada, ResultadoDecisao

CAMPO_PROBABILIDADES = "probabilidades_do_plano"
LADO_DA_ACAO = {Acao.COMPRAR: Lado.COMPRA, Acao.VENDER: Lado.VENDA}


# --------------------------------------------------------------------------------------------------
# servico generico
# --------------------------------------------------------------------------------------------------
class ServicoLLM:
    """Um PAPEL ligado a ``provedor:modelo``. Nunca levanta excecao de chamada: devolve a falha."""

    def __init__(self, provedor: Any, ref: str, papel: str, schema: type[BaseModel], *, effort: str | None = None,
                 max_tokens: int = 2000, prompt: Prompt | None = None, diretorio_prompts: str | Path | None = None,
                 modelos_servidos_aceitos: tuple[str, ...] | None = None) -> None:
        self.provedor, self.ref, self.papel, self.schema = provedor, ref, papel, schema
        self.prov_nome, self.modelo_id = dividir_modelo(ref)
        self.effort, self.max_tokens = effort, max_tokens
        self.prompt = prompt or carregar_prompt(papel, provedor=self.prov_nome, diretorio=diretorio_prompts)
        self.aceitos = modelos_servidos_aceitos
        self.chamadas = 0

    @property
    def chave(self) -> str:
        return f"{self.ref}|{self.effort}|{self.prompt.hash[:12]}"

    async def chamar(self, mensagem: str, prazo_s: float, hash_dossie: str) -> tuple[BaseModel | None, ChamadaLLM, str | None]:
        self.chamadas += 1
        obj: BaseModel | None
        try:
            obj, ch = await self.provedor.gerar_estruturado(
                modelo=self.modelo_id, sistema=self.prompt.texto, mensagem=mensagem, schema=self.schema,
                prazo_s=prazo_s, effort=self.effort, max_tokens=self.max_tokens)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # ErroProvedor (pacote ausente), orcamento, rede: sempre vira falha tratada
            obj, ch = None, ChamadaLLM(papel=self.papel, modelo_pedido=self.ref,
                                       erro=f"erro_provedor:{type(e).__name__}")
        ch = ch.model_copy(update={"papel": self.papel, "modelo_pedido": self.ref, "hash_dossie": hash_dossie,
                                   "hash_prompt": self.prompt.hash})
        if ch.valida and not modelo_servido_aceito(self.modelo_id, ch.modelo_servido, self.aceitos):
            ch = ch.model_copy(update={"valida": False,
                                       "erro": f"modelo_servido_fora_da_lista:{ch.modelo_servido}"})
            obj = None
        if obj is not None and not isinstance(obj, self.schema):
            obj = None
        if obj is None and ch.valida:
            ch = ch.model_copy(update={"valida": False, "erro": ch.erro or "json_invalido"})
        return obj, ch, classificar_chamada(ch)


# --------------------------------------------------------------------------------------------------
# proposta (compartilhada) e revisao
# --------------------------------------------------------------------------------------------------
@dataclass
class PropostaObtida:
    proposta: PropostaIA | None
    chamada: ChamadaLLM
    motivo: str | None
    violacoes: tuple[str, ...]
    compartilhada: bool
    dono: str


@dataclass
class RevisaoObtida:
    revisao: RevisaoIA | None
    chamada: ChamadaLLM
    motivo: str | None


class ServicoProposta(ServicoLLM):
    def __init__(self, provedor: Any, ref: str, *, dono: str = "", stop_max_pontos: float, **kw: Any) -> None:
        super().__init__(provedor, ref, "proponente", PropostaIA, **kw)
        self._donos: dict[str, str] = {}
        if dono:
            self._donos[""] = dono
        self.stop_max_pontos = stop_max_pontos
        self._cache: dict[tuple[str, str, str], asyncio.Future[PropostaObtida]] = {}

    def registrar_braco(self, braco_id: str, modo: str = "") -> None:
        """O primeiro braco registrado por modo e o DONO das chamadas (a quem o custo e atribuido)."""
        self._donos.setdefault(modo, braco_id)

    def dono_de(self, modo: str) -> str:
        return self._donos.get(modo) or self._donos.get("", "")

    async def obter(self, ctx: ContextoDecisao, modo: Modo, prazos: Prazos) -> PropostaObtida:
        """Proposta validada. Mesmo (ciclo, modo, dossie) => UMA chamada para todos os bracos que a pedirem."""
        chave = (ctx.ciclo_id, modo, ctx.hash_dossie)
        fut = self._cache.get(chave)
        if fut is not None:
            return replace(await fut, compartilhada=True)
        fut = asyncio.get_running_loop().create_future()
        self._cache[chave] = fut
        while len(self._cache) > 16:  # descarta ciclos antigos
            self._cache.pop(next(iter(self._cache)))
        try:
            r = await self._buscar(ctx, modo, prazos)
        except BaseException:
            r = PropostaObtida(None, ChamadaLLM(papel=self.papel, modelo_pedido=self.ref, erro="cancelada"),
                               RESPOSTA_TARDIA, (), False, self.dono_de(modo))
            fut.set_result(r)
            raise
        fut.set_result(r)
        return r

    async def _buscar(self, ctx: ContextoDecisao, modo: Modo, prazos: Prazos) -> PropostaObtida:
        obj, ch, motivo = await self.chamar(mensagem_proponente(ctx.dossie, modo), prazos.prazo_proponente(),
                                            ctx.hash_dossie)
        if motivo or not isinstance(obj, PropostaIA):
            return PropostaObtida(None, ch, motivo or RESPOSTA_INVALIDA, (), False, self.dono_de(modo))
        v = validar_proposta(obj, ctx.dossie, modo=modo, validade_max_s=int(prazos.validade_s),
                             stop_max_pontos=self.stop_max_pontos)
        if not v.ok:
            # reprovada pela validacao deterministica: a chamada conta como INVALIDA (plano 5.10)
            ch = ch.model_copy(update={"valida": False, "erro": "proposta_invalida: " + "; ".join(v.violacoes)[:300]})
            return PropostaObtida(None, ch, RESPOSTA_INVALIDA, v.violacoes, False, self.dono_de(modo))
        return PropostaObtida(obj, ch, None, (), False, self.dono_de(modo))


class ServicoRevisao(ServicoLLM):
    def __init__(self, provedor: Any, ref: str, **kw: Any) -> None:
        super().__init__(provedor, ref, "revisor", RevisaoIA, **kw)

    async def revisar(self, ctx: ContextoDecisao, proposta: PropostaIA, prazo_s: float,
                      probabilidades: dict[str, Any] | None = None) -> RevisaoObtida:
        """``probabilidades``: campo ``probabilidades_do_plano`` (Maquina do Tempo), entregue ao revisor como
        evidencia do dossie da revisao. SIMULACAO, nao previsao. So entra se passar na verificacao de anonimizacao;
        o dossie do PROPONENTE (compartilhado, com hash proprio) nao e alterado."""
        dossie, h = ctx.dossie, ctx.hash_dossie
        if probabilidades is not None:
            extra = {CAMPO_PROBABILIDADES: probabilidades}
            if verificar_anonimizacao(extra, (ctx.preco_ref, ctx.bid, ctx.ask)):  # nunca vaza data nem nivel absoluto
                extra = {CAMPO_PROBABILIDADES: {"status": "indisponivel", "motivo": "anonimizacao_falhou"}}
            dossie = {**ctx.dossie, **extra}
            h = hash_dossie(dossie)
        obj, ch, motivo = await self.chamar(mensagem_revisor(dossie, proposta.model_dump(mode="json")), prazo_s, h)
        if motivo or not isinstance(obj, RevisaoIA):
            return RevisaoObtida(None, ch, motivo or RESPOSTA_INVALIDA)
        return RevisaoObtida(obj, ch, None)


# --------------------------------------------------------------------------------------------------
# braco LLM
# --------------------------------------------------------------------------------------------------
class BracoLLM(BracoBase):
    """Proposta do proponente com (C) ou sem (B) revisor. Variantes: imediato / condicional."""

    ia = True
    familia = "llm"

    def __init__(self, id: str, proponente: ServicoProposta, revisor: ServicoRevisao | None = None,
                 modo: Modo = "imediato", geometria: Geometria | None = None, prazos: Prazos | None = None) -> None:
        super().__init__(id, modo, geometria)
        self.proponente = proponente
        self.revisor = revisor
        self.prazos = prazos or (PRAZOS_IMEDIATO if modo == "imediato" else PRAZOS_CONDICIONAL)
        proponente.registrar_braco(id, modo)

    @property
    def grupo_ciclo(self) -> str:
        return f"llm:{self.proponente.chave}:{self.modo}"

    def manifesto(self) -> dict[str, Any]:
        m = super().manifesto()
        m["proponente"] = {"modelo": self.proponente.ref, "effort": self.proponente.effort,
                           "prompt_hash": self.proponente.prompt.hash}
        m["revisor"] = (None if self.revisor is None else
                        {"modelo": self.revisor.ref, "effort": self.revisor.effort,
                         "prompt_hash": self.revisor.prompt.hash})
        m["prazos"] = {"claude_s": self.prazos.claude_s, "gpt_s": self.prazos.gpt_s,
                       "validade_s": self.prazos.validade_s, "desvio_max_ticks": self.prazos.desvio_max_ticks,
                       "margem_s": self.prazos.margem_s}
        return m

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        pz = self.prazos
        kw: dict[str, Any] = {"validade_s": pz.validade_s, "desvio_max_pts": pz.desvio_max_ticks * ctx.tick_pontos}
        obt = await self.proponente.obter(ctx, self.modo, pz)
        lat_prop_ns = int(obt.chamada.total_ms * 1e6)
        chamadas = [RegistroChamada(obt.chamada, obt.dono, "proponente", compartilhada=obt.compartilhada)]
        carimbos = [CarimboLatencia(etapa="proposta_ia", braco=self.id, t_ini_ns=ctx.t0_ns,
                                    t_fim_ns=ctx.t0_ns + lat_prop_ns, injetado=obt.chamada.modelo_servido.endswith("FALSO"))]
        kw.update(chamadas=chamadas, carimbos=carimbos, latencia_ns=lat_prop_ns)
        if obt.proposta is None:
            motivos = (f"ia:{obt.motivo}", *obt.violacoes)
            return self._bloqueada(ctx, motivos, **kw)
        p = obt.proposta
        proposta_json = p.model_dump(mode="json") | {"compartilhada": obt.compartilhada, "dono_chamada": obt.dono}
        kw["proposta"] = proposta_json

        # --- acoes que NUNCA esperam revisao ----------------------------------------------------------------
        if p.acao == Acao.NAO_OPERAR:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", **kw)
        if p.acao == Acao.MANTER:
            it = self._intencao(ctx, Acao.MANTER, stop=p.stop_pontos, alvo=None, tempo_max_s=None,
                                validade_ns=ctx.t0_ns + int(pz.validade_s * NS))
            return self._resultado(ctx, Acao.MANTER, "manter", intencao=it, **kw)
        if p.acao == Acao.ENCERRAR:
            it = self._intencao(ctx, Acao.ENCERRAR, stop=None, alvo=None, tempo_max_s=None,
                                validade_ns=ctx.t0_ns + int(pz.validade_s * NS))
            return self._resultado(ctx, Acao.ENCERRAR, "saida", intencao=it, **kw)

        # --- entradas ------------------------------------------------------------------------------------------
        if (ctx.posicao.quantidade or 0) > 0:  # a proposta pareada nasceu do dossie do lider; este braco ja tem posicao
            return self._bloqueada(ctx, ("posicao_aberta_do_braco",), acao=p.acao, **kw)
        lat_s = lat_prop_ns / NS
        if self.modo == "imediato" and lat_s > pz.validade_s - pz.margem_s:
            return self._bloqueada(ctx, (f"ia:{RESPOSTA_TARDIA}", "sem_margem_para_risco_e_oms"), acao=p.acao, **kw)
        if self.modo == "condicional" and p.plano is None:
            return self._bloqueada(ctx, ("sem_plano_condicional",), acao=p.acao, **kw)
        prob: dict[str, Any] | None = None
        if ctx.odds is not None and p.acao in LADO_DA_ACAO and p.stop_pontos and p.alvo_pontos:
            try:  # Maquina do Tempo: evidencia para o revisor; falha => indisponivel (nunca bloqueia nem aprova)
                av = ctx.odds(ctx, LADO_DA_ACAO[p.acao], p.stop_pontos, p.alvo_pontos)
                prob = av.campo
            except Exception as e:
                av = None
                prob = {"status": "indisponivel", "motivo": f"falha:{type(e).__name__}"}
            proposta_json[CAMPO_PROBABILIDADES] = prob
            if av is not None and av.veto:  # [decisao].veto_por_odds = true (desligado por padrao)
                return self._bloqueada(ctx, (av.motivo_bloqueio,), acao=p.acao, **kw)
        if self.revisor is not None:  # braco C: revisao SO de entradas, antes de armar/executar
            prazo = pz.prazo_revisor(lat_s)
            if prazo <= 0:
                return self._bloqueada(ctx, (f"ia:{SEM_TEMPO_REVISAO}",), acao=p.acao, **kw)
            rev = await self.revisor.revisar(ctx, p, prazo, probabilidades=prob)
            lat_rev_ns = int(rev.chamada.total_ms * 1e6)
            chamadas.append(RegistroChamada(rev.chamada, self.id, "revisor"))
            carimbos.append(CarimboLatencia(etapa="revisao_ia", braco=self.id, t_ini_ns=ctx.t0_ns + lat_prop_ns,
                                            t_fim_ns=ctx.t0_ns + lat_prop_ns + lat_rev_ns,
                                            injetado=rev.chamada.modelo_servido.endswith("FALSO")))
            kw["latencia_ns"] = lat_prop_ns + lat_rev_ns
            if rev.revisao is not None:
                kw["revisao"] = rev.revisao.model_dump(mode="json")
            if rev.motivo:
                return self._bloqueada(ctx, (f"ia:{rev.motivo}",), acao=p.acao, **kw)
            if not revisao_aprova(rev.revisao):
                return self._bloqueada(ctx, (DIVERGENCIA,), acao=p.acao, **kw)
        validade_s = min(float(p.validade_s), pz.validade_s)
        g = self.geometria
        plano = None
        if self.modo == "condicional":
            assert p.plano is not None
            plano = converter_plano(p.plano, ctx.t0_ns)
            validade_ns = plano.expira_ns
            kw["plano"] = plano
        else:
            validade_ns = ctx.t0_ns + int(validade_s * NS)
            kw["validade_s"] = validade_s
        it = self._intencao(ctx, p.acao, stop=p.stop_pontos, alvo=p.alvo_pontos, tempo_max_s=g.tempo_max_s,
                            validade_ns=validade_ns, plano=plano)
        return self._resultado(ctx, p.acao, "plano" if plano is not None else "entrada", intencao=it, **kw)
