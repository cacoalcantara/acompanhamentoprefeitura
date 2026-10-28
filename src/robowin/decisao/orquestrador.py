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
"""
from __future__ import annotations

import asyncio
import datetime as dt
import heapq
import math
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
    CarimboLatencia,
    ConfigRisco,
    Instrumento,
    Intencao,
    Lado,
    LivroSnapshot,
    ManifestoExperimento,
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
from robowin.ia.dossie import ErroDossie, hash_dossie, montar_dossie
from robowin.ia.orcamento import OrcamentoIA
from robowin.ia.reflexao import MemoriaReflexiva, liquidar_pendentes
from robowin.risco.kill_switch import EventoKill, KillSwitch
from robowin.risco.motor import EventoAgenda, MotorRiscoPadrao
from robowin.risco.tempo import dia_sp, segundos_do_dia

from .bracos.base import BracoBase, ContextoDecisao, Geometria, ResultadoDecisao
from .bracos.controles import BracoAleatorio
from .dsl_plano import EstadoPlano
from .gatilhos import ConfigGatilhos, EstadoGatilho, GerenciadorGatilhos
from .odds import MOTIVO_VETO, AvaliadorOdds
from .plano_condicional import (
    AssinaturaPosicao,
    EventoPlano,
    GerenciadorPlano,
    niveis_recalculados,
)
from .registro import ORIGEM_SINTETICO, Registro
from .saidas import GestorSaidas
from .validade import BloqueioDados, validar_frescor_entrada

NS = 1_000_000_000
FEATURES_DOSSIE = (
    "fluxo_imb_5s", "fluxo_imb_30s", "fluxo_imb_120s", "fluxo_intensidade_30s", "livro_imb_1", "livro_imb_5",
    "microprice_desvio_pts", "ofi_30s_norm", "ret_5s", "ret_30s", "ret_120s", "rv_1m", "rv_razao_1m_30m",
    "ctx_bucket_30m",
)


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


@dataclass
class MetaPosicao:
    intencao: Intencao
    aberta_ns: int
    tempo_max_s: int
    decisao_id: str = ""
    stop_pontos: float | None = None
    alvo_pontos: float | None = None


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
            cfg.instrumento, relogio, custos or Custos.do_instrumento(cfg.instrumento), latencia_ns=cfg.latencia_exec_ns, seed=cfg.seed)
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
        for i, b in enumerate(bracos):
            self._criar_conta(b, 1001 + i)
        self._ligar_controles()

    # ================================================================================== construcao ==
    def _criar_conta(self, b: BracoBase, magic: int) -> None:
        cfg = self.cfg
        ks = KillSwitch(arquivo_flag=cfg.arquivo_emergencia, auditoria=self.registro.auditoria)
        motor = MotorRiscoPadrao(cfg.risco, cfg.instrumento, kill_switch=ks, auditoria=self.registro.auditoria,
                                 eventos=self.eventos, grade=lambda d: calendario_b3.sessao_em(d, self.perfil))
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
            if b.ia:
                self.gatilhos[g] = GerenciadorGatilhos(cfg.gatilhos, self.orcamento)
            else:
                p = ConfigGatilhos(periodo_s=cfg.passo_s, intervalo_min_s=cfg.passo_s, rearme_evento_s=1e12,
                                   e_brl_min=1e18, z_statarb_min=1e18, evento_posicao=False,
                                   max_chamadas_dia=None)
                self.gatilhos[g] = GerenciadorGatilhos(p, None)

    def _ligar_controles(self) -> None:
        for c in self.contas.values():
            if isinstance(c.braco, BracoAleatorio) and c.braco.frequencia_fn is None:
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
            self._fechar_posicao(conta, e.trade, t)
        elif e.tipo in ("rejeitada", "cancelada"):
            o = conta.oms.por_ticket(e.ticket)
            if o is not None and o.eh_entrada and conta.meta is None:
                conta.entrada_em_voo = None  # a entrada nao virou posicao: libera o braco
            if e.tipo == "rejeitada" and e.motivo == "posicao_inexistente":  # saida redundante: a protecao ja fechou a posicao
                self.registro.evento_risco(t_ns=t, braco=conta.braco.id, tipo="saida_duplicada_evitada",
                                           detalhe=f"ticket={e.ticket}")
                conta.stats["saidas_duplicadas_evitadas"] += 1

    def _abrir_posicao(self, conta: ContaBraco, e: EventoSim, t: int) -> None:
        pos = self.sim.posicao(conta.braco.id)
        if pos.quantidade <= 0:
            return
        it = conta.entrada_em_voo
        g = conta.braco.geometria
        meta = MetaPosicao(
            intencao=it if it is not None else Intencao(
                id="?", braco=conta.braco.id, acao=Acao.COMPRAR if pos.lado == Lado.COMPRA else Acao.VENDER,
                validade_ns=t, stop_pontos=g.stop_pontos, alvo_pontos=g.alvo_pontos),
            aberta_ns=pos.aberta_ns, tempo_max_s=int(it.tempo_max_s if it and it.tempo_max_s else g.tempo_max_s),
            stop_pontos=it.stop_pontos if it else g.stop_pontos, alvo_pontos=it.alvo_pontos if it else g.alvo_pontos)
        conta.meta, conta.entrada_em_voo = meta, None
        conta.eventos_posicao.append("abriu")
        conta.stats["posicoes_abertas"] += 1
        self.registro.posicao(braco=conta.braco.id, t_ns=t, lado=pos.lado.value if pos.lado else None,
                              quantidade=pos.quantidade, preco_medio=pos.preco_medio, stop=pos.stop, alvo=pos.alvo)
        if e.execucao is not None and conta.t_envio_entrada_ns:
            self.registro.latencia(
                CarimboLatencia(etapa="ordem_para_fill", braco=conta.braco.id, t_ini_ns=conta.t_envio_entrada_ns,
                                t_fim_ns=e.execucao.t_ns, injetado=True), t_ns=t)
        self._espelhar(conta, t, "espelhar_entrada", t, pos, meta.intencao, meta.stop_pontos, meta.alvo_pontos,
                       meta.tempo_max_s)

    def _fechar_posicao(self, conta: ContaBraco, tr: Trade, t: int) -> None:
        braco = conta.braco.id
        claim = self.saidas.liberar(braco)
        motivo = tr.motivo_saida
        if claim is not None and motivo in ("saida", "zeragem"):
            motivo = claim.motivo
        self.registro.trade(tr, motivo_saida=motivo)
        conta.stats["trades"] += 1
        sinal = 1.0 if tr.lado == Lado.COMPRA else -1.0
        conta.n_trades_memoria += 1
        entrada_mem = {
            "acao": "COMPRAR" if tr.lado == Lado.COMPRA else "VENDER", "resultado_pontos": sinal * (tr.saida - tr.entrada),
            "duracao_s": max(0, (tr.t_saida_ns - tr.t_entrada_ns) // NS), "motivo_saida": motivo,
            "trade_id": f"{braco}:{conta.n_trades_memoria}"}
        conta.memoria.append(entrada_mem)
        if (self.memoria_reflexiva is not None and conta.braco.ia
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
        for conta in self.contas.values():
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

    def _saidas(self, t: int) -> None:
        for conta in self.contas.values():
            pos = self.sim.posicao(conta.braco.id)
            if pos.quantidade <= 0:
                continue
            precisa, motivo = conta.motor.precisa_zerar(pos, t)
            if precisa:
                self._sair(conta, "kill" if motivo == "kill_switch" else "zeragem", t, forcado=True, detalhe=motivo)
            elif conta.meta is not None and t - pos.aberta_ns >= conta.meta.tempo_max_s * NS:
                self._sair(conta, "tempo", t)

    def _sair(self, conta: ContaBraco, motivo: str, t: int, *, forcado: bool = False, detalhe: str = "") -> bool:
        """Saida deterministica por tempo/zeragem/kill/ENCERRAR. UM dono por posicao: so a primeira reivindicacao vale."""
        braco = conta.braco.id
        pos = self.sim.posicao(braco)
        if pos.quantidade <= 0:
            return False
        iid = f"saida-{motivo}-{braco}-{pos.ticket}"
        if not self.saidas.reivindicar(braco, pos.ticket, motivo, t, iid):
            conta.stats["saidas_recusadas_dono"] += 1
            return False
        env = None
        if forcado:
            env = conta.roteador.zerar_braco(braco, t, motivo)
        else:
            it = Intencao(id=iid, braco=braco, acao=Acao.ENCERRAR, validade_ns=t + 60 * NS,
                          preco_referencia=_meio(self.ultimo_tick) if self.ultimo_tick else 0.0, t0_ns=t)
            r = conta.roteador.executar_intencao(it, pos, t)
            env = r.envio
        enviado = env is not None and env.enviado and env.ordem.estado not in TERMINAIS
        if not enviado:  # nao saiu (ex.: recuo do OMS): libera para tentar de novo no proximo tick
            self.saidas.liberar(braco)
            return False
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

    # ====================================================================================== planos ======
    def _registrar_evento_plano(self, ev: EventoPlano) -> None:
        d = ev.como_dict()
        self.registro.auditar(f"plano_{ev.tipo}", d)
        c = self.contas[ev.braco]
        c.stats[f"plano_{ev.tipo}"] += 1
        if ev.tipo in ("cancelado", "invalidado", "expirado"):
            self.registro.evento_risco(t_ns=ev.t_ns, braco=ev.braco, tipo=f"plano_{ev.tipo}", detalhe=ev.motivo)

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

    def _disparar_plano(self, conta: ContaBraco, ev: EventoPlano, tick: Tick, t: int) -> None:
        base = ev.plano.base
        compra = base.acao == Acao.COMPRAR
        preco_real = tick.ask if compra else tick.bid
        if preco_real <= 0:
            preco_real = _meio(tick)
        it = conta.planos.intencao_disparo(ev, preco_real=preco_real, t_ns=t, novo_id=f"{base.id}:disp")
        sl, tp = niveis_recalculados(compra, preco_real, base.stop_pontos or 0.0, base.alvo_pontos)
        extra = {"plano_idade_ns": ev.idade_ns, "plano_idade_s": round(ev.idade_ns / NS, 3), "preco_real": preco_real,
                 "ref_plano": ev.plano.ref, "stop_recalculado": sl, "alvo_recalculado": tp,
                 "decisao_origem": ev.plano.decisao_id}
        self._executar_entrada(conta, it, t, hash_dossie=ev.plano.hash_dossie, gatilho="plano_disparado",
                               proposta={"origem": "plano_condicional", **extra})

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
        return did

    def _processar_resultado(self, res: ResultadoDecisao, agora: int) -> None:
        conta = self.contas[res.braco]
        self._registrar_ia(res)
        gat = ",".join(res.gatilhos)
        braco = conta.braco
        if res.resultado != "bloqueada":
            conta.ultimo_bloqueio = None
        if res.resultado == "nao_operar":
            conta.stats["nao_operar"] += 1
            if braco.ia:
                self._gravar_decisao(res, "nao_operar", motivos=res.motivos, gatilho=gat)
            return
        if res.resultado == "bloqueada":
            conta.stats["bloqueadas"] += 1
            if not braco.ia and conta.ultimo_bloqueio == res.motivos:
                return  # mesma recusa (ex.: modelo nao treinado) em ciclos seguidos: grava so a primeira
            conta.ultimo_bloqueio = res.motivos
            for m in res.motivos:
                conta.stats[f"bloq:{_chave_bloqueio(m)}"] += 1
            self._gravar_decisao(res, "bloqueada", motivos=res.motivos, gatilho=gat)
            self.registro.evento_risco(t_ns=res.t0_ns, braco=res.braco, tipo="entrada_bloqueada",
                                       detalhe=";".join(res.motivos)[:300])
            return
        if res.rotulos:
            conta.rotulos.update(res.rotulos)
        it = res.intencao
        assert it is not None
        if res.eh_entrada and res.proposta:  # base da reflexao pos-trade (so texto ja validado da proposta)
            conta.resumo_ultima_entrada = {
                "hipoteses": list(res.proposta.get("hipoteses") or [])[:3],
                "justificativa_curta": str(res.proposta.get("justificativa_curta") or ""),
                "incerteza": res.proposta.get("incerteza")}
        pos = self.sim.posicao(res.braco)
        tick = self.ultimo_tick
        meio = _meio(tick) if tick else res.preco_ref
        # ---- acoes de posicao: nunca esperam revisao, nunca dependem do frescor da proposta ---------------------
        if res.resultado in ("manter", "saida"):
            if pos.quantidade <= 0:
                self._gravar_decisao(res, "ignorada", motivos=("sem_posicao_aberta",), gatilho=gat)
                return
            if res.resultado == "saida":
                ok = self._sair(conta, "encerrar", agora)
                self._gravar_decisao(res, "executada" if ok else "ignorada",
                                     motivos=() if ok else ("saida_ja_reivindicada",), gatilho=gat)
            else:
                it2 = it.model_copy(update={"preco_referencia": meio})
                r = conta.roteador.executar_intencao(it2, pos, agora)
                self._gravar_decisao(res, "executada" if r.decisao.aprovada else "vetada",
                                     motivos=r.decisao.motivos, risco=r.decisao, gatilho=gat, intencao=it2)
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

    def _armar_plano(self, conta: ContaBraco, res: ResultadoDecisao, agora: int, gat: str) -> None:
        it = res.intencao
        assert it is not None and res.plano is not None
        if conta.motor.evento_proximo(agora) is not None:
            self._gravar_decisao(res, "bloqueada", motivos=("janela_de_evento",), gatilho=gat)
            return
        if agora > res.plano.expira_ns:
            self._gravar_decisao(res, "bloqueada", motivos=("plano_ja_expirado",), gatilho=gat)
            return
        did = self._gravar_decisao(res, "plano_armado", gatilho=gat,
                                   proposta_extra={"expira_em_s": round((res.plano.expira_ns - agora) / NS, 1)})
        base = it.model_copy(update={"plano": None})
        for ev in conta.planos.armar(base, res.plano, ref=res.preco_ref, t0_ns=res.t0_ns, t_ns=agora,
                                     assinatura=AssinaturaPosicao.de(self.sim.posicao(res.braco)), decisao_id=did,
                                     hash_dossie=res.hash_dossie):
            self._registrar_evento_plano(ev)

    def _executar_entrada(self, conta: ContaBraco, it: Intencao, t: int, *, hash_dossie: str, gatilho: str,
                          res: ResultadoDecisao | None = None,
                          proposta: dict[str, Any] | None = None) -> ExecucaoRegistrada:
        braco = conta.braco.id
        pos = self.sim.posicao(braco)
        tick = self.ultimo_tick
        spread = max((tick.ask - tick.bid), 0.0) if tick else 0.0
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
            conta.stats["entradas"] += 1
            for c in self.contas.values():  # controles copiam a geometria dos bracos de referencia
                if isinstance(c.braco, BracoAleatorio) and braco in self.cfg.referencias_frequencia:
                    c.braco.observar(it)
        else:
            conta.stats["vetadas"] += 1
            self.registro.evento_risco(t_ns=t, braco=braco, tipo="entrada_vetada", detalhe=";".join(motivos)[:300])
        if res is not None:
            did = self._gravar_decisao(res, resultado, motivos=motivos, intencao=it, risco=r.decisao, gatilho=gatilho)
        else:
            fake = ResultadoDecisao(braco=braco, modo="condicional", t0_ns=t, ciclo_id=it.id, acao=it.acao,
                                    resultado="entrada", intencao=it, proposta=proposta, hash_dossie=hash_dossie)
            did = self._gravar_decisao(fake, resultado, motivos=motivos, intencao=it, risco=r.decisao,
                                       gatilho=gatilho)
        return ExecucaoRegistrada(did, resultado, r)

    # ====================================================================================== ciclos ======
    def _eventos_pendentes(self) -> bool:
        return any(c.eventos_posicao for c in self.contas.values())

    def _grupo_ativo(self, contas: list[ContaBraco], t: int) -> bool:
        for c in contas:
            if c.kill.ativo:
                continue
            if self.sim.posicao(c.braco.id).quantidade > 0:
                return True
            if (not self.bloqueio.bloqueado(t) and c.motor.dentro_da_janela(t)
                    and c.motor.evento_proximo(t) is None):
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
            if not self._grupo_ativo(contas, t):
                continue
            gat = self.gatilhos[grupo]
            ate_s = contas[0].motor.limites_janela_s(dia)[1] - segundos_do_dia(t)
            est = EstadoGatilho(
                t_ns=t, modelo_saida=snap.saida_principal, z_statarb=snap.z, noticias_novas=snap.noticias_novas,
                eventos_posicao=tuple(e for c in contas for e in c.eventos_posicao),
                segundos_restantes=float(max(ate_s, 0)))
            disparos = gat.avaliar(est)
            if not disparos:
                continue
            gat.marcar_chamada(t)
            for c in contas:
                c.eventos_posicao.clear()
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
        if self.statarb is not None:
            precos = {self.cfg.instrumento.simbolo: _meio(tick)} | (self.precos_aux(tick) if self.precos_aux else {})
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
                              novas, (time.perf_counter() - t_ini) * 1000)
        return self._snap

    def _regime(self, snap: Snapshot, conta: ContaBraco, t: int) -> str:
        if conta.motor.evento_proximo(t) is not None:
            return "evento_proximo"
        r = snap.vetor.get("rv_razao_1m_30m", float("nan"))
        if r != r:
            return "indefinido"
        return "agitado" if r > 2.0 else ("calmo" if r < 0.5 else "normal")

    def _dossie(self, conta: ContaBraco, snap: Snapshot, t: int) -> dict[str, Any]:
        braco = conta.braco.id
        pos = self.sim.posicao(braco)
        posicao: dict[str, Any] | None = None
        if pos.quantidade > 0 and pos.lado is not None:
            posicao = {"lado": pos.lado.value, "quantidade": pos.quantidade, "preco_medio": pos.preco_medio,
                       "aberta_ns": pos.aberta_ns,
                       "stop_pontos": conta.meta.stop_pontos if conta.meta else None,
                       "alvo_pontos": conta.meta.alvo_pontos if conta.meta else None}
        est = conta.motor.contab.estado(braco, t)
        cfg = self.cfg.risco
        limites = {"perda_restante_dia_brl": cfg.perda_max_dia_brl + est.pnl_dia_total,
                   "operacoes_restantes": float(cfg.operacoes_dia_max - est.operacoes_dia),
                   "contratos_max": float(cfg.contratos_max), "stop_max_pontos": cfg.stop_max_pontos}
        dia = dia_sp(t)
        de, _, zer = conta.motor.limites_janela_s(dia)
        seg = segundos_do_dia(t)
        dono = self._dono_memoria(conta)
        feats = {k: v for k, v in snap.vetor.items() if k in FEATURES_DOSSIE}
        return montar_dossie(
            t0_ns=t, ticks=list(self.hist), spread_pontos=max(snap.ask - snap.bid, 0.0), features=feats,
            features_permitidas=nomes_permitidos(), modelos=snap.modelos, statarb=snap.statarb,
            noticias=snap.noticias, regime=self._regime(snap, conta, t), posicao=posicao, limites=limites,
            memoria=self._memoria_do_dossie(dono, t), minutos_desde_abertura=max(0, (seg - 9 * 3600) // 60),
            minutos_para_zeragem=max(0, (zer - seg) // 60), tick_pontos=self.cfg.instrumento.tick_pontos)

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
        compartilhada). Bracos sem IA usam a propria."""
        prop = getattr(conta.braco, "proponente", None)
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
            if c.kill.ativo and self.sim.posicao(c.braco.id).quantidade <= 0:
                continue
            try:
                if c.braco.ia:
                    # dossie do LIDER do grupo, compartilhado: B e C usam a MESMA proposta (chamada pareada);
                    # a posicao propria de cada braco e conferida na entrega (ver _processar_resultado)
                    if d_lider is None:
                        d_lider = self._dossie(c, snap, t)
                    d = d_lider
                else:
                    d = self._dossie_leve(snap)
            except ErroDossie as e:
                self.registro.incidente(t_ns=t, origem=f"dossie:{c.braco.id}", severidade="erro", detalhe=str(e)[:300])
                continue
            ctxs.append((c, ContextoDecisao(
                t0_ns=t, dossie=d, hash_dossie=hash_dossie(d), preco_ref=snap.meio, bid=tick.bid, ask=tick.ask,
                posicao=self.sim.posicao(c.braco.id), ciclo_id=ciclo_id, gatilhos=disparos,
                tick_pontos=self.cfg.instrumento.tick_pontos,
                odds=self._odds if self.avaliador_odds is not None else None)))
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

