"""Sessao completa em tempo simulado sobre dados SINTETICOS (``robowin simular``).

*** TUDO AQUI E SIMULACAO SOBRE DADOS SINTETICOS: nenhum resultado prova lucratividade. ***

* Dados: ``robowin.dados.sintetico`` (um pregao para treinar o modelo de microestrutura e o StatArb, outro
  para a sessao). O modelo treinado em dados sinteticos NUNCA vira ``validado_oos`` (selo recusado), logo o
  braco quant opera como EXPLORATORIO, rotulado.
* IA: provedor ``falso`` por padrao (``ProvedorFalsoSessao``: propostas/revisoes deterministicas lidas do
  dossie, latencia e falhas injetadas com seed; tudo rotulado ``-FALSO``). ``--provedor real`` usa os SDKs
  oficiais, so se houver chaves de API no ambiente.
* Saida: banco SQLite (esquema congelado), auditoria com cadeia de hash e resumo rotulado.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel

from robowin.avaliacao.estatistica import n_expectativa
from robowin.avaliacao.maquina_do_tempo import PiscinaSessoes, barras_de_ticks
from robowin.core.config import ErroConfig, carregar_toml, sha256_arquivo
from robowin.core.instrumentos import PerfilInstrumento, perfil_da_config, perfil_de_instrumento
from robowin.core.protocolos import ModeloPreditivo, SinalStatArb
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    Condicao,
    Instrumento,
    LivroSnapshot,
    ManifestoExperimento,
    PlanoCondicional,
    PropostaIA,
    RevisaoIA,
    Tick,
)
from robowin.dados.banco import BancoSQLite
from robowin.dados.sintetico import ConfigSintetico, DadosSinteticos, gerar_dias
from robowin.features.pipeline import PipelineFeatures
from robowin.ia.catalogo import Catalogo, dividir_modelo
from robowin.ia.esquemas import ArgumentoDebate, ParecerRisco, ReflexaoIA
from robowin.ia.orcamento import OrcamentoIA, ProvedorComOrcamento
from robowin.ia.papeis import carregar_papeis, validar_papeis
from robowin.ia.prompts import hashes_prompts
from robowin.ia.provedores.base import Medidor, RespostaBruta
from robowin.ia.provedores.falso import ProvedorFalso
from robowin.ia.reflexao import MemoriaReflexiva
from robowin.modelos.demo_sintetico import montar_amostras
from robowin.modelos.microestrutura import CriterioOOS, ModeloMicroestrutura
from robowin.modelos.rotulagem import ConfigBarreiras
from robowin.modelos.rotulagem import Custos as CustosRotulagem
from robowin.risco.politica import ERRO_APROVACAO_PENDENTE, stop_maximo_efetivo_pontos
from robowin.risco.tempo import dia_sp
from robowin.statarb.sinais import SinalStatArbPadrao

from .bracos.base import BracoBase, Geometria, Modo
from .bracos.controles import BracoAleatorio, BracoNaoOperar
from .bracos.fabrica import FabricaBracos, extras_de_config
from .bracos.llm import ServicoLLM
from .bracos.quant import BracoQuant
from .gatilhos import ConfigGatilhos
from .odds import AvaliadorOdds, config_odds
from .orquestrador import ConfigOrquestrador, Orquestrador, geometria_de
from .registro import Registro
from .validade import BloqueioDados, prazos_de_config

NS = 1_000_000_000
RAIZ_CONFIG = Path(__file__).resolve().parents[3] / "config"
ROTULO_SESSAO = "SIMULACAO / SINTETICO - dados gerados por simulacao; NAO sao dados de mercado"


# ==================================================================================================
# provedor FALSO da sessao
# ==================================================================================================
def proposta_falsa(dossie: dict[str, Any], modo: str) -> PropostaIA:
    """Regra FALSA e deterministica (so para exercitar o encanamento; nao e estrategia): segue o movimento de
    60 s quando o spread e curto e gerencia a posicao aberta por resultado e tempo."""
    pos = dossie.get("posicao") or {}
    mov = dossie.get("movimento") or {}
    spread = (dossie.get("microestrutura") or {}).get("spread_ticks")
    if (pos.get("quantidade") or 0) > 0:
        res = pos.get("resultado_pontos") or 0.0
        if res <= -20 or (pos.get("tempo_aberta_s") or 0) > 240:
            return PropostaIA(
                acao=Acao.ENCERRAR, evidencias_a_favor=("posicao.resultado_pontos negativo ou tempo longo",),
                justificativa_curta="[FALSO] encerra por resultado ou tempo")
        return PropostaIA(acao=Acao.MANTER, justificativa_curta="[FALSO] mantem")
    ret = mov.get("ret_60s_pts")
    if ret is None or spread is None or abs(ret) < 10 or spread > 2:
        return PropostaIA(acao=Acao.NAO_OPERAR, incerteza=0.8, justificativa_curta="[FALSO] sem sinal")
    compra = ret > 0
    plano = None
    if modo == "condicional":
        gat = (Condicao(tipo="preco_toca", valor=-10.0 if compra else 10.0),
               Condicao(tipo="spread_max_pontos", valor=10.0))
        inv = (Condicao(tipo="preco_rompe_acima" if compra else "preco_rompe_abaixo", valor=30.0 if compra else -30.0),)
        plano = PlanoCondicional(gatilhos=gat, invalidacoes=inv, expira_ns=240 * NS)
    return PropostaIA(
        acao=Acao.COMPRAR if compra else Acao.VENDER,
        hipoteses=("[FALSO] continuacao do movimento recente",),
        evidencias_a_favor=(f"movimento.ret_60s_pts = {ret}",),
        evidencias_contra=("microestrutura.spread_ticks pode alargar",),
        riscos=("reversao",), incerteza=0.55, stop_pontos=30.0, alvo_pontos=40.0, validade_s=60,
        invalidacoes=("movimento.ret_60s_pts muda de sinal",), justificativa_curta="[FALSO] momentum de 60 s",
        plano=plano)


def argumento_falso(dossie: dict[str, Any], lado: str) -> ArgumentoDebate:
    """Pesquisador FALSO: cita campos que existem no dossie; a forca vem do movimento de 60 s (so encanamento)."""
    ret = (dossie.get("movimento") or {}).get("ret_60s_pts")
    a_favor = ret is not None and ((ret > 0) == (lado == "altista"))
    return ArgumentoDebate(
        lado="altista" if lado == "altista" else "baixista",
        tese=f"[FALSO] leitura {lado} pelo movimento de 60 s",
        evidencias=(f"movimento.ret_60s_pts = {ret}",),
        pontos_fracos_do_oponente=("microestrutura.spread_ticks pode alargar",), forca=0.7 if a_favor else 0.3)


def parecer_falso(dossie: dict[str, Any], perfil: str) -> ParecerRisco:
    """Debatedor de risco FALSO: o conservador so aceita spread curto e movimento claro; o agressivo opera mais."""
    spread = (dossie.get("microestrutura") or {}).get("spread_ticks")
    ret = (dossie.get("movimento") or {}).get("ret_60s_pts")
    claro = ret is not None and abs(ret) >= 10 and spread is not None and spread <= 2
    ok = claro or (perfil == "agressivo" and ret is not None and abs(ret) >= 5)
    return ParecerRisco(
        perfil="agressivo" if perfil == "agressivo" else ("conservador" if perfil == "conservador" else "neutro"),
        recomendacao="operar" if ok else "nao_operar", riscos=("microestrutura.spread_ticks pode alargar",),
        evidencias=(f"movimento.ret_60s_pts = {ret}",), justificativa_curta="[FALSO] parecer de demonstracao")


def revisao_falsa(mensagem: str, dossie: dict[str, Any]) -> RevisaoIA:
    """Revisor FALSO: aprova entradas com spread curto, rejeitando ~25% por um hash da mensagem (determinista)."""
    spread = (dossie.get("microestrutura") or {}).get("spread_ticks")
    h = int(hashlib.sha256(mensagem.encode()).hexdigest()[:4], 16)
    ok = spread is not None and spread <= 2 and h % 4 != 0
    return RevisaoIA(veredito="APROVAR" if ok else "REJEITAR", justificativa_curta="[FALSO] revisor de demonstracao")


class ProvedorFalsoSessao(ProvedorFalso):
    """FALSO para a simulacao em tempo simulado: respostas lidas do dossie, latencia e falhas INJETADAS
    (seed), todas rotuladas. ``nome_emulado`` faz o adaptador valer como 'anthropic', 'openai'... no catalogo."""

    def __init__(self, nome_emulado: str, *, seed: int = 0, mediana_proponente_s: float = 6.0,
                 mediana_revisor_s: float = 2.5, sigma: float = 0.5, p_timeout: float = 0.0, p_http: float = 0.0,
                 **kw: Any) -> None:
        super().__init__(**kw)
        self.nome = nome_emulado
        self._rng = random.Random(f"{seed}:{nome_emulado}")
        self.mediana_proponente_s, self.mediana_revisor_s, self.sigma = mediana_proponente_s, mediana_revisor_s, sigma
        self.p_timeout, self.p_http = p_timeout, p_http
        self._modo = "imediato"

    async def _executar(self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
                        effort: str | None, max_tokens: int, medidor: Medidor) -> RespostaBruta:
        if schema is PropostaIA:
            modo = mensagem.split("MODO:", 1)[1].split()[0] if "MODO:" in mensagem else "imediato"
            obj: BaseModel = proposta_falsa(_dossie_da_mensagem(mensagem), modo)
        elif schema is RevisaoIA:
            obj = revisao_falsa(mensagem, _dossie_da_mensagem(mensagem))
        elif schema is ArgumentoDebate:
            obj = argumento_falso(_dossie_da_mensagem(mensagem), mensagem.split("LADO:", 1)[1].split()[0])
        elif schema is ParecerRisco:
            obj = parecer_falso(_dossie_da_mensagem(mensagem), mensagem.split("PERFIL:", 1)[1].split()[0])
        elif schema is ReflexaoIA:
            obj = ReflexaoIA(licao="FALSO: uma unica operacao e amostra minima; manter a cautela.", vies="mais_cautela")
        else:
            obj = self._gerador(schema)
        self._roteiro, self._i = [obj], 0
        return await super()._executar(modelo=modelo, sistema=sistema, mensagem=mensagem, schema=schema,
                                       effort=effort, max_tokens=max_tokens, medidor=medidor)

    async def gerar_estruturado(self, **kw: Any) -> tuple[BaseModel | None, Any]:
        obj, ch = await super().gerar_estruturado(**kw)
        mediana = (self.mediana_revisor_s if kw["schema"] in (RevisaoIA, ArgumentoDebate, ParecerRisco, ReflexaoIA)
                   else self.mediana_proponente_s)
        lat_s = mediana * float(np.exp(self.sigma * self._rng.gauss(0, 1)))
        u = self._rng.random()
        prazo = float(kw["prazo_s"])
        if u < self.p_timeout or lat_s > prazo:
            return None, ch.model_copy(update={"valida": False, "erro": "timeout", "total_ms": prazo * 1000,
                                               "ttft_ms": 0.0})
        if u < self.p_timeout + self.p_http:
            return None, ch.model_copy(update={"valida": False, "erro": "http_429", "total_ms": lat_s * 200})
        return obj, ch.model_copy(update={"total_ms": lat_s * 1000, "ttft_ms": lat_s * 400})


def _dossie_da_mensagem(mensagem: str) -> dict[str, Any]:
    i = mensagem.index("{")
    obj, _ = json.JSONDecoder().raw_decode(mensagem[i:])
    return dict(obj)


# ==================================================================================================
# dados, modelo e statarb (sinteticos)
# ==================================================================================================
NIVEL_VISTA = 100.0  # nivel de preco do ativo de referencia sintetico (independe do ativo operado)


def fator_vista(preco_ref: float) -> float:
    """Escala que leva o preco do ativo (nivel `preco_ref` do perfil) ao nivel do ativo de referencia sintetico."""
    return preco_ref / NIVEL_VISTA


def vista_sintetica(precos: np.ndarray, seed: int, phi: float = 0.98, sigma: float = 0.02,
                    fator: float = 1000.0) -> np.ndarray:
    """Ativo de referencia SINTETICO cointegrado com o ativo operado (preco/fator + ruido AR(1)) p/ exercitar o StatArb."""
    rng = np.random.default_rng(seed)
    u = np.zeros(len(precos))
    e = rng.normal(0, sigma, len(precos))
    for i in range(1, len(precos)):
        u[i] = phi * u[i - 1] + e[i]
    return precos / fator + u


class VistaSintetica:
    """Fornece o preco do ativo de referencia SINTETICO a cada consulta (estado AR(1) com seed)."""

    def __init__(self, seed: int, nome: str, fator: float, phi: float = 0.98, sigma: float = 0.02) -> None:
        self._rng = np.random.default_rng(seed)
        self.nome, self.fator = nome, fator
        self.phi, self.sigma, self._u = phi, sigma, 0.0

    def __call__(self, tick: Tick) -> dict[str, float]:
        self._u = self.phi * self._u + float(self._rng.normal(0, self.sigma))
        m = tick.meio if tick.meio > 0 else tick.last
        return {self.nome: m / self.fator + self._u}


@dataclass
class ParamsSimulacao:
    seed: int = 7
    n_ticks: int = 12_000  # por pregao sintetico (um para treino, um para a sessao)
    sinal_forca: float = 0.05
    saida: Path = Path("dados_locais/simulacao")
    provedor: str = "falso"  # falso | real
    config: Path = field(default_factory=lambda: RAIZ_CONFIG / "robowin.toml")
    catalogo: Path = field(default_factory=lambda: RAIZ_CONFIG / "modelos.toml")
    risco: Path = field(default_factory=lambda: RAIZ_CONFIG / "risco.toml")
    aprovacao: Path = field(default_factory=lambda: RAIZ_CONFIG / "aprovacoes" / "risco-v1.toml")
    theta_brl: float = 0.0
    com_condicional: bool = True
    p_timeout: float = 0.03
    p_http: float = 0.02
    passo_s: float = 15.0
    checagem_s: float = 15.0
    idade_max_dado_s: float = 60.0  # o pregao sintetico e esparso; ao vivo o padrao do BloqueioDados e 15 s
    periodo_ia_s: float = 240.0
    tipo_modelo: str = "logistica"
    arquivo_emergencia: Path | None = None
    sigma_brl: float = 35.0  # desvio-padrao conservador do liquido por operacao (R$) p/ dimensionar n_alvo
    mde_brl: float = 5.0  # menor efeito de interesse na expectativa por operacao (R$)
    ativo: str | None = None  # simbolo_base do perfil (config/instrumentos.toml); None => [geral].simbolo_base
    registro_modelos: Path | None = None  # registro de modelos do ativo: carrega o artefato em vez de treinar
    odds_sessoes: int = 0  # > 0: gera N pregoes sinteticos ANTERIORES e liga a Maquina do Tempo (evidencia ao revisor)
    # limites por USUARIO + CONTA (risco.usuarios); ambos None => modo arquivo unico (risco/aprovacao acima)
    usuario: str | None = None
    conta_negociacao: str | None = None  # PLATAFORMA:SERVIDOR:LOGIN
    dir_usuarios: Path | None = None


@dataclass
class Sessao:
    orq: Orquestrador
    banco: BancoSQLite
    ticks: list[Tick]
    livros: list[LivroSnapshot]
    manifesto: ManifestoExperimento
    dados: DadosSinteticos
    caminho_banco: Path
    dir_auditoria: Path
    modelo: ModeloPreditivo
    rotulo_dados: str


def treinar_modelo(treino: DadosSinteticos, perfil: PerfilInstrumento, tipo: str = "logistica") -> ModeloMicroestrutura:
    """Treina o modelo de microestrutura em dados SINTETICOS (status final: em_validacao; nunca validado_oos).

    O modelo nasce marcado com o ATIVO do perfil e so serve a ele."""
    am = montar_amostras(list(treino.ticks), list(treino.livros), ConfigBarreiras.do_perfil(perfil),
                         CustosRotulagem.do_perfil(perfil), passo_s=15)
    m = ModeloMicroestrutura(tipo=tipo, nome=f"microestrutura_{tipo}")
    m.treinar(am, origem="sintetico", criterio=CriterioOOS())
    m.ativo = perfil.simbolo_base
    return m


def _modelo_persistido(p: ParamsSimulacao, perfil: PerfilInstrumento) -> ModeloMicroestrutura | None:
    """Modelo do registro do ativo (campeao, ou exploratorio rotulado). Artefato invalido => ErroConfig: nada de
    cair em silencio num modelo treinado na hora quando o usuario pediu o persistido."""
    if p.registro_modelos is None:
        return None
    from robowin.modelos.persistencia import carregar_para_ativo
    res = carregar_para_ativo(p.registro_modelos, f"microestrutura_{p.tipo_modelo}", perfil.simbolo_base, perfil.hash())
    if res.modelo is None:
        raise ErroConfig(f"modelo persistido recusado: {res.motivo}")
    return res.modelo


def nome_referencia(perfil: PerfilInstrumento) -> str:
    return f"REF_{perfil.simbolo_base}"


def treinar_statarb(treino: DadosSinteticos, perfil: PerfilInstrumento, seed: int) -> SinalStatArbPadrao:
    p = np.array([(t.meio if t.meio > 0 else t.last) for t in treino.ticks][::300], dtype=float)  # ~passo de 5 min
    ref = nome_referencia(perfil)
    sa = SinalStatArbPadrao(alvo=perfil.simbolo_base, referencias=(ref,))
    sa.treinar({perfil.simbolo_base: p, ref: vista_sintetica(p, seed, fator=fator_vista(perfil.preco_sintetico_ref))})
    return sa


def carregar_ambiente(p: ParamsSimulacao) -> tuple[dict[str, Any], Catalogo]:
    from robowin.risco.usuarios import validar_ia_sem_limites
    cfg = carregar_toml(p.config) if Path(p.config).exists() else {}
    validar_ia_sem_limites(cfg)  # a configuracao de IA nunca define/altera limites de risco
    return cfg, Catalogo.carregar(p.catalogo)


def montar_provedores(p: ParamsSimulacao, cfg: dict[str, Any], catalogo: Catalogo, orc: OrcamentoIA,
                      refs: list[str]) -> tuple[dict[str, Any], bool]:
    """Um provedor por NOME de provedor usado (cada um com orcamento e catalogo). ``falso`` por padrao."""
    nomes = sorted({dividir_modelo(r)[0] for r in refs})
    out: dict[str, Any] = {}
    real = p.provedor == "real"
    for n in nomes:
        if real:
            from robowin.ia.registro_provedores import ErroRegistro, registro_padrao
            reg = registro_padrao()
            try:
                if not reg.chave_presente(n):
                    raise ErroConfig(f"{reg.mensagem_sem_chave(n)} (ou use --provedor falso)")
                interno = reg.criar(n, catalogo=catalogo)
            except ErroRegistro as e:
                raise ErroConfig(str(e)) from e
        else:
            interno = ProvedorFalsoSessao(n, seed=p.seed, catalogo=catalogo, p_timeout=p.p_timeout, p_http=p.p_http)
        out[n] = ProvedorComOrcamento(interno, orc, catalogo)
    return out, real


def montar_reflexao(cfg: Mapping[str, Any], fab: FabricaBracos, provedores: Mapping[str, Any], *,
                    ativo: bool = True) -> tuple[MemoriaReflexiva | None, ServicoLLM | None]:
    """Memoria reflexiva de ``[reflexao]`` (desligada por padrao). Papel e modelo vem de ``[ia.papeis]``."""
    sec = cfg.get("reflexao", {}) or {}
    if not sec.get("ativo", False) or not ativo:
        return None, None
    ref, eff = fab.resolver(str(sec.get("papel", "reflexao")), None)
    prov, _ = dividir_modelo(ref)
    serv = ServicoLLM(provedores[prov], ref, "reflexao", ReflexaoIA, effort=eff, max_tokens=300)
    mem = MemoriaReflexiva(float(sec.get("atraso_liquidacao_s", 300.0)), int(sec.get("max_reflexoes", 3)))
    return mem, serv


ROTULO_CONTAMINADO_CORTE = "contaminado_corte_desconhecido"


def rotular_declaracoes(bracos: list[BracoBase], catalogo: Catalogo, hashes: dict[str, str]) -> set[str]:
    """Marca no manifesto (``hashes``) os modelos declarados com preco/corte desconhecido. Devolve os ids dos
    bracos com modelo SEM data de corte (contaminados em qualquer backtest historico)."""
    contaminados: set[str] = set()
    for b in bracos:
        m = b.manifesto()
        refs = [m[p]["modelo"] for p in ("proponente", "revisor") if m.get(p)]
        refs += [d["modelo"] for d in (m.get("debate") or {}).get("etapas", {}).values()]
        for ref in refs:
            try:
                rot = catalogo.rotulos(ref)
            except Exception:
                continue
            if rot:
                hashes[f"rotulo:{ref}"] = ",".join(rot)
            if catalogo.contaminado_em_historico([ref]):
                contaminados.add(b.id)
                hashes[f"contaminacao:{b.id}"] = "corte_desconhecido:so_avaliacao_prospectiva"
    return contaminados


def montar_manifesto(sessao_id: str, bracos: list[BracoBase], catalogo: Catalogo, hashes: dict[str, str],
                     origem_dados: str = "sinteticos", sigma_brl: float = 35.0, mde_brl: float = 5.0,
                     ativo: str = "") -> ManifestoExperimento:
    modelos: dict[str, str] = {}
    fixavel: dict[str, bool] = {}
    for b in bracos:
        m = b.manifesto()
        if m.get("modelo"):
            modelos[f"{b.id}.modelo"] = str(m["modelo"])
        for papel in ("proponente", "revisor"):
            if m.get(papel):
                ref = m[papel]["modelo"]
                modelos[f"{b.id}.{papel}"] = ref
                try:
                    fixavel[ref] = bool(catalogo.obter(ref).fixavel)
                except Exception:
                    fixavel[ref] = False
        for etapa, d in (m.get("debate") or {}).get("etapas", {}).items():  # etapas do braco de debate
            modelos[f"{b.id}.debate.{etapa}"] = d["modelo"]
            try:
                fixavel[d["modelo"]] = bool(catalogo.obter(d["modelo"]).fixavel)
            except Exception:
                fixavel[d["modelo"]] = False
    hipoteses: tuple[str, ...] = ("H1: o braco com IA tem expectativa liquida maior que o controle aleatorio",
                 "H2: o revisor (C) melhora a expectativa liquida sobre a proposta sem revisor (B)",
                 "H3: o modo condicional difere do imediato")
    if any(b.familia == "debate" for b in bracos):
        hipoteses += ("H4 (experimental): o debate (pesquisadores + risco + sintese) difere do proponente unico "
                      "no modo condicional, com Holm e custo/latencia",)
    return ManifestoExperimento(
        id=sessao_id,
        hipoteses=hipoteses,
        metrica_primaria="expectativa_liquida_por_operacao_brl", mde_brl=mde_brl,
        n_alvo=n_expectativa(sigma_brl, mde_brl, alfa=0.05),
        regras_parada=("kill switch", "perda diaria", "orcamento de IA esgotado"),
        modelos=modelos, fixavel=fixavel, hashes=hashes, origem_dados=origem_dados, ativo=ativo)


def selecao_risco(p: ParamsSimulacao) -> Any:
    """Usuario + conta ativos (`--usuario/--conta` ou [geral].usuario/conta_negociacao); inativa = arquivo unico."""
    from robowin.risco.usuarios import selecao_de
    return selecao_de(p.config, p.usuario, p.conta_negociacao, p.dir_usuarios)


def carregar_risco_simulacao(p: ParamsSimulacao, perfil: PerfilInstrumento, *, ao_vivo: bool = False
                             ) -> tuple[Any, str | None]:
    """Politica de risco APROVADA do ATIVO (hash conferido) do usuario+conta ativos ou, sem usuario, do arquivo
    unico. Devolve (config, erro).

    Ao vivo o erro se propaga (`ErroConfig`): nunca opera com padrao nem com limites de outro usuario/conta. Na
    simulacao cai nos limites padrao/minimos (no modo usuario: os do proprio usuario ou o modelo conservador), mas
    devolve o erro para que o chamador registre incidente CRITICO e rotule `risco_nao_aprovado`."""
    from robowin.risco.usuarios import risco_para
    cfg, erro, _ = risco_para(selecao_risco(p), perfil, p.risco, p.aprovacao, ao_vivo=ao_vivo)
    return cfg, erro


def arquivo_risco_aplicado(p: ParamsSimulacao) -> Path:
    sel = selecao_risco(p)
    if not sel.ativa:
        return Path(p.risco)
    return sel.armazem().conta(sel.usuario, sel.chave).risco


@dataclass
class Montagem:
    orq: Orquestrador
    manifesto: ManifestoExperimento
    bracos: list[BracoBase]
    orcamento: OrcamentoIA
    provedores: dict[str, Any]
    real: bool


def construir_orquestrador(
    p: ParamsSimulacao, *, rel: Any, registro: Registro, ins: Instrumento, modelo: ModeloPreditivo,
    statarb: SinalStatArb | None, precos_aux: Any, sintetico: bool, modo_tempo: str, sessao_id: str,
    t_ref_ns: int, cfg: dict[str, Any] | None = None, catalogo: Catalogo | None = None,
    piscina_odds: PiscinaSessoes | None = None,
) -> Montagem:
    """Bracos (A, B, C, controles e extras de ``[[bracos]]``), provedores com orcamento e orquestrador."""
    if cfg is None or catalogo is None:
        cfg, catalogo = carregar_ambiente(p)
    papeis = carregar_papeis(cfg) if cfg.get("ia", {}).get("papeis") else {}
    if papeis:
        validar_papeis(papeis, catalogo)
    perfil = perfil_de_instrumento(ins)
    if modo_tempo == "real":
        perfil.exigir_confirmado()  # ao vivo: perfil a_confirmar BLOQUEIA
    risco, erro_risco = carregar_risco_simulacao(p, perfil, ao_vivo=modo_tempo == "real")
    orc = OrcamentoIA(float(cfg.get("ia", {}).get("orcamento_mensal_usd", 400.0)), ativo=perfil.simbolo_base)
    orc.iniciar_pregao(dia_sp(t_ref_ns).isoformat(), 21)
    pz = prazos_de_config(cfg)
    g = geometria_de(risco, ins, perfil.stop_padrao_pontos, perfil.alvo_padrao_pontos, 60)
    modos: tuple[Modo, ...] = ("imediato", "condicional") if p.com_condicional else ("imediato",)
    refs = [pp.modelo for pp in papeis.values()] + [
        r for e in extras_de_config(cfg)
        for r in (e.proponente, e.revisor, e.pesquisador, e.debatedor_risco) if r and ":" in r]
    provedores, real = montar_provedores(p, cfg, catalogo, orc, refs)
    fab = FabricaBracos(provedores, catalogo=catalogo, papeis=papeis, prazos=pz,
                        stop_max_pontos=stop_maximo_efetivo_pontos(risco, ins.ponto_valor_brl),
                        geometria=Geometria(g.stop_pontos, g.alvo_pontos, 300), cfg=cfg)
    bracos: list[BracoBase] = []
    for modo in modos:
        suf = "" if modo == "imediato" else "_cond"
        bracos.append(BracoQuant(f"quant{suf}", modo, modelo=modelo.nome, theta_brl=p.theta_brl, geometria=g))
    if papeis:
        bracos += fab.padrao(modos)
    bracos += fab.extras(cfg)
    bracos.append(BracoNaoOperar())
    for modo in modos:
        suf = "" if modo == "imediato" else "_cond"
        bracos.append(BracoAleatorio(f"controle_aleatorio{suf}", modo, seed=p.seed, geometrias=(g,)))
    hashes = {**{f"prompt:{k}": v for k, v in hashes_prompts().items()}, "perfil": perfil.hash()}
    contaminados = rotular_declaracoes(bracos, catalogo, hashes)
    memoria_reflexiva, servico_reflexao = montar_reflexao(cfg, fab, provedores, ativo=bool(papeis))
    if memoria_reflexiva is not None and servico_reflexao is not None:
        hashes["reflexao"] = f"ativa:{servico_reflexao.chave}"
    sel = selecao_risco(p)
    if sel.ativa:  # de quem sao os limites (nunca o token, so usuario e conta)
        hashes["risco_usuario"] = str(sel.usuario)
        hashes["risco_conta"] = sel.chave.texto
    arq_risco = arquivo_risco_aplicado(p)
    if (erro_risco is None or erro_risco == ERRO_APROVACAO_PENDENTE) and arq_risco.is_file():
        hashes["risco"] = _hash_arquivo(arq_risco)  # o arquivo foi o aplicado (aprovacao pendente fica registrada)
        if erro_risco is not None:
            hashes["risco_aprovacao"] = "pendente"
    else:  # o hash do arquivo NAO foi o aplicado (valeu o padrao): nao gravar como se fosse
        hashes["risco"] = ""
        hashes["risco_aplicado"] = "padrao_nao_aprovado"
    manifesto = montar_manifesto(sessao_id, bracos, catalogo, hashes,
                                 origem_dados="sinteticos" if sintetico else "desconhecida",
                                 sigma_brl=p.sigma_brl, mde_brl=p.mde_brl, ativo=perfil.simbolo_base)
    cfg_orq = ConfigOrquestrador(
        instrumento=ins, risco=risco, sessao_id=sessao_id, sintetico=sintetico,
        rotulo=ROTULO_SESSAO if sintetico else "SIMULACAO", seed=p.seed, modo_tempo=modo_tempo,
        passo_s=p.passo_s, checagem_s=p.checagem_s, arquivo_emergencia=p.arquivo_emergencia,
        gatilhos=ConfigGatilhos(periodo_s=p.periodo_ia_s, e_brl_min=1.0, z_statarb_min=2.0))
    orq = Orquestrador(cfg_orq, relogio=rel, registro=registro, bracos=bracos, pipeline=PipelineFeatures(),
                       modelos={modelo.nome: modelo}, statarb=statarb, precos_aux=precos_aux, orcamento=orc,
                       bloqueio=BloqueioDados(idade_max_dado_s=p.idade_max_dado_s),
                       avaliador_odds=(AvaliadorOdds(perfil, piscina_odds, config_odds(cfg))
                                       if piscina_odds is not None else None),
                       memoria_reflexiva=memoria_reflexiva, servico_reflexao=servico_reflexao)
    if contaminados and not sintetico and modo_tempo != "real":
        # dados historicos + modelo sem data de corte: o modelo pode ter visto o periodo => so prospectivo
        for bid in contaminados:
            if bid in orq.contas:
                orq.contas[bid].rotulos.add(ROTULO_CONTAMINADO_CORTE)
        registro.incidente(t_ns=t_ref_ns, origem="simulacao", severidade="alerta",
                           detalhe=f"{ROTULO_CONTAMINADO_CORTE}: bracos {sorted(contaminados)} usam modelo sem data de "
                                   "corte; em dados historicos o resultado NAO e comparavel (so avaliacao prospectiva).")
    if erro_risco is not None:
        registro.incidente(t_ns=t_ref_ns, origem="simulacao", severidade="critico",
                           detalhe=f"risco_nao_aprovado: politica de risco nao aprovada ({erro_risco}); "
                                   "limites aplicados SEM aprovacao valida (padrao/minimos ou aprovacao pendente). Resultado NAO e comparavel.")
        for conta in orq.contas.values():
            conta.rotulos.add("risco_nao_aprovado")
    return Montagem(orq, manifesto, bracos, orc, provedores, real)


def abrir_banco(saida: Path, rel: Any, sessao_id: str) -> tuple[BancoSQLite, Registro, Path, Path]:
    from robowin.auditoria.cadeia import RegistradorAuditoria
    saida = Path(saida)
    saida.mkdir(parents=True, exist_ok=True)
    caminho = saida / "robowin.db"
    if caminho.exists():
        raise ErroConfig(f"{caminho} ja existe: escolha outra --saida (nao sobrescrevo resultados)")
    banco = BancoSQLite(caminho)
    dir_aud = saida / "auditoria"
    return banco, Registro(banco, RegistradorAuditoria(dir_aud, rel), sessao_id), caminho, dir_aud


def montar_sessao(p: ParamsSimulacao) -> Sessao:
    cfg, catalogo = carregar_ambiente(p)
    perfil = perfil_da_config(cfg, p.ativo, permitir_a_confirmar=True)  # sintetico: a_confirmar roda, rotulado
    cfg_s = ConfigSintetico.do_perfil(perfil, seed=p.seed, n_ticks=p.n_ticks, sinal_forca=p.sinal_forca, n_saltos=0)
    ins = perfil.instrumento(cfg_s.simbolo)
    dias = gerar_dias(cfg_s, 2 + max(0, p.odds_sessoes))
    treino, dados = dias[-2], dias[-1]
    piscina_odds = None
    if p.odds_sessoes > 0:  # pregoes ANTERIORES a sessao: nenhum dado do dia da sessao entra na piscina
        piscina_odds = barras_de_ticks([t for d in dias[:-2] for t in d.ticks], perfil, sintetico=True,
                                       origem="sintetico")
    modelo = _modelo_persistido(p, perfil) or treinar_modelo(treino, perfil, p.tipo_modelo)
    statarb = treinar_statarb(treino, perfil, p.seed)
    sessao_id = f"sim-{p.seed}"
    rel = RelogioSimulado(dados.ticks[0].t_utc_ns)
    banco, registro, caminho, dir_aud = abrir_banco(p.saida, rel, sessao_id)
    m = construir_orquestrador(
        p, rel=rel, registro=registro, ins=ins, modelo=modelo, statarb=statarb, precos_aux=VistaSintetica(p.seed, nome_referencia(perfil), fator_vista(perfil.preco_sintetico_ref)),
        sintetico=True, modo_tempo="simulado", sessao_id=sessao_id, t_ref_ns=rel.agora_ns(), cfg=cfg,
        catalogo=catalogo, piscina_odds=piscina_odds)
    return Sessao(m.orq, banco, list(dados.ticks), list(dados.livros), m.manifesto, dados, caminho, dir_aud,
                  modelo, "real" if m.real else "falso")


def _hash_arquivo(p: Path) -> str:
    try:
        return sha256_arquivo(p)
    except OSError:
        return ""


async def executar_sessao(s: Sessao) -> dict[str, dict[str, Any]]:
    """Alimenta o orquestrador com todos os eventos da sessao, em ordem de tempo, e encerra."""
    orq = s.orq
    orq.iniciar(s.manifesto, t_ns=s.ticks[0].t_utc_ns)
    j = 0
    livros = s.livros
    for tick in s.ticks:
        while j < len(livros) and livros[j].t_utc_ns <= tick.t_utc_ns:
            orq.on_livro(livros[j])
            j += 1
        await orq.on_tick(tick)
    await orq.encerrar(s.manifesto)
    return orq.resumo()


def formatar_resumo(resumo: Mapping[str, Mapping[str, Any]], *, rotulo_ia: str, caminho_banco: Path | None = None,
                    dir_auditoria: Path | None = None, rotulo_dados: str = ROTULO_SESSAO,
                    extra: str = "", moeda: str = "BRL") -> str:
    L = ["=" * 100, f"  {rotulo_dados}", f"  IA: {rotulo_ia.upper()}"
         + ("  (respostas e latencias INJETADAS; nao representam nenhum modelo real)" if rotulo_ia == "falso" else ""),
         "  Nenhum resultado abaixo prova lucratividade (sem validacao fora da amostra).",
         "=" * 100,
         f"{'braco':<24}{'modo':<12}{'dec.':>6}{'entr.':>6}{'vet.':>6}{'bloq.':>6}{'trades':>7}{'liq. ' + moeda:>10}"
         f"{'IA':>5}{'IA inv.':>8}{'custo US$':>10}  rotulos"]
    for b, r in resumo.items():
        L.append(f"{b:<24}{r['modo']:<12}{r['decisoes']:>6}{r['entradas']:>6}{r['vetadas']:>6}{r['bloqueadas']:>6}"
                 f"{r['trades']:>7}{r['liquido_brl']:>10.2f}{r['chamadas_ia']:>5}{r['chamadas_ia_invalidas']:>8}"
                 f"{r['custo_ia_usd']:>10.4f}  {','.join(r['rotulos'])}")
    if extra:
        L.append(extra)
    if caminho_banco:
        L.append(f"\nBanco: {caminho_banco}")
    if dir_auditoria:
        L.append(f"Auditoria (cadeia de hash): {dir_auditoria}   -> robowin auditoria --dir <pasta>")
    return "\n".join(L)
