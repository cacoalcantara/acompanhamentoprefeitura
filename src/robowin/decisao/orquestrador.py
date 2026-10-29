"""Orquestrador de decisao (plano 5.2): ticks/livros -> features -> modelos/statarb/noticias -> dossie
anonimizado -> bracos -> validadores -> MotorRisco -> Roteador/OMS -> SimuladorExecucao.

* Cada braco tem a PROPRIA conta simulada: carteira (netting), kill switch, motor de risco, OMS e roteador;
  o simulador de execucao e compartilhado (uma carteira por `magic`). Nenhuma ordem real e enviada: o
  `Roteador` so opera em modo `simulado` aqui, e o gateway demo e o unico dono de `order_send` (mt5).
* Falha segura: qualquer falha de IA/dado BLOQUEIA NOVAS ENTRADAS (ver `validade.py`); saidas e protecao
  continuam. Stop e alvo sao executados pelo simulador (equivalente ao SL/TP do servidor); tempo-limite,
  ENCERRAR, zeragem e kill passam pelo `GestorSaidas` (UM dono de saida por posicao).
* Tempo: modo `simulado` (relogio simulado; as chamadas de IA sao aguardadas na hora e a resposta so "chega"
  em t0 + latencia medida da chamada) ou `real` (chamadas em tarefas; a resposta e entregue quando chega).
* Tudo e gravado no BancoSQLite (esquema de core.esquema_banco) e na auditoria com cadeia de hash, com
  carimbos de latencia por etapa. Trechos simulados sao rotulados `injetado`.
* Braco planejador (docs/16 WP7): a IA decide o trade inteiro; o plano compilado e armado com niveis ANCORADOS em
  t0 (``validade.ajustar_entrada_ancorada`` no disparo) e as regras de gestao rodam a cada tick (``_gestao``, antes
  de ``_saidas``): cada acao vira uma ``Intencao`` MANTER/ajuste que passa pelo roteador e pelo ``MotorRiscoPadrao``
  (veto final). Parciais nao fecham o episodio; cada episodio (abertura -> fechamento) vai para
  ``posicoes_episodio``, cada ajuste para ``gestao_eventos`` e cada entrada proposta para ``previsoes_ia``. Sombras
  replicam a entrada de P no mesmo tick (roteador e motor proprios) e o controle do plano observa o plano compilado.
  MANTER/ajuste/ENCERRAR ficam presos a posicao vista em t0 (``ignorada:posicao_mudou_desde_t0``) e cada braco
  posicionado recebe dossie proprio (C6). A IA nunca le ``ConfigRisco``: ve so o envelope derivado do motor.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import heapq
import json
import math
import statistics
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robowin.core import calendario_b3
from robowin.core.config import ErroConfig
from robowin.core.instrumentos import exigir_mesmo_ativo, perfil_de_instrumento
from robowin.core.protocolos import ModeloPreditivo, SinalStatArb
from robowin.core.relogio import Relogio, RelogioSimulado
from robowin.core.tipos import (
    Acao,
    AjustePosicao,
    CarimboLatencia,
    ConfigPlanejador,
    ConfigRisco,
    EnvelopeDecisao,
    Instrumento,
    Intencao,
    Lado,
    LivroSnapshot,
    ManifestoExperimento,
    PlanoCondicional,
    PlanoGestaoCompilado,
    Posicao,
    Tick,
    Trade,
)
from robowin.dados.features_ciclo import compactar as compactar_features
from robowin.execucao.custos import Custos
from robowin.execucao.oms import OMS, TERMINAIS
from robowin.execucao.roteador import ResultadoExecucao, Roteador
from robowin.execucao.simulador import EventoSim, SimuladorExecucao
from robowin.features.pipeline import PipelineFeatures
from robowin.features.registro import nomes_permitidos
from robowin.ia.dossie import UNIDADES_PROIBIDAS_V2, ErroDossie, hash_dossie, montar_dossie, unidade_feature
from robowin.ia.orcamento import OrcamentoIA
from robowin.ia.reflexao import MemoriaReflexiva, liquidar_pendentes
from robowin.ia.validadores import texto_seguro
from robowin.risco.kill_switch import EventoKill, KillSwitch
from robowin.risco.motor import EventoAgenda, MotorRiscoPadrao
from robowin.risco.tempo import dia_sp, segundos_do_dia

from .bracos.base import BracoBase, ContextoDecisao, Geometria, ResultadoDecisao
from .bracos.controles import BracoAleatorio, BracoAleatorioPlano
from .bracos.planejador import BracoPlanejador
from .bracos.sombra import BracoSombra
from .dossie_conta import CustosPublicos, codigo_retorno, custos_publicos, limites_legado, secoes_planejador
from .dsl_plano import EstadoPlano
from .espelho import validar_braco_espelhavel
from .gatilhos import ConfigGatilhos, EstadoGatilho, GerenciadorGatilhos
from .gestao import (
    AcaoGestao,
    ApertarStop,
    Encerrar,
    EstadoGestao,
    EstenderPrazo,
    EventoReavaliacao,
    Reduzir,
    aplicar_acao,
    atualizar_excursao,
    avaliar_gestao,
    iniciar_estado,
    registrar_liquido_parcial,
    sincronizar_posicao,
)
from .odds import MOTIVO_VETO, AvaliadorOdds
from .plano_condicional import (
    AssinaturaPosicao,
    EventoPlano,
    GerenciadorPlano,
    niveis_recalculados,
)
from .registro import ORIGEM_SINTETICO, Registro
from .saidas import GestorSaidas
from .validade import BloqueioDados, ajustar_entrada_ancorada, validar_frescor_entrada

NS = 1_000_000_000
FEATURES_DOSSIE = (
    "fluxo_imb_5s", "fluxo_imb_30s", "fluxo_imb_120s", "fluxo_intensidade_30s", "livro_imb_1", "livro_imb_5",
    "microprice_desvio_pts", "ofi_30s_norm", "ret_5s", "ret_30s", "ret_120s", "rv_1m", "rv_razao_1m_30m",
    "ctx_bucket_30m",
)
# dossie v2 (planejador, C30): sem unidade log/relativa (o retorno log `rv_1m` com os movimentos em pontos
# reconstruiria o preco). Derivado do registro de features: uma feature nova de unidade proibida nunca entra.
FEATURES_DOSSIE_V2 = tuple(f for f in FEATURES_DOSSIE
                           if unidade_feature(f) not in (*UNIDADES_PROIBIDAS_V2, "desconhecida"))
MAX_SLIPPAGES_STOP = 2000  # amostras causais de slippage de stop guardadas (custos publicos do dossie v2)


@dataclass
class ConfigOrquestrador:
    instrumento: Instrumento  # obrigatorio: vem do perfil do ativo
    risco: ConfigRisco = field(default_factory=ConfigRisco)
    sessao_id: str = "sim"
    sintetico: bool = True
    rotulo: str = "SIMULACAO"
    modo_tempo: str = "simulado"  # simulado | real
    seed: int = 0
    latencia_exec_ns: int = 300_000_000  # latencia de execucao INJETADA no simulador (rotulada)
    passo_s: float = 15.0  # periodo dos bracos sem IA (quant e controles)
    checagem_s: float = 15.0  # a cada quanto tempo os gatilhos sao avaliados (snapshot de features/modelos)
    equity_a_cada_s: float = 60.0
    janela_hist_s: float = 1200.0
    gatilhos: ConfigGatilhos = field(default_factory=ConfigGatilhos)
    arquivo_emergencia: Path | None = None
    referencias_frequencia: tuple[str, ...] = ("quant", "claude", "claude_gpt")
    exigir_aquecimento: bool = True
    # qualquer mercado: fator moeda de lucro -> moeda da conta NO INSTANTE (t_ns); None com moedas diferentes
    # (instrumento.moeda_conta != moeda) => entradas vetadas por falta de cotacao (ver core.moedas)
    conversor: Callable[[int], float | None] | None = None
    # braco planejador (adicoes opcionais): gatilhos do grupo do planejador ([planejador.gatilhos]); None => os
    # `gatilhos` acima com eventos de posicao so "fechou" e a reserva de gestao do [planejador] do braco
    gatilhos_planejador: ConfigGatilhos | None = None
    espelhar: str = ""  # braco espelhado na DEMO (validado na construcao: nunca sombra, controle ou passivo)


@dataclass
class MetaPosicao:
    intencao: Intencao
    aberta_ns: int
    tempo_max_s: int
    decisao_id: str = ""
    stop_pontos: float | None = None  # distancias na ABERTURA (pos.stop/pos.alvo reais); as atuais vem do simulador
    alvo_pontos: float | None = None
    gestao: PlanoGestaoCompilado | None = None
    estado: EstadoGestao | None = None
    ticket: int | None = None
    qtd_inicial: int = 0
    episodio_id: str = ""
    p_alvo: float | None = None
    trades: list[Trade] = field(default_factory=list)  # trades do episodio (parciais + o que fecha)
    sombra_de: str | None = None
    detalhe_saida: str = ""  # regra/detalhe da saida reivindicada (motivo final do episodio)


@dataclass
class ContaBraco:
    braco: BracoBase
    magic: int
    kill: KillSwitch
    motor: MotorRiscoPadrao
    oms: OMS
    roteador: Roteador
    planos: GerenciadorPlano
    meta: MetaPosicao | None = None
    entrada_em_voo: Intencao | None = None
    t_envio_entrada_ns: int = 0
    eventos_posicao: list[str] = field(default_factory=list)
    memoria: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=5))
    stats: Counter[str] = field(default_factory=Counter)
    custo_ia_usd: float = 0.0
    custo_ia_dia: dict[str, float] = field(default_factory=dict)  # dia (SP) -> gasto de IA do braco
    rotulos: set[str] = field(default_factory=set)
    ultimo_bloqueio: tuple[str, ...] | None = None  # evita repetir a mesma recusa de bracos sem IA a cada ciclo
    resumo_ultima_entrada: dict[str, Any] = field(default_factory=dict)  # hipoteses/justificativa (memoria reflexiva)
    n_trades_memoria: int = 0
    # braco planejador ---------------------------------------------------------------------------------------
    eventos_gestao: list[str] = field(default_factory=list)  # gestao:<evento> pedidos em reavaliar_se
    reavaliacao_ns: int | None = None  # instante pedido pela IA (reavaliar_em_s)
    plano_terminou: bool = False  # o plano armado expirou/foi invalidado/cancelado desde o ultimo ciclo
    decisao_entrada_id: str = ""  # decisao que enviou a entrada em voo (episodio)
    previsoes: dict[str, str] = field(default_factory=dict)  # id da intencao base -> id em previsoes_ia
    latencias_ia_s: deque[float] = field(default_factory=lambda: deque(maxlen=50))  # proprias, proposta_ia
    retorno: dict[str, Any] = field(default_factory=dict)  # codigos do ultimo desfecho (dossie v2, sem numeros)
    ultimo_veto: tuple[str, ...] | None = None  # deduplicacao de vetos repetidos (tentativas x episodios)
    n_gestao: int = 0


@dataclass
class Snapshot:
    t_ns: int
    vetor: dict[str, float]
    modelos: dict[str, dict[str, Any]]
    statarb: dict[str, float]
    z: float
    saida_principal: dict[str, float]
    meio: float
    bid: float
    ask: float
    noticias: list[dict[str, Any]]
    noticias_novas: list[float]
    wall_ms: float
    precos_aux: dict[str, float] = field(default_factory=dict)  # absolutos: so para conferir a anonimizacao (C29)


def pregoes_restantes_no_mes(dia: dt.date) -> int:
    """Pregoes B3 do mes de `dia` a partir dele (inclusive), no minimo 1."""
    n, d = 0, dia
    while d.month == dia.month:
        if calendario_b3.eh_pregao(d):
            n += 1
        d += dt.timedelta(days=1)
    return max(n, 1)


def _chave_bloqueio(motivo: str) -> str:
    """Chave de contagem do bloqueio: ``ia:x`` -> ``x``; vetos por odds (Maquina do Tempo) viram ``veto_por_odds``."""
    if motivo.startswith(MOTIVO_VETO):
        return MOTIVO_VETO
    return motivo.split(":", 1)[-1] if motivo.startswith("ia:") else motivo


def _codigo_retorno(motivo: object) -> str | None:
    """Codigo do motivo para a secao ``retorno`` do dossie, sem o detalhe livre apos ``: `` (que traz numeros do
    envelope, ex. ``quantidade_fora_do_envelope: 3 (1..2)``, invisiveis com ``expor_envelope="nenhum"``)."""
    m = str(motivo)
    return codigo_retorno(MOTIVO_VETO if m.startswith(MOTIVO_VETO) else m)


def _meio(tick: Tick) -> float:
    m = tick.meio
    return m if m > 0 else tick.last


class Orquestrador:
    def __init__(
        self, cfg: ConfigOrquestrador, *, relogio: Relogio, registro: Registro, bracos: Sequence[BracoBase],
        pipeline: PipelineFeatures | None = None, modelos: Mapping[str, ModeloPreditivo] | None = None,
        statarb: SinalStatArb | None = None, precos_aux: Callable[[Tick], dict[str, float]] | None = None,
        noticias: Callable[[int], list[dict[str, Any]]] | None = None, eventos: Iterable[EventoAgenda] = (),
        orcamento: OrcamentoIA | None = None, bloqueio: BloqueioDados | None = None, custos: Custos | None = None,
        simulador: SimuladorExecucao | None = None, avaliador_odds: AvaliadorOdds | None = None,
        memoria_reflexiva: MemoriaReflexiva | None = None, servico_reflexao: Any = None,
    ) -> None:
        if not bracos:
            raise ErroConfig("nenhum braco configurado")
        ids = [b.id for b in bracos]
        if len(set(ids)) != len(ids):
            raise ErroConfig(f"ids de braco duplicados: {ids}")
        self.cfg, self.rel, self.registro = cfg, relogio, registro
        self.avaliador_odds = avaliador_odds  # Maquina do Tempo (opcional): evidencia ao revisor e veto por odds
        self.perfil = perfil_de_instrumento(cfg.instrumento)
        self.ativo = self.perfil.simbolo_base
        self.pipeline = pipeline or PipelineFeatures()
        # capacidade do INSTRUMENTO: sem negocios publicos (forex/CFD/cripto) ou sem livro => features desligadas (NaN)
        from robowin.features.registro import features_desligadas_instrumento
        fora = features_desligadas_instrumento(self.perfil)
        if fora:
            self.pipeline.desligadas = self.pipeline.desligadas | frozenset(fora)
        self.modelos = dict(modelos or {})
        for nome_m, m in self.modelos.items():  # modelo de outro ativo NUNCA e reutilizado (nem o sem ativo, fora de sintetico)
            declarado = str(getattr(m, "ativo", ""))
            if declarado or not cfg.sintetico:
                exigir_mesmo_ativo(self.ativo, declarado, f"modelo '{nome_m}'")
        if orcamento is not None:
            orcamento.vincular_ativo(self.ativo)
        self.statarb, self.precos_aux, self.noticias_fn = statarb, precos_aux, noticias
        # memoria reflexiva (opcional, desligada por padrao): reflexoes SO depois do resultado liquidado
        self.memoria_reflexiva = memoria_reflexiva
        self.servico_reflexao = servico_reflexao
        self._liquidando = False
        self.orcamento = orcamento
        self._mes_orcamento: str | None = None
        self.bloqueio = bloqueio or BloqueioDados()
        self.eventos = list(eventos)
        self.sim = simulador or SimuladorExecucao(
            cfg.instrumento, relogio, custos or Custos.do_instrumento(cfg.instrumento), latencia_ns=cfg.latencia_exec_ns, seed=cfg.seed,
            conversor=cfg.conversor)
        self.saidas = GestorSaidas()
        self.contas: dict[str, ContaBraco] = {}
        self.grupos: dict[str, list[ContaBraco]] = {}
        self.gatilhos: dict[str, GerenciadorGatilhos] = {}
        self.hist: deque[tuple[int, float]] = deque()
        self.ultimo_tick: Tick | None = None
        self.t_inicio_ns: int | None = None
        self.t_ultimo_ns = 0
        self._pend: list[tuple[int, int, ResultadoDecisao]] = []
        self._seq = 0
        self._tarefas: set[asyncio.Task[None]] = set()
        self._prox_checagem_ns = 0
        self._prox_equity_ns = 0
        self._prox_flag_ns = 0
        self._noticias_vistas: set[str] = set()
        self._snap: Snapshot | None = None
        self.fora_de_ordem = 0
        self.ticks_processados = 0
        self.erros_braco: Counter[str] = Counter()
        self.encerrada = False
        self.espelho: Any = None  # EspelhoDemo (decisao/espelho.py): espelha UM braco na conta DEMO; None => desligado
        if cfg.espelhar:
            alvo = next((b for b in bracos if b.id == cfg.espelhar), None)
            validar_braco_espelhavel(alvo if alvo is not None else cfg.espelhar)  # nunca sombra/controle/passivo
        # custos publicos por unidade (pontos): estresse = custo de ida e volta usado pelo planejador e pela gestao
        self._custos_base = custos_publicos(self.perfil, spread_pts=0.0, slippages_stop_anteriores=())
        self._slip_stop: deque[tuple[str, float]] = deque(maxlen=MAX_SLIPPAGES_STOP)  # (dia SP, pontos contra)
        for i, b in enumerate(bracos):
            self._criar_conta(b, 1001 + i)
        self._ligar_controles()

    # ================================================================================== construcao ==
    def _criar_conta(self, b: BracoBase, magic: int) -> None:
        cfg = self.cfg
        ks = KillSwitch(arquivo_flag=cfg.arquivo_emergencia, auditoria=self.registro.auditoria)
        motor = MotorRiscoPadrao(cfg.risco, cfg.instrumento, kill_switch=ks, auditoria=self.registro.auditoria,
                                 eventos=self.eventos, grade=lambda d: calendario_b3.sessao_em(d, self.perfil),
                                 conversor=cfg.conversor)
        oms = OMS(self.rel, {b.id: magic}, banco=self.registro.banco, auditoria=self.registro.auditoria,
                  simulada=True)
        self.sim.registrar_braco(b.id, magic)
        rot = Roteador("simulado", cfg.instrumento, self.rel, oms, motor, simulador=self.sim,
                       bloqueio_externo=self._bloqueio_externo, posicao_fn=self.sim.posicao,
                       exigir_aprovacao=True)
        oms.ao_transicao.append(lambda o: self.registro.ordem(o))
        conta = ContaBraco(b, magic, ks, motor, oms, rot, GerenciadorPlano(b.id, cfg.instrumento.tick_pontos))
        ks.registrar_efeito(lambda ev, c=conta: self._efeito_kill(c, ev))
        conta.rotulos |= self.perfil.rotulos()  # ativo_a_confirmar: resultado sintetico, nunca operacional
        self.contas[b.id] = conta
        g = b.grupo_ciclo
        self.grupos.setdefault(g, []).append(conta)
        if g not in self.gatilhos:
            if isinstance(b, BracoPlanejador):
                gp = cfg.gatilhos_planejador or cfg.gatilhos.model_copy(update={
                    "eventos_posicao_permitidos": ("fechou",), "reserva_gestao_frac": b.cfg.reserva_gestao_frac})
                self.gatilhos[g] = GerenciadorGatilhos(gp, self.orcamento)
            elif b.ia:
                self.gatilhos[g] = GerenciadorGatilhos(cfg.gatilhos, self.orcamento)
            else:
                p = ConfigGatilhos(periodo_s=cfg.passo_s, intervalo_min_s=cfg.passo_s, rearme_evento_s=1e12,
                                   e_brl_min=1e18, z_statarb_min=1e18, evento_posicao=False,
                                   max_chamadas_dia=None)
                self.gatilhos[g] = GerenciadorGatilhos(p, None)

    def _ligar_controles(self) -> None:
        for c in self.contas.values():  # o controle do plano segue UM sorteio por plano observado (nao a taxa)
            if (isinstance(c.braco, BracoAleatorio) and not isinstance(c.braco, BracoAleatorioPlano)
                    and c.braco.frequencia_fn is None):
                c.braco.frequencia_fn = self._taxa_referencia

    def _taxa_referencia(self, t_ns: int) -> float:
        """Entradas por hora observadas nos bracos de referencia (mesma frequencia do controle)."""
        refs = [self.contas[i] for i in self.cfg.referencias_frequencia if i in self.contas]
        if not refs or self.t_inicio_ns is None:
            return 0.0
        horas = max((t_ns - self.t_inicio_ns) / NS / 3600.0, 0.25)
        return sum(c.stats["entradas"] for c in refs) / len(refs) / horas

    def _bloqueio_externo(self) -> str:
        m = self.bloqueio.motivo(self.rel.agora_ns())
        return f"dado:{m}" if m else ""

    # ===================================================================================== tempo ======
    def _avancar(self, t: int) -> None:
        if isinstance(self.rel, RelogioSimulado) and t > self.rel.agora_ns():
            self.rel.avancar_para(t)

    def _agora(self) -> int:
        return self.rel.agora_ns()

    # ================================================================================== ingestao =======
    def on_livro(self, livro: LivroSnapshot) -> None:
        if livro.t_utc_ns < self.t_ultimo_ns:
            # ao vivo, ticks e livros chegam por polls distintos: o livro vale a partir da CHEGADA (causal)
            livro = livro.model_copy(update={"t_utc_ns": self.t_ultimo_ns})
        self.sim.on_livro(livro)
        try:
            self.pipeline.on_livro(livro)
        except ValueError:
            self.fora_de_ordem += 1

    def gasto_mes_gravado(self, mes: str) -> float:
        """Gasto de IA do mes (AAAA-MM) ja gravado na tabela `orcamento_ia` (sobrevive a reinicios)."""
        linhas = self.registro.banco.consultar(
            "SELECT COALESCE(SUM(gasto_usd), 0) AS s FROM orcamento_ia WHERE substr(dia, 1, 7) = ?", (mes,))
        return float(linhas[0]["s"]) if linhas else 0.0

    def _sincronizar_orcamento(self, t: int, forcar: bool = False) -> None:
        """Na virada de dia (America/Sao_Paulo) reabre o pregao do orcamento; na virada de mes (ou ao iniciar)
        recarrega o gasto do mes gravado. Sem isto a cota diaria nunca rola e o gasto do mes nao persiste."""
        orc = self.orcamento
        if orc is None:
            return
        dia = dia_sp(t)
        if not forcar and orc.dia == dia.isoformat():
            return
        mes = dia.strftime("%Y-%m")
        if forcar or mes != self._mes_orcamento:
            orc.gasto_mes = self.gasto_mes_gravado(mes)
            self._mes_orcamento = mes
        orc.iniciar_pregao(dia.isoformat(), pregoes_restantes_no_mes(dia))

    async def on_tick(self, tick: Tick) -> None:
        if self.encerrada:
            return
        t = tick.t_utc_ns
        self._sincronizar_orcamento(t)
        if self.t_inicio_ns is None:
            self.t_inicio_ns = t
        self._avancar(t)
        t = max(t, self.t_ultimo_ns)
        self.t_ultimo_ns = t
        self.ticks_processados += 1
        self.bloqueio.dado_recebido(t)
        self.ultimo_tick = tick
        m = _meio(tick)
        if m > 0:
            self.hist.append((t, m))
            lim = t - int(self.cfg.janela_hist_s * NS)
            while self.hist and self.hist[0][0] < lim:
                self.hist.popleft()
        try:
            self.pipeline.on_tick(tick)
        except ValueError:
            self.fora_de_ordem += 1
        self._rotear_eventos(self.sim.on_tick(tick), t)
        self._verificar_flag(t)
        self._marcar_a_mercado(t, tick)
        self._gestao(t, tick)  # gestao automatica ANTES das saidas por tempo/zeragem
        self._saidas(t)
        await self._planos(t, tick)
        await self._entregar(t)
        await self._ciclos(t)
        await self._entregar(t)
        self._equity_periodico(t)

    async def on_tempo(self) -> None:
        """Passo de relogio sem tick (modo real): entrega respostas de IA e confere kill/saidas por tempo."""
        t = max(self._agora(), self.t_ultimo_ns)
        self._sincronizar_orcamento(t)
        self._verificar_flag(t)
        if self.ultimo_tick is not None:
            self._gestao(t, self.ultimo_tick)
        self._saidas(t)
        await self._entregar(t)

    # ==================================================================== eventos do simulador ==========
    def _rotear_eventos(self, eventos: list[EventoSim], t: int) -> None:
        por_braco: dict[str, list[EventoSim]] = {}
        for e in eventos:
            por_braco.setdefault(e.braco, []).append(e)
        for braco, evs in por_braco.items():
            conta = self.contas.get(braco)
            if conta is None:
                continue
            conta.roteador.processar_eventos(evs, t)
            for e in evs:
                self._registrar_evento_sim(conta, e, t)

    def _registrar_evento_sim(self, conta: ContaBraco, e: EventoSim, t: int) -> None:
        if e.tipo == "execucao" and e.execucao is not None:
            o = conta.oms.por_ticket(e.ticket)
            self.registro.execucao(e.execucao, ordem_id=o.id if o else f"sim-{e.ticket}")
            if o is not None and o.eh_entrada:
                self._abrir_posicao(conta, e, t)
        elif e.tipo == "trade" and e.trade is not None:
            if e.restante > 0:  # parcial: o episodio continua (a meta fica)
                self._registrar_parcial(conta, e.trade, t)
            else:
                self._fechar_posicao(conta, e.trade, t)
        elif e.tipo in ("rejeitada", "cancelada"):
            o = conta.oms.por_ticket(e.ticket)
            if o is not None and o.eh_entrada and conta.meta is None:
                conta.entrada_em_voo = None  # a entrada nao virou posicao: libera o braco
            if o is not None and not o.eh_entrada:  # parcial que nao executou: solta a propria reivindicacao
                self.saidas.liberar_se(conta.braco.id, "parcial", o.intencao_id)
            if e.tipo == "rejeitada" and e.motivo == "posicao_inexistente":  # saida redundante: a protecao ja fechou a posicao
                self.registro.evento_risco(t_ns=t, braco=conta.braco.id, tipo="saida_duplicada_evitada",
                                           detalhe=f"ticket={e.ticket}")
                conta.stats["saidas_duplicadas_evitadas"] += 1

    def _tempo_da_posicao(self, conta: ContaBraco, it: Intencao | None) -> int:
        """Tempo maximo da posicao: o da intencao (ou, sem ele, o da geometria do braco), sempre limitado pelo teto
        do usuario (``tempo_max_posicao_s``) e NUNCA 0 (a geometria sentinela do planejador tem tempo 0)."""
        teto = max(1, int(self.cfg.risco.tempo_max_posicao_s))
        g = conta.braco.geometria
        cand = [v for v in ((it.tempo_max_s if it is not None else None),
                            (g.tempo_max_s if g.tempo_max_s > 0 else None)) if v is not None and v > 0]
        tempo = int(cand[0]) if cand else teto
        return max(1, min(tempo, teto))

    def _abrir_posicao(self, conta: ContaBraco, e: EventoSim, t: int) -> None:
        pos = self.sim.posicao(conta.braco.id)
        if pos.quantidade <= 0:
            return
        if conta.meta is not None and conta.meta.ticket == pos.ticket:
            return  # entrada limitada preenchida em partes: o episodio ja foi aberto
        it = conta.entrada_em_voo
        g = conta.braco.geometria
        tempo = self._tempo_da_posicao(conta, it)
        # stop e alvo REAIS da posicao (o roteador arredonda em direcao ao preco): distancias da abertura (C4)
        if pos.stop is not None:
            stop_pts: float | None = abs(pos.preco_medio - pos.stop)
        else:
            stop_pts = it.stop_pontos if it is not None else (g.stop_pontos or None)
        if pos.alvo is not None:
            alvo_pts: float | None = abs(pos.alvo - pos.preco_medio)
        else:
            alvo_pts = it.alvo_pontos if it is not None else (g.alvo_pontos or None)
        intencao = it if it is not None else Intencao(
            id="?", braco=conta.braco.id, acao=Acao.COMPRAR if pos.lado == Lado.COMPRA else Acao.VENDER,
            validade_ns=t, stop_pontos=g.stop_pontos or None, alvo_pontos=g.alvo_pontos or None)
        meta = MetaPosicao(
            intencao=intencao, aberta_ns=pos.aberta_ns, tempo_max_s=tempo, decisao_id=conta.decisao_entrada_id,
            stop_pontos=round(stop_pts, 10) if stop_pts is not None else None,
            alvo_pontos=round(alvo_pts, 10) if alvo_pts is not None else None,
            gestao=intencao.gestao, ticket=pos.ticket, qtd_inicial=pos.quantidade, p_alvo=intencao.p_alvo,
            sombra_de=conta.braco.referencia if isinstance(conta.braco, BracoSombra) else None)
        try:
            meta.estado = iniciar_estado(pos, intencao, tempo)
        except ValueError:
            meta.estado = None
        conta.meta, conta.entrada_em_voo = meta, None
        conta.eventos_posicao.append("abriu")
        conta.stats["posicoes_abertas"] += 1
        self.registro.posicao(braco=conta.braco.id, t_ns=t, lado=pos.lado.value if pos.lado else None,
                              quantidade=pos.quantidade, preco_medio=pos.preco_medio, stop=pos.stop, alvo=pos.alvo)
        meta.episodio_id = self.registro.episodio(
            braco=conta.braco.id, ticket=pos.ticket, decisao_id=meta.decisao_id or None, intencao_id=intencao.id,
            lado=pos.lado.value if pos.lado else None, q_inicial=pos.quantidade, ancora_t0=intencao.ancora_t0,
            stop_inicial_pts=meta.stop_pontos, alvo_inicial_pts=meta.alvo_pontos, tempo_max_s=tempo,
            t_aberta_ns=pos.aberta_ns, p_alvo=intencao.p_alvo, sombra_de=meta.sombra_de) or ""
        if e.execucao is not None and conta.t_envio_entrada_ns:
            self.registro.latencia(
                CarimboLatencia(etapa="ordem_para_fill", braco=conta.braco.id, t_ini_ns=conta.t_envio_entrada_ns,
                                t_fim_ns=e.execucao.t_ns, injetado=True), t_ns=t)
        self._espelhar(conta, t, "espelhar_entrada", t, pos, meta.intencao, meta.stop_pontos, meta.alvo_pontos,
                       meta.tempo_max_s)

    def _registrar_parcial(self, conta: ContaBraco, tr: Trade, t: int) -> None:
        """Parcial (``restante > 0``): grava o trade, MANTEM a meta (o episodio continua), libera a reivindicacao da
        parcial e gera o evento ``parcial``. Perdas seguidas e martingale so contam ao fechar o episodio (motor)."""
        braco = conta.braco.id
        self.registro.trade(tr, motivo_saida="parcial")
        conta.stats["trades"] += 1
        conta.stats["parciais"] += 1
        self.saidas.liberar_se(braco, "parcial")
        meta = conta.meta
        pos = self.sim.posicao(braco)
        if meta is not None:
            meta.trades.append(tr)
            est = meta.estado
            if est is not None:
                reduzido = est.qtd - pos.quantidade
                if reduzido >= 1:
                    aplicar_acao(est, Reduzir(reduzido, "parcial"))
                sincronizar_posicao(est, pos)
                registrar_liquido_parcial(est, tr.liquido_brl)
        conta.eventos_posicao.append("parcial")
        self.registro.posicao(braco=braco, t_ns=t, lado=pos.lado.value if pos.lado else None,
                              quantidade=pos.quantidade, preco_medio=pos.preco_medio, stop=pos.stop, alvo=pos.alvo)
        self._gravar_equity(conta, t)

    def _motivo_final(self, conta: ContaBraco, tr: Trade, motivo: str) -> str:
        """Motivo detalhado do fim do episodio: stop_inicial | stop_movido_ia | stop_movido_auto | alvo | tempo |
        encerrar_ia | gestao:<regra> | zeragem | kill (ou o motivo bruto)."""
        meta = conta.meta
        if tr.motivo_saida == "stop":
            est = meta.estado if meta is not None else None
            origem = est.origem_ultimo_stop if est is not None else "inicial"
            return "stop_inicial" if origem == "inicial" else f"stop_movido_{origem}"
        if motivo == "encerrar":
            return "encerrar_ia" if conta.braco.ia else "encerrar"
        if motivo == "gestao":
            det = meta.detalhe_saida if meta is not None else ""
            return f"gestao:{det}" if det else "gestao"
        return motivo

    def _fechar_posicao(self, conta: ContaBraco, tr: Trade, t: int) -> None:
        braco = conta.braco.id
        claim = self.saidas.liberar(braco)
        motivo = tr.motivo_saida
        if claim is not None and motivo in ("saida", "zeragem"):
            motivo = claim.motivo
        self.registro.trade(tr, motivo_saida=motivo)
        conta.stats["trades"] += 1
        if conta.meta is not None:
            self._gravar_episodio(conta, conta.meta, tr, self._motivo_final(conta, tr, motivo))
        sinal = 1.0 if tr.lado == Lado.COMPRA else -1.0
        conta.n_trades_memoria += 1
        entrada_mem = {
            "acao": "COMPRAR" if tr.lado == Lado.COMPRA else "VENDER", "resultado_pontos": sinal * (tr.saida - tr.entrada),
            "duracao_s": max(0, (tr.t_saida_ns - tr.t_entrada_ns) // NS), "motivo_saida": motivo,
            "trade_id": f"{braco}:{conta.n_trades_memoria}"}
        conta.memoria.append(entrada_mem)
        # a reflexao pos-trade faz parte das variantes B/C; o planejador foi pre-registrado SEM ela (docs/16 4.1)
        if (self.memoria_reflexiva is not None and conta.braco.ia and not isinstance(conta.braco, BracoPlanejador)
                and self._dono_memoria(conta) is conta):  # so a memoria que ALIMENTA algum dossie
            # PENDENTE: a reflexao so nasce depois do atraso de liquidacao
            self.memoria_reflexiva.registrar_trade(
                trade_id=entrada_mem["trade_id"], braco=braco, t_saida_ns=tr.t_saida_ns, acao=entrada_mem["acao"],
                resultado_pontos=entrada_mem["resultado_pontos"], duracao_s=entrada_mem["duracao_s"],
                motivo_saida=motivo, resumo_decisao=conta.resumo_ultima_entrada)
        conta.meta = None
        conta.eventos_posicao.append("fechou")
        self.registro.posicao(braco=braco, t_ns=t, lado=None, quantidade=0, preco_medio=0.0, stop=None, alvo=None)
        self._gravar_equity(conta, t)
        self._espelhar(conta, t, "sim_fechou", motivo, t)  # stop/alvo do simulador: a demo fecha junto (se ainda aberta)

    def _gravar_episodio(self, conta: ContaBraco, meta: MetaPosicao, tr: Trade, motivo_final: str) -> None:
        """Fecha o episodio (MESMO id da abertura): liquido somado das parciais e do trade final, ajustes, MFE/MAE."""
        trades = [*meta.trades, tr]
        est = meta.estado
        if tr.motivo_saida == "stop" and est is not None and est.sl is not None:  # slippage causal de stop (pontos)
            s = 1.0 if tr.lado == Lado.COMPRA else -1.0
            slip = s * (est.sl - tr.saida)
            if math.isfinite(slip):
                self._slip_stop.append((dia_sp(tr.t_saida_ns).isoformat(), round(slip, 10)))
        conta.retorno = {"ultimo_episodio": motivo_final}
        conta.stats["episodios"] += 1
        self.registro.episodio(
            id=meta.episodio_id or None, braco=conta.braco.id, ticket=meta.ticket, t_fechada_ns=tr.t_saida_ns,
            liquido_total=round(sum(x.liquido_brl for x in trades), 6), n_parciais=len(meta.trades),
            n_ajustes_ia=est.ajustes_ia if est else 0, n_ajustes_auto=est.ajustes_auto if est else 0,
            mfe_pts=est.mfe_pontos if est else None, mae_pts=est.mae_pontos if est else None,
            motivo_final=motivo_final,
            trades_json=json.dumps([{"id": x.id, "liquido": round(x.liquido_brl, 6), "saida": x.motivo_saida,
                                     "t_saida_ns": x.t_saida_ns} for x in trades], ensure_ascii=False))

    # ============================================================================ risco e protecao ======
    def _verificar_flag(self, t: int) -> None:
        if self.cfg.arquivo_emergencia is None or t < self._prox_flag_ns:
            return
        self._prox_flag_ns = t + NS
        if Path(self.cfg.arquivo_emergencia).exists():
            self.acionar_emergencia("arquivo EMERGENCIA presente", t, origem="manual_flag")

    def acionar_emergencia(self, motivo: str, t_ns: int | None = None, origem: str = "manual_cli") -> None:
        """Kill switch manual: aciona TODAS as contas (cancela planos e ordens, zera posicoes, bloqueia entradas)."""
        t = self._agora() if t_ns is None else t_ns
        for conta in self.contas.values():
            if conta.kill.acionar(motivo, origem, t):
                self.registro.evento_risco(t_ns=t, braco=conta.braco.id, tipo="kill_switch", detalhe=motivo,
                                           critico=True)

    def _efeito_kill(self, conta: ContaBraco, ev: EventoKill) -> None:
        braco = conta.braco.id
        e = conta.planos.cancelar("kill_switch", ev.t_ns)
        if e is not None:
            self._registrar_evento_plano(e)
        for o in list(conta.oms.abertas()):
            if o.ticket and o.estado.value in ("ACEITA", "PARCIAL") and o.eh_entrada:
                conta.roteador.cancelar(o.id, ev.t_ns, forcar=True)
        pos = self.sim.posicao(braco)
        if pos.quantidade > 0 and ev.zerar:
            self._sair(conta, "kill", ev.t_ns, forcado=True)
        self._espelhar(conta, ev.t_ns, "ao_kill", f"{ev.origem}: {ev.motivo}", ev.t_ns)

    def _marcar_a_mercado(self, t: int, tick: Tick) -> None:
        ins = self.cfg.instrumento
        spread = max(tick.ask - tick.bid, 0.0) if tick.ask > 0 and tick.bid > 0 else 0.0
        por_cotacao = not self.perfil.tem_negocios
        for conta in self.contas.values():
            self._alimentar_spread(conta, spread)
            pos = self.sim.posicao(conta.braco.id)
            if pos.quantidade <= 0 or pos.lado is None:
                continue
            if pos.lado == Lado.COMPRA:
                mk = tick.bid or tick.last
                pnl = (mk - pos.preco_medio) * pos.quantidade * ins.ponto_valor_brl
            else:
                mk = tick.ask or tick.last
                pnl = (pos.preco_medio - mk) * pos.quantidade * ins.ponto_valor_brl
            conta.motor.marcar_a_mercado(conta.braco.id, pnl, t)
            est = conta.meta.estado if conta.meta is not None else None
            if est is not None:  # MFE/MAE pela mesma referencia de disparo do stop no simulador
                atualizar_excursao(est, bid=tick.bid, ask=tick.ask, last=tick.last, por_cotacao=por_cotacao)

    @staticmethod
    def _alimentar_spread(conta: ContaBraco, spread: float) -> None:
        """Spread corrente no motor (distancia minima de stop/alvo ajustado = DIST_MIN_STOP_TICKS ticks + spread)."""
        mk = conta.motor.mercado
        if math.isfinite(spread) and mk.spread_pts != spread:
            conta.motor.mercado = mk.model_copy(update={"spread_pts": spread})

    def _teto_tempo_s(self, conta: ContaBraco) -> int:
        """Tempo maximo da posicao: o da meta (pedido pela IA/geometria), nunca acima do teto do usuario."""
        teto = int(self.cfg.risco.tempo_max_posicao_s)
        meta = conta.meta
        return min(meta.tempo_max_s, teto) if meta is not None else teto

    def _saidas(self, t: int) -> None:
        for conta in self.contas.values():
            pos = self.sim.posicao(conta.braco.id)
            if pos.quantidade <= 0:
                continue
            precisa, motivo = conta.motor.precisa_zerar(pos, t)
            if precisa:
                self._sair(conta, "kill" if motivo == "kill_switch" else "zeragem", t, forcado=True, detalhe=motivo)
            elif conta.meta is not None and t - pos.aberta_ns >= self._teto_tempo_s(conta) * NS:
                self._sair(conta, "tempo", t)

    def _sair(self, conta: ContaBraco, motivo: str, t: int, *, forcado: bool = False, detalhe: str = "",
              ticket_esperado: int | None = None) -> bool:
        """Saida deterministica por tempo/zeragem/kill/ENCERRAR/gestao. UM dono por posicao: so a primeira
        reivindicacao vale (uma parcial pendente cede a vez a uma saida total). ``ticket_esperado``: a saida pedida
        pela IA fica presa a posicao vista em t0."""
        braco = conta.braco.id
        pos = self.sim.posicao(braco)
        if pos.quantidade <= 0:
            return False
        if ticket_esperado is not None and pos.ticket != ticket_esperado:
            conta.stats["saidas_ticket_divergente"] += 1
            return False
        self.saidas.liberar_se(braco, "parcial")  # a saida total prevalece sobre a reducao pendente
        iid = f"saida-{motivo}-{braco}-{pos.ticket}"
        if not self.saidas.reivindicar(braco, pos.ticket, motivo, t, iid):
            conta.stats["saidas_recusadas_dono"] += 1
            return False
        env = None
        if forcado:
            env = conta.roteador.zerar_braco(braco, t, motivo)
        else:
            it = Intencao(id=iid, braco=braco, acao=Acao.ENCERRAR, validade_ns=t + 60 * NS,
                          preco_referencia=_meio(self.ultimo_tick) if self.ultimo_tick else 0.0, t0_ns=t,
                          posicao_ticket=ticket_esperado)
            r = conta.roteador.executar_intencao(it, pos, t)
            env = r.envio
        enviado = env is not None and env.enviado and env.ordem.estado not in TERMINAIS
        if not enviado:  # nao saiu (ex.: recuo do OMS): libera para tentar de novo no proximo tick
            self.saidas.liberar(braco)
            return False
        if conta.meta is not None:
            conta.meta.detalhe_saida = detalhe
        conta.stats[f"saida_{motivo}"] += 1
        self.registro.evento_risco(t_ns=t, braco=braco, tipo=f"saida_{motivo}", detalhe=detalhe or motivo,
                                   critico=motivo == "kill")
        # o kill vai ao gateway como EMERGENCIA (cancela, zera e trava); as demais saidas, como saida por ticket
        self._espelhar(conta, t, "ao_kill" if motivo == "kill" else "saida", detalhe or motivo, t)
        return True

    def _espelhar(self, conta: ContaBraco, t: int, metodo: str, *args: Any) -> None:
        """Delega ao espelho (so do braco espelhado). Falha do espelho NUNCA derruba a sessao: desliga novas entradas."""
        esp = self.espelho
        if esp is None or conta.braco.id != esp.cfg.braco:
            return
        try:
            getattr(esp, metodo)(*args)
        except Exception as e:  # noqa: BLE001
            self.registro.incidente(t_ns=t, origem="espelho", severidade="erro",
                                    detalhe=f"{metodo}: {type(e).__name__}: {e}"[:300])
            esp.desligar(f"erro em {metodo}: {type(e).__name__}", t)

    # ==================================================================================== gestao ========
    def _perto_frac(self, conta: ContaBraco) -> float:
        g = self.gatilhos.get(conta.braco.grupo_ciclo)
        return float(g.cfg.perto_frac) if g is not None else 0.3

    def _gestao(self, t: int, tick: Tick) -> None:
        """Regras de gestao (decisao/gestao.py) das posicoes com plano de gestao. Cada acao vira uma Intencao MANTER
        com ``ajuste`` que passa pelo roteador e pelo motor (veto final). Pula a conta que precisa zerar ou que ja tem
        dono de saida. Falha => ``kill.registrar_erro`` e incidente; a posicao segue protegida pelo SL e pelo prazo."""
        for conta in self.contas.values():
            meta = conta.meta
            if meta is None or meta.gestao is None or meta.estado is None:
                continue
            braco = conta.braco.id
            pos = self.sim.posicao(braco)
            if pos.quantidade <= 0 or pos.lado is None or pos.ticket != meta.ticket:
                continue
            try:
                if self.saidas.tem_dono(braco) or conta.motor.precisa_zerar(pos, t)[0]:
                    continue
                self._gestao_conta(conta, meta, pos, t, tick)
            except Exception as e:  # noqa: BLE001 - gestao nunca derruba a sessao
                self.erros_braco[braco] += 1
                self.registro.incidente(t_ns=t, origem=f"gestao:{braco}", severidade="erro",
                                        detalhe=f"{type(e).__name__}: {e}"[:300])
                conta.kill.registrar_erro(f"gestao:{braco}", t)

    def _gestao_conta(self, conta: ContaBraco, meta: MetaPosicao, pos: Posicao, t: int, tick: Tick) -> None:
        est, plano = meta.estado, meta.gestao
        assert est is not None and plano is not None
        sincronizar_posicao(est, pos)  # stop/alvo/quantidade REAIS (o que valeu no simulador)
        teto = min(meta.aberta_ns + int(self.cfg.risco.tempo_max_posicao_s) * NS, conta.motor.zeragem_ns(t))
        acoes = avaliar_gestao(
            plano, est, bid=tick.bid, ask=tick.ask, last=tick.last, t_ns=t, tick=self.cfg.instrumento.tick_pontos,
            dist_min=conta.motor.dist_min_ajuste_pontos(),
            custo_ida_volta_pontos=self._custos_base.ida_volta_pontos_estresse, teto_prazo_ns=teto,
            por_cotacao=not self.perfil.tem_negocios, perto_frac=self._perto_frac(conta))
        for a in acoes:
            if isinstance(a, EventoReavaliacao):
                conta.eventos_gestao.append(a.nome)
            elif isinstance(a, Encerrar):
                self._sair(conta, "gestao", t, detalhe=a.motivo, ticket_esperado=meta.ticket)
                return
            else:
                self._acao_gestao_auto(conta, a, t, tick)

    def _acao_gestao_auto(self, conta: ContaBraco, a: AcaoGestao, t: int, tick: Tick) -> None:
        braco = conta.braco.id
        pos = self.sim.posicao(braco)
        if pos.quantidade <= 0 or pos.lado is None:
            return
        s = 1.0 if pos.lado == Lado.COMPRA else -1.0
        if isinstance(a, ApertarStop):
            aj, nome, regra = AjustePosicao(stop_resultado_pontos=round(s * (a.nivel - pos.preco_medio), 10)), \
                "apertar_stop", a.regra
        elif isinstance(a, EstenderPrazo):
            aj, nome, regra = AjustePosicao(tempo_max_total_s=int((a.prazo_ns - pos.aberta_ns) // NS)), \
                "estender_prazo", a.regra
        elif isinstance(a, Reduzir):
            aj, nome, regra = AjustePosicao(reduzir_quantidade=int(a.q)), "reduzir", a.regra
        else:
            return
        conta.n_gestao += 1
        it = Intencao(id=f"gestao-{braco}-{pos.ticket}-{conta.n_gestao}", braco=braco, acao=Acao.MANTER,
                      validade_ns=t + 60 * NS, preco_referencia=self._preco_saida(pos, tick), t0_ns=t, ajuste=aj,
                      posicao_ticket=pos.ticket, quantidade_t0=pos.quantidade)
        self._executar_ajuste(conta, it, pos, t, origem="auto", acao=nome, regra=regra)

    @staticmethod
    def _preco_saida(pos: Posicao, tick: Tick | None, padrao: float = 0.0) -> float:
        """Preco EXECUTAVEL de saida (bid comprado, ask vendido): referencia de distancia minima dos ajustes."""
        if tick is None:
            return padrao
        p = tick.bid if pos.lado == Lado.COMPRA else tick.ask
        return p if p > 0 else (_meio(tick) or padrao)

    def _executar_ajuste(self, conta: ContaBraco, it: Intencao, pos: Posicao, t: int, *, origem: str, acao: str,
                         regra: str = "") -> tuple[str, ResultadoExecucao | None, tuple[str, ...]]:
        """MANTER com ``ajuste`` (da IA ou automatico) pelo roteador e pelo motor. So e ``executada`` se TUDO o que
        foi pedido foi aplicado; recusa do motor => ``vetada``; recusa do simulador/OMS => ``nao_aplicada``. A
        reducao reivindica a posicao (sem dono vigente) ate a parcial executar. Grava ``gestao_eventos``."""
        braco = conta.braco.id
        aj = it.ajuste
        assert aj is not None
        meta, est = conta.meta, (conta.meta.estado if conta.meta is not None else None)
        reduz = aj.reduzir_quantidade is not None
        sl0, tp0 = pos.stop, pos.alvo
        if reduz and not self.saidas.reivindicar(braco, pos.ticket, "parcial", t, it.id):
            conta.stats["saidas_recusadas_dono"] += 1
            motivos: tuple[str, ...] = ("saida_ja_reivindicada",)
            self._gravar_gestao(conta, t, pos, origem, acao, regra, sl0, tp0, False, motivos)
            return "ignorada", None, motivos
        r = conta.roteador.executar_intencao(it, pos, t)
        protecao = aj.stop_resultado_pontos is not None or aj.alvo_resultado_pontos is not None
        aprovada = r.decisao.aprovada
        falhas: list[str] = []
        if aprovada and protecao and r.niveis_aplicados is None:
            falhas.append(r.motivo or "protecao_nao_aplicada")
        enviado = r.envio is not None and r.envio.enviado and r.envio.ordem.estado not in TERMINAIS
        if aprovada and reduz and not enviado:
            falhas.append(f"reducao_nao_enviada:{r.motivo}" if r.motivo else "reducao_nao_enviada")
        if reduz and not (aprovada and enviado):
            self.saidas.liberar_se(braco, "parcial", it.id)
        pos2 = self.sim.posicao(braco)
        aplicado = aprovada and not falhas
        if aprovada and est is not None and meta is not None:
            if r.niveis_aplicados is not None:  # stop/alvo REAIS depois do ajuste
                if pos2.stop is not None and pos2.stop != est.sl:
                    est.origem_ultimo_stop = origem
                sincronizar_posicao(est, pos2)
            if aj.tempo_max_total_s is not None:
                meta.tempo_max_s = min(int(aj.tempo_max_total_s), int(self.cfg.risco.tempo_max_posicao_s))
                est.prazo_ns = meta.aberta_ns + meta.tempo_max_s * NS
            if it.gestao is not None:  # substituir_gestao: regras novas, contagem de disparos recomeca
                meta.gestao = it.gestao
                est.regras_disparadas = set()
            if aplicado:
                if origem == "ia":
                    est.ajustes_ia += 1
                else:
                    est.ajustes_auto += 1
        if aplicado:
            conta.stats["gestoes_ia" if origem == "ia" else "gestoes_auto"] += 1
        elif not aprovada:
            conta.stats["gestoes_vetadas"] += 1
        else:
            conta.stats["gestoes_nao_aplicadas"] += 1
        motivos = tuple(r.decisao.motivos) if not aprovada else tuple(falhas)
        self._gravar_gestao(conta, t, pos2 if pos2.quantidade > 0 else pos, origem, acao, regra, sl0, tp0, aplicado,
                            motivos)
        if aprovada and r.niveis_aplicados is not None:
            sl, tp = r.niveis_aplicados
            self._espelhar(conta, t, "espelhar_protecao", sl, tp, t)
        if aprovada and reduz and enviado:
            self._espelhar(conta, t, "saida_parcial", int(aj.reduzir_quantidade or 0), f"parcial_{origem}", t)
        resultado = "executada" if aplicado else ("vetada" if not aprovada else "nao_aplicada")
        return resultado, r, motivos

    def _gravar_gestao(self, conta: ContaBraco, t: int, pos: Posicao, origem: str, acao: str, regra: str,
                       sl0: float | None, tp0: float | None, aplicado: bool, motivos: Sequence[str]) -> None:
        est = conta.meta.estado if conta.meta is not None else None
        self.registro.gestao_evento(
            t_ns=t, braco=conta.braco.id, ticket=pos.ticket, origem=origem, acao=acao, regra=regra or None,
            sl_antes=sl0, sl_depois=pos.stop, tp_antes=tp0, tp_depois=pos.alvo,
            prazo_ns=est.prazo_ns if est is not None else None, aplicado=bool(aplicado),
            motivo=";".join(motivos)[:300] or None)

    # ====================================================================================== planos ======
    def _registrar_evento_plano(self, ev: EventoPlano) -> None:
        d = ev.como_dict()
        self.registro.auditar(f"plano_{ev.tipo}", d)
        c = self.contas[ev.braco]
        c.stats[f"plano_{ev.tipo}"] += 1
        if ev.tipo in ("cancelado", "invalidado", "expirado", "invalidado_ao_armar"):
            self.registro.evento_risco(t_ns=ev.t_ns, braco=ev.braco, tipo=f"plano_{ev.tipo}", detalhe=ev.motivo)
            if isinstance(c.braco, BracoPlanejador):  # o planejador pode pedir para ser chamado de novo
                c.plano_terminou = True
                c.retorno = {"plano": ev.tipo}

    async def _planos(self, t: int, tick: Tick) -> None:
        for conta in self.contas.values():
            if conta.planos.armado is None:
                continue
            pos = self.sim.posicao(conta.braco.id)
            est = EstadoPlano(t_ns=t, preco=_meio(tick), spread_pts=max(tick.ask - tick.bid, 0.0))
            ev = conta.planos.avaliar(
                est=est, assinatura=AssinaturaPosicao.de(pos), kill_ativo=conta.kill.ativo,
                janela_evento=conta.motor.evento_proximo(t) is not None)
            if ev is None:
                continue
            self._registrar_evento_plano(ev)
            if ev.tipo == "disparado":
                self._disparar_plano(conta, ev, tick, t)

    def _cfg_planejador(self, conta: ContaBraco) -> ConfigPlanejador:
        cfg = getattr(conta.braco, "cfg", None)
        if isinstance(cfg, ConfigPlanejador):
            return cfg
        ref = self.contas.get(str(getattr(conta.braco, "referencia", "")))
        cfg_ref = getattr(ref.braco, "cfg", None) if ref is not None else None
        return cfg_ref if isinstance(cfg_ref, ConfigPlanejador) else ConfigPlanejador()

    def _favor_max(self, conta: ContaBraco, base: Intencao, plano: PlanoCondicional, compra: bool) -> float:
        """Quanto o preco pode ter andado a favor desde t0 sem perseguir: a faixa do plano compilado (planejador) ou,
        sem ele, a invalidacao a favor do plano (rompe acima na compra, rompe abaixo na venda). Sem faixa: 0."""
        braco = conta.braco
        if isinstance(braco, BracoPlanejador):
            c = braco.compilado(base.id)
            v = c.resumo.get("faixa_favor_pontos") if c is not None else None
            if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) and v >= 0:
                return float(v)
        tipo = "preco_rompe_acima" if compra else "preco_rompe_abaixo"
        vals = [abs(float(c.valor)) for c in plano.invalidacoes if c.tipo == tipo and c.valor is not None
                and math.isfinite(float(c.valor)) and (c.valor > 0) == compra]
        return min(vals) if vals else 0.0

    def _disparar_plano(self, conta: ContaBraco, ev: EventoPlano, tick: Tick, t: int) -> None:
        base = ev.plano.base
        compra = base.acao == Acao.COMPRAR
        preco_real = tick.ask if compra else tick.bid
        if preco_real <= 0:
            preco_real = _meio(tick)
        it = conta.planos.intencao_disparo(ev, preco_real=preco_real, t_ns=t, novo_id=f"{base.id}:disp")
        extra: dict[str, Any] = {"plano_idade_ns": ev.idade_ns, "plano_idade_s": round(ev.idade_ns / NS, 3),
                                 "preco_real": preco_real, "ref_plano": ev.plano.ref,
                                 "decisao_origem": ev.plano.decisao_id}
        if it.ancora_t0 is not None:  # niveis ABSOLUTOS vistos em t0 ficam fixos (faixa assimetrica)
            cfg_p = self._cfg_planejador(conta)
            tick_pts = self.cfg.instrumento.tick_pontos
            alvo_min = cfg_p.k_custo * self._custos_base.ida_volta_pontos_estresse + tick_pts
            favor = self._favor_max(conta, base, ev.plano.plano, compra)
            ajustada, viol = ajustar_entrada_ancorada(it, preco_exec=preco_real, compra=compra,
                                                      stop_min=conta.motor.stop_min_pontos(), alvo_min=alvo_min,
                                                      favor_max=favor)
            extra.update(ancora_t0=it.ancora_t0, favor_max_pontos=favor, alvo_min_pontos=round(alvo_min, 10))
            if ajustada is None:
                motivos = tuple(f"ancoragem:{m}" for m in viol)
                conta.stats["bloqueadas"] += 1
                conta.stats["bloq:ancoragem"] += 1
                conta.retorno = {"ultima_entrada": "bloqueada", "motivos": [c for c in map(_codigo_retorno, viol) if c]}
                self.registro.evento_risco(t_ns=t, braco=conta.braco.id, tipo="entrada_bloqueada",
                                           detalhe=";".join(motivos)[:300])
                r0 = ResultadoDecisao(braco=conta.braco.id, modo="condicional", t0_ns=t, ciclo_id=it.id, acao=it.acao,
                                      resultado="entrada", intencao=it, hash_dossie=ev.plano.hash_dossie,
                                      proposta={"origem": "plano_condicional", **extra})
                self._gravar_decisao(r0, "bloqueada", motivos=motivos, intencao=it, gatilho="plano_disparado")
                return
            it = ajustada
        sl, tp = niveis_recalculados(compra, preco_real, it.stop_pontos or 0.0, it.alvo_pontos)
        extra.update(stop_recalculado=sl, alvo_recalculado=tp)
        r = self._executar_entrada(conta, it, t, hash_dossie=ev.plano.hash_dossie, gatilho="plano_disparado",
                                   proposta={"origem": "plano_condicional", **extra})
        prv = conta.previsoes.get(base.id)
        if prv and r.resultado == "executada":
            self.registro.previsao_ia(id=prv, executada=True, t_execucao_ns=t)

    # ===================================================================================== sombras ======
    def _replicar_sombras(self, conta_ref: ContaBraco, it: Intencao, t: int, hash_dossie: str) -> None:
        """Cada sombra de ``conta_ref`` replica a entrada JA AJUSTADA no MESMO tick, pelo proprio roteador e motor
        (mesmo ConfigRisco). O veto da sombra fica registrado (``sombra_vetada``): par incompleto."""
        for c in self.contas.values():
            b = c.braco
            if not isinstance(b, BracoSombra) or b.referencia != conta_ref.braco.id:
                continue
            try:
                it_s = b.derivar(it, f"{it.id}:{b.id}")
            except ValueError:
                continue
            # sombra ainda posicionada: o motor veta (sem aumento de posicao) e o par fica incompleto e registrado
            r = self._executar_entrada(c, it_s, t, hash_dossie=hash_dossie, gatilho="sombra",
                                       proposta={"origem": "sombra", "referencia": conta_ref.braco.id,
                                                 "politica": b.politica, "intencao_ref": it.id})
            if r.resultado != "executada":
                c.stats["sombra_vetada"] += 1
                c.stats["pares_incompletos_sombra"] += 1
                self.registro.evento_risco(t_ns=t, braco=b.id, tipo="sombra_vetada",
                                           detalhe=f"ref={it.id};{r.resultado}")

    def _observar_plano(self, conta_ref: ContaBraco, it: Intencao) -> None:
        """O controle do plano copia o plano COMPILADO inteiro de cada entrada enviada do braco de referencia."""
        braco = conta_ref.braco
        if not isinstance(braco, BracoPlanejador):
            return
        comp = braco.compilado(it.id.removesuffix(":disp"))
        if comp is None:
            return
        for c in self.contas.values():
            if isinstance(c.braco, BracoAleatorioPlano) and c.braco.referencia == braco.id:
                c.braco.observar_plano(comp)

    # ======================================================================================= entregas ===
    def _enfileirar(self, chegada_ns: int, res: ResultadoDecisao) -> None:
        self._seq += 1
        heapq.heappush(self._pend, (chegada_ns, self._seq, res))

    async def _entregar(self, t: int) -> None:
        while self._pend and self._pend[0][0] <= t:
            chegada, _, res = heapq.heappop(self._pend)
            try:
                self._processar_resultado(res, max(t, chegada))
            except Exception as e:  # defeito de processamento nunca derruba a sessao
                self.erros_braco[res.braco] += 1
                self.registro.incidente(t_ns=t, origem=f"orquestrador:{res.braco}", severidade="erro",
                                        detalhe=f"{type(e).__name__}: {e}")
                self.contas[res.braco].kill.registrar_erro(f"processamento:{res.braco}", t)

    def _registrar_ia(self, res: ResultadoDecisao) -> None:
        conta = self.contas[res.braco]
        for rc in res.chamadas:
            if rc.compartilhada:
                continue
            self.registro.chamada(rc.chamada, braco=rc.braco, t_ns=res.t0_ns)
            dono = self.contas.get(rc.braco)
            if dono is not None:
                dono.custo_ia_usd += rc.chamada.custo_usd
                dia_c = dia_sp(res.t0_ns).isoformat()
                dono.custo_ia_dia[dia_c] = dono.custo_ia_dia.get(dia_c, 0.0) + rc.chamada.custo_usd
                self.registro.orcamento(dia=dia_c, braco=dono.braco.id, gasto_usd=dono.custo_ia_dia[dia_c])
                dono.stats["chamadas_ia"] += 1
                if not rc.chamada.valida:
                    dono.stats["chamadas_ia_invalidas"] += 1
            g = self.gatilhos.get(self.contas[res.braco].braco.grupo_ciclo)
            if g is not None:
                g.registrar_custo(rc.chamada.custo_usd)
        for c in res.carimbos:
            papel = "revisor" if c.etapa == "revisao_ia" else "proponente"
            ia = next((rc.chamada for rc in res.chamadas if rc.papel == papel), None)
            self.registro.latencia(c, t_ns=res.t0_ns, modelo=ia.modelo_pedido if ia else "")
            if c.etapa == "proposta_ia" and c.braco == res.braco and c.t_fim_ns >= c.t_ini_ns:
                conta.latencias_ia_s.append((c.t_fim_ns - c.t_ini_ns) / NS)  # p50/p95 do proprio papel
        conta.stats["decisoes"] += 1

    def _gravar_decisao(self, res: ResultadoDecisao, resultado: str, *, motivos: Sequence[str] = (),
                        intencao: Intencao | None = None, risco: Any = None, gatilho: str = "",
                        proposta_extra: dict[str, Any] | None = None, did: str | None = None) -> str:
        did = did or self.registro.novo_id("dec")
        proposta = dict(res.proposta or {})
        if res.rotulos:
            proposta["rotulos"] = list(res.rotulos)
        if proposta_extra:
            proposta.update(proposta_extra)
        it = intencao or res.intencao
        self.registro.decisao(
            id=did, t_ns=res.t0_ns, braco=res.braco, modo=(it.modo if it is not None else res.modo),
            gatilho=gatilho, acao=res.acao.value, intencao=it, proposta=proposta or None, revisao=res.revisao,
            risco=risco, resultado=resultado, motivos=(*motivos, *(f"rotulo:{r}" for r in res.rotulos)),
            hash_dossie=res.hash_dossie)
        self._previsao(res, did, it)
        return did

    def _previsao(self, res: ResultadoDecisao, did: str, it: Intencao | None) -> None:
        """``previsoes_ia``: TODA entrada proposta pelo planejador (executada ou nao), com ``p_alvo`` e ``incerteza``
        declarados. Uma por intencao base; a execucao atualiza o MESMO id."""
        conta = self.contas.get(res.braco)
        prop = res.proposta
        if (conta is None or not isinstance(conta.braco, BracoPlanejador) or not res.eh_entrada or not prop
                or "p_alvo" not in prop):
            return
        chave = (res.intencao.id if res.intencao is not None else f"{res.ciclo_id}:{res.braco}")
        if chave in conta.previsoes:
            return
        base = res.intencao or it

        def _num(v: Any) -> float | None:
            return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) else None

        prv = self.registro.previsao_ia(
            decisao_id=did, t0_ns=res.t0_ns, braco=res.braco, lado="compra" if res.acao == Acao.COMPRAR else "venda",
            p_alvo=_num(prop.get("p_alvo")), incerteza=_num(prop.get("incerteza")),
            stop_pts=_num(base.stop_pontos) if base is not None else _num(prop.get("stop_pontos")),
            alvo_pts=_num(base.alvo_pontos) if base is not None else _num(prop.get("alvo_pontos")),
            tempo_s=(base.tempo_max_s if base is not None else prop.get("tempo_max_s")),
            ancora=res.preco_ref or None, executada=False, t_entrega_ns=res.t0_ns + max(0, int(res.latencia_ns)))
        if prv:
            conta.previsoes[chave] = prv
            while len(conta.previsoes) > 256:
                conta.previsoes.pop(next(iter(conta.previsoes)))

    def _resumo_seguro(self, proposta: Mapping[str, Any]) -> dict[str, Any]:
        """Base da reflexao pos-trade: so texto que passa em ``texto_seguro`` (injecao, anonimizacao, indicador)."""
        niveis = [p for _, p in self.hist]

        def ok(x: Any) -> bool:
            return isinstance(x, str) and not texto_seguro(x, niveis)

        return {"hipoteses": [h for h in list(proposta.get("hipoteses") or [])[:3] if ok(h)],
                "justificativa_curta": j if ok(j := str(proposta.get("justificativa_curta") or "")) else "",
                "incerteza": proposta.get("incerteza")}

    def _contar_veto(self, conta: ContaBraco, motivos: Sequence[str]) -> None:
        """Tentativas x episodios: o mesmo veto repetido em seguida conta UMA vez em ``episodios_veto``."""
        chave = tuple(motivos)
        conta.stats["tentativas_veto"] += 1
        if conta.ultimo_veto != chave:
            conta.stats["episodios_veto"] += 1
        conta.ultimo_veto = chave

    def _processar_resultado(self, res: ResultadoDecisao, agora: int) -> None:
        conta = self.contas[res.braco]
        self._registrar_ia(res)
        gat = ",".join(res.gatilhos)
        braco = conta.braco
        if isinstance(braco, BracoPlanejador) and res.reavaliar_em_s is not None and res.reavaliar_em_s > 0:
            conta.reavaliacao_ns = res.t0_ns + int(res.reavaliar_em_s) * NS
        if res.resultado != "bloqueada":
            conta.ultimo_bloqueio = None
        if res.resultado == "nao_operar":
            conta.stats["nao_operar"] += 1
            if braco.ia:
                self._gravar_decisao(res, "nao_operar", motivos=res.motivos, gatilho=gat)
            return
        if res.resultado == "manter_sem_ajuste":  # so registrado
            conta.stats["manter_sem_ajuste"] += 1
            self._gravar_decisao(res, "manter_sem_ajuste", motivos=res.motivos, gatilho=gat)
            return
        if res.resultado == "bloqueada":
            conta.stats["bloqueadas"] += 1
            if not braco.ia and conta.ultimo_bloqueio == res.motivos:
                return  # mesma recusa (ex.: modelo nao treinado) em ciclos seguidos: grava so a primeira
            if conta.ultimo_bloqueio != res.motivos:
                conta.stats["episodios_bloqueio"] += 1
            conta.ultimo_bloqueio = res.motivos
            for m in res.motivos:
                conta.stats[f"bloq:{_chave_bloqueio(m)}"] += 1
            self._gravar_decisao(res, "bloqueada", motivos=res.motivos, gatilho=gat)
            self.registro.evento_risco(t_ns=res.t0_ns, braco=res.braco, tipo="entrada_bloqueada",
                                       detalhe=";".join(res.motivos)[:300])
            if isinstance(braco, BracoPlanejador):
                codigos = [c for c in map(_codigo_retorno, res.motivos) if c]
                conta.retorno = {"ultima_decisao": "bloqueada", "motivos": codigos}
            return
        if res.rotulos:
            conta.rotulos.update(res.rotulos)
        it = res.intencao
        assert it is not None
        if res.eh_entrada and res.proposta:  # base da reflexao pos-trade (so texto ja validado da proposta)
            conta.resumo_ultima_entrada = self._resumo_seguro(res.proposta)
        pos = self.sim.posicao(res.braco)
        tick = self.ultimo_tick
        meio = _meio(tick) if tick else res.preco_ref
        # ---- acoes de posicao: nunca esperam revisao, nunca dependem do frescor da proposta ---------------------
        if res.resultado in ("manter", "ajuste", "saida"):
            self._acao_de_posicao(conta, res, it, pos, agora, gat, meio)
            return
        # ---- entradas e planos ----------------------------------------------------------------------------------
        if conta.kill.ativo:
            self._gravar_decisao(res, "bloqueada", motivos=("kill_switch_ativo",), gatilho=gat)
            return
        if pos.quantidade > 0 or conta.entrada_em_voo is not None:
            self._gravar_decisao(res, "bloqueada", motivos=("posicao_mudou_desde_t0",), gatilho=gat)
            return
        if res.resultado == "plano":
            self._armar_plano(conta, res, agora, gat)
            return
        fr = validar_frescor_entrada(t0_ns=res.t0_ns, agora_ns=agora, validade_s=res.validade_s,
                                     preco_ref=res.preco_ref, preco_atual=meio, desvio_max_pts=res.desvio_max_pts)
        if not fr.ok:
            conta.stats["bloqueadas"] += 1
            conta.stats["bloq:frescor"] += 1
            self._gravar_decisao(res, "bloqueada", motivos=fr.violacoes, gatilho=gat)
            self.registro.evento_risco(t_ns=agora, braco=res.braco, tipo="entrada_bloqueada",
                                       detalhe="frescor: " + ";".join(fr.violacoes))
            return
        compra = it.acao == Acao.COMPRAR
        preco_atual = (tick.ask if compra else tick.bid) if tick else meio
        it2 = it.model_copy(update={"preco_referencia": preco_atual or meio})
        self.registro.latencia(
            CarimboLatencia(etapa="decisao_total", braco=res.braco, t_ini_ns=res.t0_ns, t_fim_ns=agora,
                            injetado=any(c.chamada.modelo_servido.endswith("FALSO") for c in res.chamadas)),
            t_ns=agora)
        self._executar_entrada(conta, it2, agora, hash_dossie=res.hash_dossie, gatilho=gat, res=res)

    def _acao_de_posicao(self, conta: ContaBraco, res: ResultadoDecisao, it: Intencao, pos: Posicao, agora: int,
                         gat: str, meio: float) -> None:
        """MANTER / ajuste / ENCERRAR, presos a posicao vista em t0 (ticket e quantidade, C5). A referencia e o
        preco EXECUTAVEL de saida; so vira ``executada`` se aplicado (senao ``nao_aplicada``, C3)."""
        if pos.quantidade <= 0:
            self._gravar_decisao(res, "ignorada", motivos=("sem_posicao_aberta",), gatilho=gat)
            return
        if (pos.ticket, pos.quantidade) != (res.posicao_ticket_t0, res.quantidade_t0):
            conta.stats["ignoradas_posicao_mudou"] += 1
            self._gravar_decisao(res, "ignorada", motivos=("posicao_mudou_desde_t0",), gatilho=gat)
            return
        if res.resultado == "saida":
            ok = self._sair(conta, "encerrar", agora, ticket_esperado=res.posicao_ticket_t0)
            self._gravar_decisao(res, "executada" if ok else "ignorada",
                                 motivos=() if ok else ("saida_ja_reivindicada",), gatilho=gat)
            return
        it2 = it.model_copy(update={"preco_referencia": self._preco_saida(pos, self.ultimo_tick, meio)})
        if res.resultado == "ajuste" and it2.ajuste is not None:
            resultado, r, motivos = self._executar_ajuste(conta, it2, pos, agora, origem="ia", acao="ajuste_ia",
                                                          regra="ia")
            if isinstance(conta.braco, BracoPlanejador):
                conta.retorno = {"ultimo_ajuste": resultado, "motivos": [m.split(":", 1)[0] for m in motivos]}
            self._gravar_decisao(res, resultado, motivos=motivos, risco=r.decisao if r is not None else None,
                                 gatilho=gat, intencao=it2)
            return
        r2 = conta.roteador.executar_intencao(it2, pos, agora)  # MANTER legado (stop_pontos medido da referencia)
        if not r2.decisao.aprovada:
            resultado, motivos2 = "vetada", tuple(r2.decisao.motivos)
        elif r2.motivo.startswith("protecao_nao_aplicada"):
            resultado, motivos2 = "nao_aplicada", (r2.motivo,)
        else:
            resultado, motivos2 = "executada", ()
            if r2.niveis_aplicados is not None:
                est = conta.meta.estado if conta.meta is not None else None
                if est is not None:
                    est.origem_ultimo_stop = "ia"
                    est.ajustes_ia += 1
                    sincronizar_posicao(est, self.sim.posicao(res.braco))
                self._espelhar(conta, agora, "espelhar_protecao", *r2.niveis_aplicados, agora)
        self._gravar_decisao(res, resultado, motivos=motivos2, risco=r2.decisao, gatilho=gat, intencao=it2)

    def _armar_plano(self, conta: ContaBraco, res: ResultadoDecisao, agora: int, gat: str) -> None:
        it = res.intencao
        assert it is not None and res.plano is not None
        if conta.motor.evento_proximo(agora) is not None:
            self._gravar_decisao(res, "bloqueada", motivos=("janela_de_evento",), gatilho=gat)
            return
        if agora > res.plano.expira_ns:
            self._gravar_decisao(res, "bloqueada", motivos=("plano_ja_expirado",), gatilho=gat)
            return
        tick = self.ultimo_tick
        estado = (EstadoPlano(t_ns=agora, preco=_meio(tick), spread_pts=max(tick.ask - tick.bid, 0.0))
                  if tick is not None else None)
        did = self.registro.novo_id("dec")
        base = it.model_copy(update={"plano": None})
        try:
            eventos = conta.planos.armar(base, res.plano, ref=res.preco_ref, t0_ns=res.t0_ns, t_ns=agora,
                                         assinatura=AssinaturaPosicao.de(self.sim.posicao(res.braco)), decisao_id=did,
                                         hash_dossie=res.hash_dossie, estado_atual=estado)
        except ValueError as e:  # plano fora da DSL: bloqueia a entrada (nunca derruba a sessao)
            self._gravar_decisao(res, "bloqueada", motivos=("plano_invalido",), gatilho=gat, did=did,
                                 proposta_extra={"erro_plano": str(e)[:200]})
            return
        inval = next((ev for ev in eventos if ev.tipo == "invalidado_ao_armar"), None)
        if inval is not None:
            conta.stats["bloqueadas"] += 1
            self._gravar_decisao(res, "bloqueada", motivos=("invalidado_ao_armar",), gatilho=gat, did=did,
                                 proposta_extra={"invalidacao": inval.motivo})
        else:
            self._gravar_decisao(res, "plano_armado", gatilho=gat, did=did,
                                 proposta_extra={"expira_em_s": round((res.plano.expira_ns - agora) / NS, 1)})
        for ev in eventos:
            self._registrar_evento_plano(ev)

    def _executar_entrada(self, conta: ContaBraco, it: Intencao, t: int, *, hash_dossie: str, gatilho: str,
                          res: ResultadoDecisao | None = None,
                          proposta: dict[str, Any] | None = None) -> ExecucaoRegistrada:
        braco = conta.braco.id
        pos = self.sim.posicao(braco)
        tick = self.ultimo_tick
        spread = max((tick.ask - tick.bid), 0.0) if tick else 0.0
        self._alimentar_spread(conta, spread)  # distancia minima de stop/alvo com o spread corrente
        if (m := self.bloqueio.motivo(t)):  # lacuna de sequencia / dado velho: bloqueia a ENTRADA
            conta.stats["bloqueadas"] += 1
            conta.stats[f"bloq:dado:{m}"] += 1
            r0 = res or ResultadoDecisao(braco=braco, modo="condicional", t0_ns=t, ciclo_id=it.id, acao=it.acao,
                                         resultado="entrada", intencao=it, proposta=proposta, hash_dossie=hash_dossie)
            self.registro.evento_risco(t_ns=t, braco=braco, tipo="entrada_bloqueada", detalhe=f"dado:{m}")
            did = self._gravar_decisao(r0, "bloqueada", motivos=(f"dado:{m}",), intencao=it, gatilho=gatilho)
            return ExecucaoRegistrada(did, "bloqueada", None)
        t_ini = time.perf_counter_ns()
        r: ResultadoExecucao = conta.roteador.executar_intencao(
            it, pos, t, {"spread_ticks": spread / self.cfg.instrumento.tick_pontos})
        dur = time.perf_counter_ns() - t_ini
        self.registro.latencia(CarimboLatencia(etapa="risco_oms", braco=braco, t_ini_ns=0, t_fim_ns=dur), t_ns=t)
        enviada = r.envio is not None and r.envio.enviado and r.envio.ordem.estado.value != "REJEITADA"
        resultado = "executada" if enviada else ("vetada" if not r.decisao.aprovada else "nao_enviada")
        motivos = r.decisao.motivos if not r.decisao.aprovada else ((r.motivo,) if r.motivo and not enviada else ())
        if enviada:
            conta.entrada_em_voo, conta.t_envio_entrada_ns = it, t
            conta.ultimo_veto = None
            conta.stats["entradas"] += 1
            for c in self.contas.values():  # controles copiam a geometria dos bracos de referencia
                if isinstance(c.braco, BracoAleatorio) and braco in self.cfg.referencias_frequencia:
                    c.braco.observar(it)
        else:
            conta.stats["vetadas"] += 1
            if not r.decisao.aprovada:
                self._contar_veto(conta, motivos)
            self.registro.evento_risco(t_ns=t, braco=braco, tipo="entrada_vetada", detalhe=";".join(motivos)[:300])
        if isinstance(conta.braco, BracoPlanejador):
            conta.retorno = {"ultima_entrada": resultado, "motivos": [str(m).split(":", 1)[0] for m in motivos]}
        if res is not None:
            did = self._gravar_decisao(res, resultado, motivos=motivos, intencao=it, risco=r.decisao, gatilho=gatilho)
        else:
            fake = ResultadoDecisao(braco=braco, modo="condicional", t0_ns=t, ciclo_id=it.id, acao=it.acao,
                                    resultado="entrada", intencao=it, proposta=proposta, hash_dossie=hash_dossie)
            did = self._gravar_decisao(fake, resultado, motivos=motivos, intencao=it, risco=r.decisao,
                                       gatilho=gatilho)
        if enviada:
            conta.decisao_entrada_id = did
            if isinstance(conta.braco, BracoPlanejador):  # sombras no MESMO tick e controle do plano
                self._replicar_sombras(conta, it, t, hash_dossie)
                self._observar_plano(conta, it)
        return ExecucaoRegistrada(did, resultado, r)

    # ====================================================================================== ciclos ======
    def _eventos_pendentes(self) -> bool:
        return any(c.eventos_posicao or c.eventos_gestao for c in self.contas.values())

    @staticmethod
    def _passiva(conta: ContaBraco) -> bool:
        """Sombras (passivas) nunca entram em grupos nem gatilhos: so replicam a entrada do braco de referencia."""
        return bool(getattr(conta.braco, "passivo", False))

    def _grupo_ativo(self, contas: list[ContaBraco], t: int) -> bool:
        for c in contas:
            if c.kill.ativo or self._passiva(c):
                continue
            if self.sim.posicao(c.braco.id).quantidade > 0:
                return True
            if self.bloqueio.bloqueado(t) or not c.motor.dentro_da_janela(t) or c.motor.evento_proximo(t) is not None:
                continue
            if c.braco.ia or c.braco.familia == "quant":
                # IA e quant sem posicao so sao chamados se ALGUMA entrada ainda pode passar no risco (C19: nao paga
                # chamada de IA que o motor vetaria de qualquer jeito). Controles mantem o fluxo de sorteios.
                if c.motor.entradas_possiveis(c.braco.id, t):
                    continue
            return True
        return False

    async def _ciclos(self, t: int) -> None:
        if t < self._prox_checagem_ns and not self._eventos_pendentes():
            return
        self._prox_checagem_ns = t + int(self.cfg.checagem_s * NS)
        if self.cfg.exigir_aquecimento and not self.pipeline.aquecido(t):
            return
        snap = self._snapshot(t)
        if snap is None:
            return
        dia = dia_sp(t)
        for grupo, contas in self.grupos.items():
            if all(self._passiva(c) for c in contas) or not self._grupo_ativo(contas, t):
                continue
            gat = self.gatilhos[grupo]
            ate_s = contas[0].motor.limites_janela_s(dia)[1] - segundos_do_dia(t)
            reav = [c.reavaliacao_ns for c in contas if c.reavaliacao_ns is not None]
            est = EstadoGatilho(
                t_ns=t, modelo_saida=snap.saida_principal, z_statarb=snap.z, noticias_novas=snap.noticias_novas,
                eventos_posicao=tuple(e for c in contas for e in c.eventos_posicao),
                segundos_restantes=float(max(ate_s, 0)),
                eventos_gestao=tuple(e for c in contas for e in c.eventos_gestao),
                reavaliacao_ns=min(reav) if reav else None,
                com_posicao=any(self.sim.posicao(c.braco.id).quantidade > 0 for c in contas),
                plano_terminou=any(c.plano_terminou for c in contas))
            disparos = gat.avaliar(est)
            if not disparos:
                continue
            gat.marcar_chamada(t)
            for c in contas:
                c.eventos_posicao.clear()
                c.eventos_gestao.clear()
                c.plano_terminou = False
                if c.reavaliacao_ns is not None and t >= c.reavaliacao_ns:
                    c.reavaliacao_ns = None
            await self._ciclo(contas, snap, t, tuple(d.nome for d in disparos))

    def _snapshot(self, t: int) -> Snapshot | None:
        if self._snap is not None and self._snap.t_ns == t:
            return self._snap
        tick = self.ultimo_tick
        if tick is None:
            return None
        t_ini = time.perf_counter()
        try:
            vec = self.pipeline.vetor(t)
        except ValueError:
            return None
        vetor = vec.valores
        modelos: dict[str, dict[str, Any]] = {}
        principal: dict[str, float] = {}
        for nome, m in self.modelos.items():
            try:
                saida = {k: float(v) for k, v in m.prever(vec).items()}
            except Exception:  # modelo nao treinado / falha: sem saida, com o status honesto
                saida = {}
            modelos[nome] = {"status": getattr(m, "status", "nao_treinado"), "saida": saida}
            if not principal and saida:
                principal = saida
        statarb: dict[str, float] = {}
        z = 0.0
        aux: dict[str, float] = {}
        if self.precos_aux is not None:  # UMA consulta por snapshot (a vista sintetica tem estado)
            try:
                aux = dict(self.precos_aux(tick))
            except Exception:
                aux = {}
        if self.statarb is not None:
            precos = {self.cfg.instrumento.simbolo: _meio(tick)} | aux
            try:
                bruto = self.statarb.sinal(t, precos)
            except Exception:
                bruto = {}
            nomes: dict[str, str] = {}
            for k, v in bruto.items():  # anonimiza o nome do ativo de referencia (z_BOVA11 -> z_ref1)
                kk = k
                for pref in ("z_", "beta_"):
                    if k.startswith(pref) and k not in ("z",):
                        ref = k[len(pref):]
                        nomes.setdefault(ref, f"ref{len(nomes) + 1}")
                        kk = pref + nomes[ref]
                statarb[kk] = float(v)
            z = float(bruto.get("z", 0.0))
        noticias: list[dict[str, Any]] = []
        novas: list[float] = []
        if self.noticias_fn is not None:
            noticias = [n for n in self.noticias_fn(t) if n.get("t_disponivel_ns", t) <= t]
            for n in noticias:
                nid = str(n.get("id", id(n)))
                if nid not in self._noticias_vistas:
                    self._noticias_vistas.add(nid)
                    novas.append(float(n.get("relevancia", 0.0)))
        self._snap = Snapshot(t, vetor, modelos, statarb, z, principal, _meio(tick), tick.bid, tick.ask, noticias,
                              novas, (time.perf_counter() - t_ini) * 1000, aux)
        return self._snap

    def _regime(self, snap: Snapshot, conta: ContaBraco, t: int) -> str:
        if conta.motor.evento_proximo(t) is not None:
            return "evento_proximo"
        r = snap.vetor.get("rv_razao_1m_30m", float("nan"))
        if r != r:
            return "indefinido"
        return "agitado" if r > 2.0 else ("calmo" if r < 0.5 else "normal")

    def _niveis_aux(self, snap: Snapshot) -> tuple[float, ...]:
        """Precos absolutos dos ativos auxiliares do instante (nunca podem aparecer no dossie, C29)."""
        return tuple(float(v) for v in snap.precos_aux.values() if isinstance(v, int | float) and math.isfinite(v))

    def _dossie(self, conta: ContaBraco, snap: Snapshot, t: int) -> dict[str, Any]:
        braco = conta.braco.id
        pos = self.sim.posicao(braco)
        posicao: dict[str, Any] | None = None
        if pos.quantidade > 0 and pos.lado is not None:  # stop e alvo ATUAIS do simulador (C4, v1.1)
            posicao = {"lado": pos.lado.value, "quantidade": pos.quantidade, "preco_medio": pos.preco_medio,
                       "aberta_ns": pos.aberta_ns,
                       "stop_pontos": round(abs(pos.preco_medio - pos.stop), 10) if pos.stop is not None else None,
                       "alvo_pontos": round(abs(pos.alvo - pos.preco_medio), 10) if pos.alvo is not None else None}
        if isinstance(conta.braco, BracoPlanejador):
            return self._dossie_v2(conta, snap, t, posicao)
        est = conta.motor.contab.estado(braco, t)
        # v1.1: stop maximo EFETIVO para q=1 (mesma conta do veto, C2) em vez do teto bruto da politica
        limites = limites_legado(conta.motor.envelope(braco, t), est, self.cfg.risco)
        dia = dia_sp(t)
        de, _, zer = conta.motor.limites_janela_s(dia)
        seg = segundos_do_dia(t)
        dono = self._dono_memoria(conta)
        feats = {k: v for k, v in snap.vetor.items() if k in FEATURES_DOSSIE}
        return montar_dossie(
            t0_ns=t, ticks=list(self.hist), spread_pontos=max(snap.ask - snap.bid, 0.0), features=feats,
            features_permitidas=nomes_permitidos(), modelos=snap.modelos, statarb=snap.statarb,
            noticias=snap.noticias, regime=self._regime(snap, conta, t), posicao=posicao,
            limites=limites, memoria=self._memoria_do_dossie(dono, t),
            minutos_desde_abertura=max(0, (seg - de) // 60),  # v1.1: abertura da janela de entradas (C18)
            minutos_para_zeragem=max(0, (zer - seg) // 60), tick_pontos=self.cfg.instrumento.tick_pontos,
            niveis_extras_absolutos=self._niveis_aux(snap))

    def _envelope_conta(self, conta: ContaBraco, t: int) -> EnvelopeDecisao:
        """Envelope DERIVADO (so leitura) do motor da propria conta: pontos, unidades e segundos."""
        braco = conta.braco.id
        return conta.motor.envelope(braco, t, posicao=self.sim.posicao(braco),
                                    margem_s=int(self._cfg_planejador(conta).margem_zeragem_s))

    def _custos(self, spread_pts: float, t: int) -> CustosPublicos:
        """Custos publicos com o slippage medio de stop CAUSAL (so pregoes anteriores ao de ``t``)."""
        hoje = dia_sp(t).isoformat()
        return custos_publicos(self.perfil, spread_pts=spread_pts,
                               slippages_stop_anteriores=[x for d, x in self._slip_stop if d < hoje])

    def _latencia_conta(self, conta: ContaBraco) -> tuple[float, float] | None:
        lat = sorted(conta.latencias_ia_s)
        if not lat:
            return None
        p95 = lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)]
        return round(statistics.median(lat), 3), round(p95, 3)

    def _min_para_evento(self, t: int) -> int | None:
        futuros = [e.t_ns for e in self.eventos if e.t_ns >= t]
        return int((min(futuros) - t) // (60 * NS)) if futuros else None

    @staticmethod
    def _plano_vigente(conta: ContaBraco, t: int) -> dict[str, Any] | None:
        p = conta.planos.armado
        if p is None:
            return None
        return {"acao": p.base.acao.value, "idade_s": max(0, int((t - p.t_armado_ns) // NS)),
                "expira_em_s": max(0, int((p.plano.expira_ns - t) // NS)),
                "gatilhos": [f"{c.tipo}({c.valor})" for c in p.plano.gatilhos],
                "invalidacoes": [f"{c.tipo}({c.valor})" for c in p.plano.invalidacoes]}

    def _dossie_v2(self, conta: ContaBraco, snap: Snapshot, t: int,
                   posicao: dict[str, Any] | None) -> dict[str, Any]:
        """Dossie v2 do planejador: sem limites nem dinheiro de limite; envelope derivado, custos publicos, sessao,
        mercado, posicao com niveis ATUAIS, plano vigente, retorno (so codigos) e latencia do proprio papel."""
        braco = conta.braco.id
        cfg_p = self._cfg_planejador(conta)
        pos = self.sim.posicao(braco)
        env = self._envelope_conta(conta, t)
        spread = max(snap.ask - snap.bid, 0.0)
        dia = dia_sp(t)
        de, ate, zer = conta.motor.limites_janela_s(dia)
        seg = segundos_do_dia(t)
        sessao = {"min_desde_abertura": max(0, (seg - de) // 60), "de": f"{de // 3600:02d}:{de % 3600 // 60:02d}",
                  "min_para_fim_entradas": max(0, (ate - seg) // 60), "min_para_zeragem": max(0, (zer - seg) // 60),
                  "min_para_evento": self._min_para_evento(t)}
        mk = conta.motor.mercado
        mercado = {"feed_ok": bool(mk.feed_ok and not self.bloqueio.bloqueado(t)), "leilao": bool(mk.leilao),
                   "vol_extrema": bool(mk.vol_extrema)}
        lat = self._latencia_conta(conta)
        latencia = {"p50_s": lat[0], "p95_s": lat[1], "n": len(conta.latencias_ia_s)} if lat else {}
        meta = conta.meta
        ultimo = self._preco_saida(pos, self.ultimo_tick, snap.meio) if pos.quantidade > 0 else snap.meio
        extras = secoes_planejador(
            pos=pos, est=meta.estado if meta is not None else None, env=env, expor=cfg_p.expor_envelope,
            perfil=self.perfil, custos=self._custos(spread, t), sessao=sessao, mercado=mercado,
            plano_vigente=self._plano_vigente(conta, t), retorno=conta.retorno, latencia=latencia,
            ultimo_preco=ultimo, t_ns=t, gestao=meta.gestao if meta is not None else None)
        feats = {k: v for k, v in snap.vetor.items() if k in FEATURES_DOSSIE_V2}
        return montar_dossie(
            t0_ns=t, ticks=list(self.hist), spread_pontos=spread, features=feats,
            features_permitidas=nomes_permitidos(), modelos=snap.modelos, statarb=snap.statarb,
            noticias=snap.noticias, regime=self._regime(snap, conta, t), posicao=posicao,
            memoria=self._memoria_do_dossie(self._dono_memoria(conta), t),
            minutos_desde_abertura=max(0, (seg - de) // 60), minutos_para_zeragem=max(0, (zer - seg) // 60),
            tick_pontos=self.cfg.instrumento.tick_pontos, niveis_extras_absolutos=self._niveis_aux(snap),
            versao=2, extras=extras)

    def _memoria_do_dossie(self, dono: ContaBraco, t: int) -> list[dict[str, Any]]:
        """Memoria de operacoes do dono; com memoria reflexiva, anexa as licoes JA DISPONIVEIS em ``t``."""
        mem = list(dono.memoria)
        if self.memoria_reflexiva is None:
            return mem
        return self.memoria_reflexiva.enriquecer(mem, dono.braco.id, t)

    async def _liquidar_reflexoes(self, t: int) -> None:
        """Pede (e custeia) as reflexoes das operacoes cujo atraso de liquidacao ja passou. Opcional e a prova de
        falha: um erro aqui nunca bloqueia o ciclo nem toca em risco ou ordens."""
        if self.memoria_reflexiva is None or self.servico_reflexao is None or self._liquidando:
            return
        self._liquidando = True
        try:
            niveis = [p for _, p in self.hist]
            chamadas = await liquidar_pendentes(self.memoria_reflexiva, t, self.servico_reflexao, niveis_absolutos=niveis)
            for braco_id, ch in chamadas:  # custo atribuido ao braco dono da memoria
                dono = self.contas.get(braco_id)
                self.registro.chamada(ch, braco=braco_id, t_ns=t)
                if dono is not None:
                    dono.custo_ia_usd += ch.custo_usd
                    dia_c = dia_sp(t).isoformat()
                    dono.custo_ia_dia[dia_c] = dono.custo_ia_dia.get(dia_c, 0.0) + ch.custo_usd
                    self.registro.orcamento(dia=dia_c, braco=braco_id, gasto_usd=dono.custo_ia_dia[dia_c])
            for e in self.memoria_reflexiva.exportar():
                if e["estado"] in ("liquidada", "descartada") and e["t_liquidacao_ns"] == t:
                    self.registro.auditar("reflexao_pos_trade", e)
        except Exception as exc:  # defensivo: reflexao e opcional
            self.registro.incidente(t_ns=t, origem="reflexao", severidade="aviso", detalhe=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            self._liquidando = False

    @staticmethod
    def _dossie_leve(snap: Snapshot) -> dict[str, Any]:
        """Bracos SEM IA nao enviam nada a ninguem: so as saidas dos modelos/StatArb que eles leem."""
        return {
            "modelos": {n: {"status": m["status"], "saida": {k: round(v, 4) for k, v in m["saida"].items()}}
                        for n, m in snap.modelos.items()},
            "statarb": {k: round(v, 3) for k, v in snap.statarb.items()}}

    def _dono_memoria(self, conta: ContaBraco) -> ContaBraco:
        """Memoria de operacoes do DONO do servico de proposta (mantem o dossie de B e C identico: chamada
        compartilhada). O planejador usa o dono do servico de planejamento (P e PC pareados). Bracos sem IA usam a
        propria."""
        prop = getattr(conta.braco, "proponente", None)
        if prop is None and isinstance(conta.braco, BracoPlanejador):
            prop = conta.braco.servico
        if prop is not None:
            dono = prop.dono_de(conta.braco.modo)
            if dono in self.contas:
                return self.contas[dono]
        return conta

    async def _ciclo(self, contas: list[ContaBraco], snap: Snapshot, t: int, disparos: tuple[str, ...]) -> None:
        await self._liquidar_reflexoes(t)
        ciclo_id = self.registro.novo_id("cic")
        tick = self.ultimo_tick
        assert tick is not None
        ctxs: list[tuple[ContaBraco, ContextoDecisao]] = []
        d_lider: dict[str, Any] | None = None
        for c in contas:
            pos_c = self.sim.posicao(c.braco.id)
            if self._passiva(c) or (c.kill.ativo and pos_c.quantidade <= 0):
                continue
            try:
                if c.braco.ia and pos_c.quantidade <= 0:
                    # dossie do LIDER do grupo, compartilhado SO entre bracos SEM posicao: B e C zerados usam a
                    # MESMA proposta (chamada pareada); o braco posicionado tem dossie proprio (C6, v1.1)
                    if d_lider is None:
                        d_lider = self._dossie(c, snap, t)
                    d = d_lider
                elif c.braco.ia:
                    d = self._dossie(c, snap, t)  # cache por hash junta casos identicos
                else:
                    d = self._dossie_leve(snap)
            except ErroDossie as e:
                self.registro.incidente(t_ns=t, origem=f"dossie:{c.braco.id}", severidade="erro", detalhe=str(e)[:300])
                continue
            extras: dict[str, Any] = {}
            if isinstance(c.braco, BracoPlanejador):  # so o planejador ve o envelope DERIVADO (nunca o ConfigRisco)
                extras = {"envelope": self._envelope_conta(c, t),
                          "custo_ida_volta_pontos": self._custos_base.ida_volta_pontos_estresse,
                          "plano_vigente": c.planos.armado is not None, "latencia_ia_s": self._latencia_conta(c)}
            ctxs.append((c, ContextoDecisao(
                t0_ns=t, dossie=d, hash_dossie=hash_dossie(d), preco_ref=snap.meio, bid=tick.bid, ask=tick.ask,
                posicao=pos_c, ciclo_id=ciclo_id, gatilhos=disparos,
                tick_pontos=self.cfg.instrumento.tick_pontos,
                odds=self._odds if self.avaliador_odds is not None else None, **extras)))
        if not ctxs:
            return
        self._gravar_features(ctxs, snap, t, ciclo_id, disparos)
        self.registro.latencia(
            CarimboLatencia(etapa="features_modelo", braco="*", t_ini_ns=0, t_fim_ns=int(snap.wall_ms * 1e6)), t_ns=t)
        # tick -> gatilho: o relogio do servidor nao e sincronizado, entao e um LIMITE (recebido - evento)
        self.registro.latencia(
            CarimboLatencia(etapa="dado_para_gatilho", braco="*", t_ini_ns=tick.t_utc_ns,
                            t_fim_ns=max(tick.t_recebido_ns, tick.t_utc_ns), injetado=self.cfg.sintetico), t_ns=t)
        if self.cfg.modo_tempo == "real":
            tarefa = asyncio.create_task(self._ciclo_real(ctxs))
            self._tarefas.add(tarefa)
            tarefa.add_done_callback(self._tarefas.discard)
            return
        resultados = await self._executar_bracos(ctxs)
        for res in resultados:
            self._enfileirar(t + res.latencia_ns, res)

    def _gravar_features(self, ctxs: list[tuple[ContaBraco, ContextoDecisao]], snap: Snapshot, t: int,
                         ciclo_id: str, disparos: tuple[str, ...]) -> None:
        """Grava o vetor de features (lista permitida) do ciclo: fonte do PSI. Falha aqui nunca derruba o ciclo."""
        try:
            vals = compactar_features(snap.vetor, nomes_permitidos())
            if vals:
                self.registro.features_ciclo(t_ns=t, ciclo_id=ciclo_id, bracos=[c.braco.id for c, _ in ctxs],
                                             gatilhos=disparos, valores=vals)
        except Exception as e:  # defensivo: monitoramento e informativo
            self.registro.incidente(t_ns=t, origem="features_ciclo", severidade="aviso",
                                    detalhe=f"{type(e).__name__}: {e}"[:300])

    def _odds(self, ctx: ContextoDecisao, lado: Lado, stop_pontos: float, alvo_pontos: float) -> Any:
        """Probabilidades do plano PROPOSTO (so sessoes anteriores ao dia de t0: sem vazamento). None se desligado."""
        if self.avaliador_odds is None:
            return None
        return self.avaliador_odds.avaliar(t0_ns=ctx.t0_ns, lado=lado, stop_pontos=stop_pontos,
                                           alvo_pontos=alvo_pontos, preco_ref=ctx.preco_ref)

    async def _executar_bracos(self, ctxs: list[tuple[ContaBraco, ContextoDecisao]]) -> list[ResultadoDecisao]:
        brutos = await asyncio.gather(*[c.braco.adecidir(ctx) for c, ctx in ctxs], return_exceptions=True)
        out: list[ResultadoDecisao] = []
        for (c, ctx), r in zip(ctxs, brutos, strict=True):
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, BaseException):  # defeito no braco: bloqueia a entrada e registra o incidente
                self.erros_braco[c.braco.id] += 1
                self.registro.incidente(t_ns=ctx.t0_ns, origem=f"braco:{c.braco.id}", severidade="erro",
                                        detalhe=f"{type(r).__name__}: {r}"[:300])
                c.kill.registrar_erro(f"braco:{c.braco.id}", ctx.t0_ns)
                r = ResultadoDecisao(braco=c.braco.id, modo=c.braco.modo, t0_ns=ctx.t0_ns, ciclo_id=ctx.ciclo_id,
                                     acao=Acao.NAO_OPERAR, resultado="bloqueada",
                                     motivos=(f"erro_braco:{type(r).__name__}",), hash_dossie=ctx.hash_dossie)
            out.append(r)
        return out

    async def _ciclo_real(self, ctxs: list[tuple[ContaBraco, ContextoDecisao]]) -> None:
        resultados = await self._executar_bracos(ctxs)
        agora = self._agora()
        for res in resultados:
            self._enfileirar(agora, res)

    async def aguardar_tarefas(self, timeout_s: float = 5.0) -> None:
        if self._tarefas:
            await asyncio.wait(list(self._tarefas), timeout=timeout_s)

    # =================================================================================== equity =========
    def _gravar_equity(self, conta: ContaBraco, t: int) -> None:
        e = conta.motor.contab.estado(conta.braco.id, t)
        self.registro.equity(braco=conta.braco.id, t_ns=t, equity_brl=e.equity, drawdown_brl=e.drawdown)

    def _equity_periodico(self, t: int) -> None:
        if t < self._prox_equity_ns:
            return
        self._prox_equity_ns = t + int(self.cfg.equity_a_cada_s * NS)
        for conta in self.contas.values():
            self._gravar_equity(conta, t)

    # ================================================================================ sessao ============
    def iniciar(self, manifesto: ManifestoExperimento | None = None, t_ns: int | None = None) -> None:
        """Grava o inicio da sessao: faixa sintetica, manifesto do experimento e modelos."""
        t = self._agora() if t_ns is None else t_ns
        self._sincronizar_orcamento(t, forcar=True)
        self.registro.auditar("sessao_inicio", {"sessao": self.cfg.sessao_id, "rotulo": self.cfg.rotulo,
                                                "sintetico": self.cfg.sintetico, "t_ns": t,
                                                "bracos": [c.braco.manifesto() for c in self.contas.values()]})
        if self.cfg.sintetico:
            self.registro.incidente(
                t_ns=t, origem=ORIGEM_SINTETICO, severidade="info",
                detalhe="SIMULACAO sobre dados SINTETICOS: nenhum resultado aqui prova lucratividade.")
        if manifesto is not None:
            self.registro.experimento(manifesto, t_ns=t, status="em_andamento")
        for nome, m in self.modelos.items():
            hd = str(getattr(m, "hash_dados", ""))
            self.registro.modelo(
                nome=nome, versao=str(getattr(m, "versao", "") or hd[:8] or "0"),
                status=str(getattr(m, "status", "nao_treinado")), hash_dados=hd,
                hash_codigo=str(getattr(m, "hash_codigo", "") or _hash_codigo_modelos()),
                treinado_ate_ns=int(getattr(m, "treinado_ate_ns", 0)), metricas=dict(getattr(m, "metricas_oos", {})))

    async def encerrar(self, manifesto: ManifestoExperimento | None = None) -> None:
        """Fim da sessao: aguarda IA pendente, zera o que restar, grava equity, orcamento e o status final."""
        await self.aguardar_tarefas()
        t = max(self.t_ultimo_ns, self._agora())
        while self._pend:  # respostas de IA ainda em voo ao fim dos dados: descartadas e registradas
            _, _, res = heapq.heappop(self._pend)
            self._registrar_ia(res)
            self._gravar_decisao(res, "bloqueada", motivos=("sessao_encerrada_antes_da_resposta",),
                                 gatilho=",".join(res.gatilhos))
        for conta in self.contas.values():
            e = conta.planos.cancelar("fim_da_sessao", t)
            if e is not None:
                self._registrar_evento_plano(e)
        abertas = [c for c in self.contas.values() if self.sim.posicao(c.braco.id).quantidade > 0]
        for conta in abertas:
            self._sair(conta, "zeragem", t, forcado=True, detalhe="fim_da_sessao")
        if abertas and self.ultimo_tick is not None:
            # dados acabaram: reaplica o ultimo tick 1 ms depois para a saida de zeragem poder executar
            u = self.ultimo_tick
            t2 = max(t, u.t_utc_ns) + self.cfg.latencia_exec_ns + 1_000_000
            self._avancar(t2)
            fim = u.model_copy(update={"t_utc_ns": t2, "t_recebido_ns": t2, "t_servidor_ms": t2 // 1_000_000})
            self._rotear_eventos(self.sim.on_tick(fim), t2)
            t = t2
        if self.espelho is not None:
            try:
                self.espelho.encerrar(t)
            except Exception as e:  # noqa: BLE001
                self.registro.incidente(t_ns=t, origem="espelho", severidade="erro", detalhe=f"encerrar: {e}"[:300])
        for conta in self.contas.values():
            self._gravar_equity(conta, t)
        for conta in self.contas.values():
            for dia_c, gasto in conta.custo_ia_dia.items():
                if gasto > 0:
                    self.registro.orcamento(dia=dia_c, braco=conta.braco.id, gasto_usd=gasto)
        if manifesto is not None:
            self.registro.experimento(manifesto, t_ns=t, status="concluido")
        self.registro.auditar("sessao_fim", {"sessao": self.cfg.sessao_id, "t_ns": t, "resumo": self.resumo()})
        self.encerrada = True

    # =================================================================================== resumo =========
    def resumo(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for braco, conta in self.contas.items():
            trades = self.sim.carteira(braco).trades
            s = conta.stats
            out[braco] = {
                "modo": conta.braco.modo, "familia": conta.braco.familia, "ia": conta.braco.ia,
                "decisoes": s["decisoes"], "entradas": s["entradas"], "vetadas": s["vetadas"],
                "bloqueadas": s["bloqueadas"], "trades": len(trades),
                "liquido_brl": round(sum(t.liquido_brl for t in trades), 2),
                "planos_armados": s["plano_armado"], "planos_disparados": s["plano_disparado"],
                "planos_cancelados": s["plano_cancelado"] + s["plano_invalidado"] + s["plano_expirado"],
                "chamadas_ia": s["chamadas_ia"], "chamadas_ia_invalidas": s["chamadas_ia_invalidas"],
                "custo_ia_usd": round(conta.custo_ia_usd, 6), "rotulos": sorted(conta.rotulos),
                "saidas_duplicadas_evitadas": s["saidas_duplicadas_evitadas"],
                # tentativas x episodios (vetos repetidos contam um episodio), gestao e pares das sombras
                "tentativas_veto": s["tentativas_veto"], "episodios_veto": s["episodios_veto"],
                "episodios_bloqueio": s["episodios_bloqueio"], "episodios": s["episodios"],
                "parciais": s["parciais"], "gestoes_ia": s["gestoes_ia"], "gestoes_auto": s["gestoes_auto"],
                "gestoes_vetadas": s["gestoes_vetadas"], "gestoes_nao_aplicadas": s["gestoes_nao_aplicadas"],
                "pares_incompletos_sombra": s["pares_incompletos_sombra"],
            }
        return out


def _hash_codigo_modelos() -> str:
    try:
        from robowin.modelos.microestrutura import hash_codigo
        return hash_codigo()
    except Exception:
        return ""


@dataclass
class ExecucaoRegistrada:
    decisao_id: str
    resultado: str
    execucao: ResultadoExecucao | None


def geometria_de(risco: ConfigRisco, ins: Instrumento, stop: float, alvo: float, tempo_max_s: int = 60) -> Geometria:
    """Geometria dentro do que o risco aceita (o stop nunca passa do maximo efetivo da politica), na grade do tick."""
    por_perda = risco.perda_max_operacao_brl / (risco.contratos_max * ins.ponto_valor_brl) - risco.slippage_folga_pontos
    bruto = min(stop, risco.stop_max_pontos, por_perda)
    return Geometria(max(math.floor(bruto / ins.tick_pontos + 1e-9), 1) * ins.tick_pontos, alvo, tempo_max_s)

