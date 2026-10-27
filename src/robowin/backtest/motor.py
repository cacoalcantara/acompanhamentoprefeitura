"""Motor de backtest: replay cronologico por disponibilidade -> features -> bracos -> risco -> OMS -> simulador.

Garantias (ver tambem `vazamento.py`):
* o relogio simulado so avanca para o instante de DISPONIBILIDADE do proximo evento (replay.py);
* o dossie de t usa apenas eventos ja entregues (t_disponivel <= t), e e anonimizado (sem datas/niveis);
* a decisao do braco, o veto de risco, as ordens e as execucoes passam pelos mesmos componentes da
  simulacao em tempo real (Roteador/OMS/MotorRiscoPadrao/SimuladorExecucao);
* nada usa relogio de parede nem sorteio nao semeado: duas execucoes com a mesma seed geram o mesmo
  `hash_resultado()`.

Divergencias deliberadas em relacao ao ao-vivo: (1) o kill switch e rearmado a cada novo pregao (no real o
rearme e manual, com motivo e responsavel); (2) o tempo-limite da intencao e imposto pelo motor de backtest
(no real, pelo orquestrador); (3) posicao ainda aberta no fim dos dados e apenas marcada a mercado e
reportada (`posicoes_abertas_fim` + evento de risco critico), nunca liquidada com preco inventado.
"""
from __future__ import annotations

import bisect
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from robowin.core import calendario_b3
from robowin.core.ids import hash_json
from robowin.core.protocolos import Banco, Braco
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    ConfigRisco,
    Instrumento,
    Intencao,
    Lado,
    Trade,
)
from robowin.execucao.custos import Custos
from robowin.execucao.oms import OrdemOMS
from robowin.execucao.preenchimento import ParamsSlippage, cotacao_valida
from robowin.execucao.simulador import EventoSim
from robowin.features.pipeline import PipelineFeatures
from robowin.features.registro import nomes_permitidos
from robowin.ia.dossie import ErroDossie, hash_dossie, montar_dossie
from robowin.risco.kill_switch import EventoKill
from robowin.risco.motor import EstadoMercado, EventoAgenda
from robowin.risco.tempo import dia_sp, hhmm_para_s, segundos_do_dia

from .anonimizacao import anonimizacao_rapida
from .executor import ExecucaoBacktest
from .latencia_injetada import DistribuicaoLatencia, carimbo_injetado, linha_latencia
from .replay import EventoReplay, FonteArmazem, FonteEventos, tick_no_instante

NS = 1_000_000_000


# ============================================================================================ configuracao
@dataclass(frozen=True)
class ConfigBacktest:
    instrumento: Instrumento = field(default_factory=lambda: Instrumento(simbolo="WIN"))
    cfg_risco: ConfigRisco = field(default_factory=ConfigRisco)
    seed: int = 0
    intervalo_decisao_s: float = 15.0
    aquecimento_s: float = 600.0          # sem decisoes ate o pipeline de features estar aquecido (por pregao)
    equity_a_cada_s: float = 60.0
    gap_feed_s: float = 60.0              # silencio maior que isto = lacuna de feed
    recuperacao_gap_s: float = 30.0       # apos a lacuna, entradas vetadas por `feed_parado` durante este tempo
    janela_dossie_s: float = 1000.0
    memoria_trades: int = 5
    custos: Custos = field(default_factory=Custos)
    slippage: ParamsSlippage = field(default_factory=ParamsSlippage)
    latencia: DistribuicaoLatencia = field(default_factory=lambda: DistribuicaoLatencia.fixa(150.0))
    cenario: str = "base"
    fator_fila: float = 1.0
    eventos_agenda: tuple[EventoAgenda, ...] = ()
    rearmar_kill_por_dia: bool = True
    id_experimento: str = "backtest"
    extras_dossie: Callable[[int, dict[str, float]], dict[str, Any]] | None = None

    def estresse(self) -> ConfigBacktest:
        """Cenario de estresse: corretagem de estresse, slippage de stop agravado e fila mais longa."""
        return replace(self, cenario="estresse", custos=Custos.estresse(), slippage=ParamsSlippage.estresse(),
                       fator_fila=max(self.fator_fila, 2.0))


# ============================================================================================ resultado
@dataclass
class ResultadoBacktest:
    cenario: str
    seed: int
    sintetico: bool
    n_eventos: int
    bracos: list[str]
    decisoes: list[dict[str, Any]]
    ordens: list[dict[str, Any]]
    execucoes: list[dict[str, Any]]
    trades: dict[str, list[Trade]]
    equity: list[dict[str, Any]]
    eventos_risco: list[dict[str, Any]]
    latencias: list[dict[str, Any]]
    posicoes: list[dict[str, Any]]
    posicoes_abertas_fim: dict[str, int]
    motivos_saida: dict[str, dict[str, int]] = field(default_factory=dict)

    def trades_por_braco(self) -> dict[str, list[Trade]]:
        return {b: list(self.trades.get(b, [])) for b in self.bracos}

    def liquido_brl(self, braco: str) -> float:
        return sum(t.liquido_brl for t in self.trades.get(braco, []))

    def conteudo_normalizado(self) -> dict[str, Any]:
        """Conteudo que entra no hash: sem ids gerados por contador global nem relogio de parede."""
        return {
            "cenario": self.cenario, "seed": self.seed, "n_eventos": self.n_eventos, "bracos": self.bracos,
            "decisoes": [[d["t_ns"], d["braco"], d["gatilho"], d["acao"], d["resultado"], d["motivos"],
                          d["hash_dossie"], d["intencao_json"], d["risco_json"]] for d in self.decisoes],
            "ordens": sorted([o["t_ns"], o["braco"], o["lado"], o["tipo"], o["quantidade"], o["preco"],
                              o["estado"], o["ticket"], o["retcode"]] for o in self.ordens),
            "execucoes": [[e["braco"], e["t_ns"], e["preco"], e["quantidade"], e["taxas_brl"]]
                          for e in self.execucoes],
            "trades": {b: [[t.lado.value, t.entrada, t.saida, t.t_entrada_ns, t.t_saida_ns, t.bruto_brl,
                            t.tarifas_brl, t.corretagem_brl, t.slippage_brl, t.motivo_saida] for t in ts]
                       for b, ts in sorted(self.trades.items())},
            "equity": [[e["braco"], e["t_ns"], e["equity_brl"], e["drawdown_brl"]] for e in self.equity],
            "eventos_risco": [[e["t_ns"], e["braco"], e["tipo"], e["detalhe"], e["critico"]]
                              for e in self.eventos_risco],
            "latencias": [[x["t_ns"], x["braco"], x["etapa"], x["dur_ms"], x["injetado"]] for x in self.latencias],
            "posicoes_abertas_fim": self.posicoes_abertas_fim,
        }

    def hash_resultado(self) -> str:
        return hash_json(self.conteudo_normalizado())

    def resumo(self) -> dict[str, Any]:
        return {
            "cenario": self.cenario, "seed": self.seed, "sintetico": self.sintetico, "n_eventos": self.n_eventos,
            "hash_resultado": self.hash_resultado(), "posicoes_abertas_fim": self.posicoes_abertas_fim,
            "bracos": {b: {"decisoes": sum(1 for d in self.decisoes if d["braco"] == b),
                           "trades": len(self.trades.get(b, [])), "liquido_brl": round(self.liquido_brl(b), 4),
                           "motivos_saida": self.motivos_saida.get(b, {})} for b in self.bracos},
        }


# ============================================================================================ gravacao
class _Gravador:
    """Acumula linhas por tabela e grava em lotes (uma transacao por lote) no Banco."""

    def __init__(self, banco: Banco | None, lote: int = 2000) -> None:
        self.banco, self.lote = banco, lote
        self._buf: list[tuple[str, dict[str, Any]]] = []

    def add(self, tabela: str, linha: dict[str, Any]) -> None:
        if self.banco is None:
            return
        self._buf.append((tabela, linha))
        if len(self._buf) >= self.lote:
            self.flush()

    def flush(self) -> None:
        if self.banco is None or not self._buf:
            return
        buf, self._buf = self._buf, []
        transacao = getattr(self.banco, "transacao", None)
        if transacao is not None:
            with transacao():
                for tabela, linha in buf:
                    self.banco.inserir(tabela, linha)
        else:
            for tabela, linha in buf:
                self.banco.inserir(tabela, linha)


# ============================================================================================ motor
class MotorBacktest:
    """Executa UM cenario sobre uma fonte de eventos com N bracos. Uso unico (`executar()` so uma vez)."""

    def __init__(self, fonte: FonteEventos, bracos: Sequence[Braco], cfg: ConfigBacktest | None = None,
                 banco: Banco | None = None) -> None:
        if not bracos:
            raise ValueError("backtest sem bracos")
        ids = [b.id for b in bracos]
        if len(set(ids)) != len(ids):
            raise ValueError("ids de bracos duplicados")
        self.fonte, self.bracos, self.cfg, self.banco = fonte, list(bracos), cfg or ConfigBacktest(), banco
        self._usado = False

    @classmethod
    def de_armazem(cls, armazem: Any, simbolo: str, bracos: Sequence[Braco], cfg: ConfigBacktest | None = None,
                   banco: Banco | None = None, de_ns: int | None = None, ate_ns: int | None = None) -> MotorBacktest:
        return cls(FonteArmazem(armazem, simbolo, de_ns, ate_ns), bracos, cfg, banco)

    # ------------------------------------------------------------------------------------ execucao
    def executar(self) -> ResultadoBacktest:
        if self._usado:
            raise RuntimeError("MotorBacktest e de uso unico; crie outro para repetir")
        self._usado = True
        eventos = self.fonte.eventos()
        self._iniciar(eventos)
        for ev in eventos:
            self._processar(ev)
        return self._finalizar()

    # ------------------------------------------------------------------------------------ preparo
    def _iniciar(self, eventos: Sequence[EventoReplay]) -> None:
        cfg = self.cfg
        self.rel = RelogioSimulado(eventos[0].t_disp_ns if eventos else 0)
        self.ids = [b.id for b in self.bracos]
        self.grav = _Gravador(self.banco)
        self.lat = cfg.latencia.amostrador(cfg.seed + 7)
        self.exec = ExecucaoBacktest(
            cfg.instrumento, self.rel, self.ids, cfg.cfg_risco, cfg.custos, cfg.slippage, self.lat, cfg.seed,
            banco=self.banco, eventos_agenda=cfg.eventos_agenda, fator_fila=cfg.fator_fila,
            ao_ordem=self._ao_ordem, ao_kill=self._ao_kill)
        self.r_decisoes: list[dict[str, Any]] = []
        self.r_execucoes: list[dict[str, Any]] = []
        self.r_equity: list[dict[str, Any]] = []
        self.r_risco: list[dict[str, Any]] = []
        self.r_lat: list[dict[str, Any]] = []
        self.r_pos: list[dict[str, Any]] = []
        self.r_trades: dict[str, list[Trade]] = {b: [] for b in self.ids}
        self.r_ordens: dict[str, dict[str, Any]] = {}
        self.motivos_saida: dict[str, Counter[str]] = {b: Counter() for b in self.ids}
        self.n_eventos = len(eventos)
        self._dia = None
        self._pipe: PipelineFeatures | None = None
        self._t_dia0 = 0
        self._ult_disp: int | None = None
        self._gap_ate = 0
        self._feed_ok = True
        self._prox_decisao = 0
        self._prox_equity = 0
        self._ring: list[tuple[int, float]] = []  # (t_evento, preco) ordenado por t_evento
        self._memoria: dict[str, list[dict[str, Any]]] = {b: [] for b in self.ids}
        self._limite_tempo: dict[str, int | None] = {b: None for b in self.ids}
        self._causa_saida: dict[str, str] = {}
        self._n_dec = 0
        self._n_exe = 0
        self._n_trd = {b: 0 for b in self.ids}
        self._n_auto = 0
        self._t_ult = self.rel.agora_ns()
        self.grav.add("experimentos", {
            "id": cfg.id_experimento, "criado_ns": self.rel.agora_ns(), "status": "backtest_" + cfg.cenario,
            "manifesto_json": {
                "tipo": "backtest", "cenario": cfg.cenario, "seed": cfg.seed, "bracos": self.ids,
                "origem_dados": "sinteticos" if self.fonte.sintetico else "armazenados",
                "latencia": {"origem": cfg.latencia.origem, "injetada": True, **cfg.latencia.quantis()},
                "custos": {"corretagem_ida_volta_brl": cfg.custos.corretagem_ida_volta_brl},
                "slippage": {"minimo_ticks": cfg.slippage.minimo_ticks, "alpha": cfg.slippage.alpha},
                "intervalo_decisao_s": cfg.intervalo_decisao_s}})

    # ------------------------------------------------------------------------------------ callbacks
    def _ao_ordem(self, o: OrdemOMS) -> None:
        linha = {"id": o.id, "t_ns": o.t_criacao_ns, "braco": o.braco, "intencao_id": o.intencao_id,
                 "lado": o.cmd.lado.value, "tipo": o.cmd.tipo, "quantidade": o.cmd.quantidade,
                 "preco": o.cmd.preco, "estado": o.estado.value, "ticket": o.ticket, "retcode": o.retcode,
                 "simulada": 1}
        self.r_ordens[o.id] = linha
        self.grav.add("ordens", linha)  # cada transicao regrava a linha (INSERT OR REPLACE): o estado final prevalece

    def _ao_kill(self, ev: EventoKill) -> None:
        self._risco(ev.t_ns, "", "kill_switch", {"motivo": ev.motivo, "origem": ev.origem}, critico=True)

    def _risco(self, t_ns: int, braco: str, tipo: str, detalhe: Any, critico: bool = False) -> None:
        linha = {"t_ns": t_ns, "braco": braco, "tipo": tipo,
                 "detalhe": json.dumps(detalhe, sort_keys=True, default=str, ensure_ascii=False),
                 "critico": int(critico)}
        self.r_risco.append(linha)
        self.grav.add("eventos_risco", linha)

    def _incidente(self, t_ns: int, origem: str, severidade: str, detalhe: str) -> None:
        self.grav.add("incidentes", {"t_ns": t_ns, "origem": origem, "severidade": severidade, "detalhe": detalhe})

    # ------------------------------------------------------------------------------------ loop
    def _processar(self, ev: EventoReplay) -> None:
        cfg = self.cfg
        t = ev.t_disp_ns
        self.rel.avancar_para(t)
        self._t_ult = t
        d = dia_sp(t)
        if d != self._dia:
            self._novo_dia(d, t)
        if self._ult_disp is not None and t - self._ult_disp > cfg.gap_feed_s * NS:
            self._gap_ate = t + int(cfg.recuperacao_gap_s * NS)
            self._risco(t, "", "feed_gap", {"silencio_s": round((t - self._ult_disp) / NS, 3)})
        self._ult_disp = t
        feed_ok = t >= self._gap_ate
        if feed_ok != self._feed_ok:
            self._feed_ok = feed_ok
            self.exec.motor.atualizar_mercado(EstadoMercado(feed_ok=feed_ok))
        assert self._pipe is not None
        if ev.tipo == "tick":
            tk = tick_no_instante(ev.tick, t)  # type: ignore[arg-type]
            self._pipe.on_tick(tk)
            self._inserir_ring(ev.t_evento_ns, tk.meio)
            sims = self.exec.on_tick(tk)
            self._registrar_sim(sims, t)
            self._drenar_latencia("execucao_stop", "", t)
            self._protecao(t)
        else:
            lv = ev.livro
            assert lv is not None
            self._pipe.on_livro(lv)
            self.exec.on_livro(lv)
        if t >= self._prox_equity:
            self._gravar_equity(t)
            self._prox_equity = t + int(cfg.equity_a_cada_s * NS)
        if t >= self._prox_decisao and self.exec.ultimo_tick is not None and t - self._t_dia0 >= cfg.aquecimento_s * NS:
            self._rodada_decisao(t)
            self._prox_decisao = t + int(cfg.intervalo_decisao_s * NS)

    def _novo_dia(self, d: Any, t: int) -> None:
        self._dia = d
        self._pipe = PipelineFeatures(usar_recebido=True)
        self._t_dia0 = t
        self._ult_disp = None
        self._gap_ate = 0
        self._prox_decisao = t
        self._ring = [x for x in self._ring if x[0] >= t - int(self.cfg.janela_dossie_s * NS)]
        k = self.exec.kill
        if k.ativo and self.cfg.rearmar_kill_por_dia:
            k.rearmar("novo pregao no backtest", "backtest", t)
            self._risco(t, "", "kill_rearmado_backtest", {"dia": d.isoformat()})

    def _inserir_ring(self, t_evento: int, preco: float) -> None:
        r = self._ring
        if not r or t_evento >= r[-1][0]:
            r.append((t_evento, preco))
        else:
            bisect.insort(r, (t_evento, preco))  # backfill: entra na ordem do tempo do evento
        lim = t_evento - int(self.cfg.janela_dossie_s * NS)
        if r[0][0] < lim and len(r) > 64:
            i = bisect.bisect_left(r, (lim, -1e18))
            del r[:i]

    # ------------------------------------------------------------------------------------ eventos do simulador
    def _registrar_sim(self, evs: list[EventoSim], t: int) -> None:
        mudou: set[str] = set()
        for e in evs:
            if e.tipo == "execucao" and e.execucao is not None:
                self._n_exe += 1
                o = self.exec.oms.por_ticket(e.ticket)
                ex = e.execucao
                linha = {"id": f"exe-{self._n_exe:07d}", "ordem_id": o.id if o else str(e.ticket), "t_ns": ex.t_ns,
                         "preco": ex.preco, "quantidade": ex.quantidade, "taxas_brl": ex.taxas_brl, "simulada": 1}
                self.r_execucoes.append({**linha, "braco": e.braco})
                self.grav.add("execucoes", linha)
                mudou.add(e.braco)
            elif e.tipo == "trade" and e.trade is not None:
                self._registrar_trade(e)
                mudou.add(e.braco)
            elif e.tipo == "rejeitada":
                self._risco(t, e.braco, "ordem_rejeitada_simulador", {"ticket": e.ticket, "motivo": e.motivo})
        for b in sorted(mudou):
            p = self.exec.posicao(b)
            linha = {"braco": b, "t_ns": t, "lado": p.lado.value if p.lado else None, "quantidade": p.quantidade,
                     "preco_medio": p.preco_medio, "stop": p.stop, "alvo": p.alvo}
            self.r_pos.append(linha)
            self.grav.add("posicoes", linha)

    def _registrar_trade(self, e: EventoSim) -> None:
        tr = e.trade
        assert tr is not None
        b = e.braco
        self._n_trd[b] += 1
        motivo = tr.motivo_saida
        if motivo not in ("stop", "alvo"):
            motivo = self._causa_saida.pop(b, motivo)
        tr = tr.model_copy(update={"id": f"trd-{b}-{self._n_trd[b]:05d}", "motivo_saida": motivo})
        self.r_trades[b].append(tr)
        self.motivos_saida[b][motivo] += 1
        self._limite_tempo[b] = None
        sinal = 1.0 if tr.lado == Lado.COMPRA else -1.0
        self._memoria[b].append({
            "acao": "COMPRAR" if tr.lado == Lado.COMPRA else "VENDER",
            "resultado_pontos": sinal * (tr.saida - tr.entrada), "duracao_s": (tr.t_saida_ns - tr.t_entrada_ns) // NS,
            "motivo_saida": motivo})
        self._memoria[b] = self._memoria[b][-self.cfg.memoria_trades:]
        self.grav.add("trades", {
            "id": tr.id, "braco": b, "lado": tr.lado.value, "entrada": tr.entrada, "saida": tr.saida,
            "t_entrada_ns": tr.t_entrada_ns, "t_saida_ns": tr.t_saida_ns, "bruto_brl": tr.bruto_brl,
            "tarifas_brl": tr.tarifas_brl, "corretagem_brl": tr.corretagem_brl, "slippage_brl": tr.slippage_brl,
            "liquido_brl": tr.liquido_brl, "motivo_saida": motivo, "dia": dia_sp(tr.t_saida_ns).isoformat()})

    def _drenar_latencia(self, etapa: str, braco: str, t: int) -> None:
        for ms in self.lat.drenar():
            linha = linha_latencia(carimbo_injetado(etapa, braco, t, ms))
            self.r_lat.append(linha)
            self.grav.add("latencias", linha)

    # ------------------------------------------------------------------------------------ protecao deterministica
    def _protecao(self, t: int) -> None:
        for b in self.ids:
            if not self.exec.tem_posicao(b):
                continue
            self.exec.marcar(b, t)
            r = self.exec.zerar_se_preciso(b, t)
            if r is not None:
                motivo, env = r
                self._causa_saida[b] = motivo
                self._risco(t, b, "zeragem_forcada", {"motivo": motivo, "enviada": env.enviado, "estado": env.ordem.estado.value})
                self._drenar_latencia("execucao_ordem", b, t)
                continue
            lim = self._limite_tempo.get(b)
            if lim is not None and self.exec.pode_tentar_saida(b, t):
                pos = self.exec.posicao(b)
                if t - pos.aberta_ns >= lim * NS:
                    self._saida_automatica(b, t, "tempo_limite")

    def _saida_automatica(self, b: str, t: int, motivo: str) -> None:
        tk = self.exec.ultimo_tick
        assert tk is not None
        bid, ask = cotacao_valida(tk)
        self._n_auto += 1
        it = Intencao(id=f"auto-{motivo}-{b}-{self._n_auto:06d}", braco=b, acao=Acao.ENCERRAR, validade_ns=t + 60 * NS,
                      preco_referencia=(bid + ask) / 2, modo="quant", t0_ns=t)
        self.exec.marcar_tentativa_saida(b, t)
        self._causa_saida[b] = motivo
        res = self.exec.executar(it, t, {})
        self._drenar_latencia("execucao_ordem", b, t)
        self._registrar_decisao(b, t, motivo, it, "", res.decisao.model_dump_json(),
                                "saida_enviada" if res.envio is not None and res.envio.enviado else "saida_nao_enviada",
                                list(res.decisao.motivos))

    # ------------------------------------------------------------------------------------ equity
    def _gravar_equity(self, t: int) -> None:
        for b in self.ids:
            if self.exec.tem_posicao(b):
                self.exec.marcar(b, t)
            est = self.exec.motor.contab.estado(b, t)
            linha = {"braco": b, "t_ns": t, "equity_brl": est.equity, "drawdown_brl": est.drawdown}
            self.r_equity.append(linha)
            self.grav.add("equity", linha)

    # ------------------------------------------------------------------------------------ decisao
    def _dossie(self, braco: str, t: int, vetor: dict[str, float], bid: float, ask: float) -> dict[str, Any]:
        cfg, ins = self.cfg, self.cfg.instrumento
        pos = self.exec.posicao(braco)
        est = self.exec.motor.contab.estado(braco, t)
        d = dia_sp(t)
        g = calendario_b3.sessao_em(d)
        s = segundos_do_dia(t)
        posicao = None
        if pos.quantidade > 0 and pos.lado is not None:
            posicao = {"lado": pos.lado.value, "quantidade": pos.quantidade, "preco_medio": pos.preco_medio,
                       "aberta_ns": pos.aberta_ns,
                       "stop_pontos": abs(pos.preco_medio - pos.stop) if pos.stop is not None else None,
                       "alvo_pontos": abs(pos.alvo - pos.preco_medio) if pos.alvo is not None else None}
        extras = cfg.extras_dossie(t, vetor) if cfg.extras_dossie is not None else {}
        with anonimizacao_rapida():
            return montar_dossie(
                t0_ns=t, ticks=list(self._ring), spread_pontos=ask - bid, features=vetor,
                features_permitidas=nomes_permitidos(), posicao=posicao,
                limites={"perda_restante_dia_brl": cfg.cfg_risco.perda_max_dia_brl + est.pnl_dia_total,
                         "operacoes_restantes": float(cfg.cfg_risco.operacoes_dia_max - est.operacoes_dia),
                         "perdas_seguidas": float(est.perdas_seguidas), "drawdown_brl": est.drawdown},
                memoria=self._memoria[braco], minutos_desde_abertura=int((s - hhmm_para_s(g.abertura)) // 60),
                minutos_para_zeragem=int((hhmm_para_s(g.zeragem) - s) // 60), tick_pontos=ins.tick_pontos,
                regime="normal" if self._feed_ok else "feed_instavel", **extras)

    def _rodada_decisao(self, t: int) -> None:
        assert self._pipe is not None
        tk = self.exec.ultimo_tick
        assert tk is not None
        bid, ask = cotacao_valida(tk)
        if bid <= 0 or ask <= 0 or not self._ring:
            return
        vetor = {k: v for k, v in self._pipe.vetor(t).valores.items()}
        for br in self.bracos:
            try:
                dossie = self._dossie(br.id, t, vetor, bid, ask)
            except ErroDossie as e:  # falha segura: sem dossie valido, sem decisao
                self._incidente(t, "backtest.dossie", "aviso", str(e)[:300])
                continue
            h = hash_dossie(dossie)
            try:
                it = br.decidir(t, dossie)
            except Exception as e:
                self._incidente(t, f"braco:{br.id}", "erro", f"{type(e).__name__}: {e}"[:300])
                self.exec.kill.registrar_erro(br.id, t)
                continue
            if it is None:
                continue
            if it.braco != br.id:
                self._incidente(t, f"braco:{br.id}", "erro", f"intencao de outro braco: {it.braco}")
                continue
            self._decidir(br.id, t, it, h, bid, ask)

    def _decidir(self, b: str, t: int, it: Intencao, h: str, bid: float, ask: float) -> None:
        ref = ask if it.acao == Acao.COMPRAR else bid if it.acao == Acao.VENDER else (bid + ask) / 2
        it = it.model_copy(update={"preco_referencia": ref, "t0_ns": t})  # preco real de execucao, nao o da IA
        if it.acao == Acao.NAO_OPERAR:
            self._registrar_decisao(b, t, "periodico", it, h, "", "sem_acao", [])
            return
        estado = {"spread_ticks": (ask - bid) / self.cfg.instrumento.tick_pontos}
        res = self.exec.executar(it, t, estado)
        self._drenar_latencia("execucao_ordem", b, t)
        dec = res.decisao
        motivos = list(dec.motivos)
        if not dec.aprovada:
            resultado = "vetada"
            self._risco(t, b, "veto", {"intencao": it.id, "motivos": motivos})
        elif res.envio is None:
            resultado = "nada_a_enviar"
        elif not res.envio.enviado:
            resultado = "bloqueada_oms"
            motivos.append(res.envio.motivo)
        else:
            resultado = "ordem_" + res.envio.ordem.estado.value.lower()
            if it.acao in (Acao.COMPRAR, Acao.VENDER) and res.envio.ordem.estado.value not in (
                    "REJEITADA", "CANCELADA", "EXPIRADA"):
                self._limite_tempo[b] = it.tempo_max_s
            if it.acao == Acao.ENCERRAR:
                self._causa_saida[b] = "encerrar_braco"
        self._registrar_decisao(b, t, "periodico", it, h, dec.model_dump_json(), resultado, motivos)

    def _registrar_decisao(self, b: str, t: int, gatilho: str, it: Intencao, h: str, risco_json: str,
                           resultado: str, motivos: list[str]) -> None:
        self._n_dec += 1
        linha = {"id": f"dec-{self._n_dec:07d}", "t_ns": t, "braco": b, "modo": it.modo, "gatilho": gatilho,
                 "acao": it.acao.value, "intencao_json": it.model_dump_json(), "proposta_json": None,
                 "revisao_json": None, "risco_json": risco_json, "resultado": resultado,
                 "motivos": ";".join(motivos), "hash_dossie": h}
        self.r_decisoes.append(linha)
        self.grav.add("decisoes", linha)

    # ------------------------------------------------------------------------------------ fim
    def _finalizar(self) -> ResultadoBacktest:
        t = self._t_ult
        abertas: dict[str, int] = {}
        if self.n_eventos:
            self._gravar_equity(t)
        for b in self.ids:
            q = self.exec.sim.carteira(b).pos.qtd
            if q > 0:
                abertas[b] = q
                self._risco(t, b, "posicao_aberta_fim_dos_dados", {"quantidade": q}, critico=True)
        self.grav.flush()
        ordens = sorted(self.r_ordens.values(), key=lambda o: (o["t_ns"], o["id"]))
        return ResultadoBacktest(
            cenario=self.cfg.cenario, seed=self.cfg.seed, sintetico=self.fonte.sintetico, n_eventos=self.n_eventos,
            bracos=self.ids, decisoes=self.r_decisoes, ordens=ordens, execucoes=self.r_execucoes,
            trades=self.r_trades, equity=self.r_equity, eventos_risco=self.r_risco, latencias=self.r_lat,
            posicoes=self.r_pos, posicoes_abertas_fim=abertas,
            motivos_saida={b: dict(c) for b, c in self.motivos_saida.items()})


# ============================================================================================ cenarios
def executar_cenarios(
    fonte: FonteEventos, fabrica_bracos: Callable[[], Sequence[Braco]], cfg: ConfigBacktest,
    fabrica_banco: Callable[[str], Banco | None] | None = None, cenarios: Sequence[str] = ("base", "estresse"),
) -> dict[str, ResultadoBacktest]:
    """Roda custos-base e estresse (slippage/corretagem agravados) com bracos NOVOS por cenario."""
    out: dict[str, ResultadoBacktest] = {}
    for nome in cenarios:
        if nome == "base":
            c = replace(cfg, cenario="base")
        elif nome == "estresse":
            c = cfg.estresse()
        else:
            raise ValueError(f"cenario desconhecido: {nome}")
        banco = fabrica_banco(nome) if fabrica_banco else None
        out[nome] = MotorBacktest(fonte, fabrica_bracos(), c, banco).executar()
    return out

