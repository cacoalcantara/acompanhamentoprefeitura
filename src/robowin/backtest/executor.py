"""Ligacao do backtest ao Roteador / SimuladorExecucao / OMS / MotorRiscoPadrao (somente simulacao).

`ExecucaoBacktest` monta, para um conjunto de bracos, a mesma cadeia usada na simulacao em tempo real:
intencao -> risco (veto final) -> OMS -> Roteador("simulado") -> SimuladorExecucao. A latencia do simulador
e INJETADA (`AmostradorLatencia`). Nao importa MetaTrader5 e nao chama `order_send`.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from robowin.core.protocolos import Banco
from robowin.core.relogio import Relogio
from robowin.core.tipos import ConfigRisco, Instrumento, Intencao, Lado, LivroSnapshot, Posicao, Tick
from robowin.execucao.custos import Custos
from robowin.execucao.oms import OMS, OrdemOMS, ResultadoEnvio
from robowin.execucao.politicas import PoliticaExecucao, PoliticaPadrao
from robowin.execucao.preenchimento import ParamsSlippage, cotacao_valida
from robowin.execucao.roteador import ResultadoExecucao, Roteador
from robowin.execucao.simulador import EventoSim, SimuladorExecucao
from robowin.risco.antimanipulacao import MonitorAntimanipulacao
from robowin.risco.kill_switch import EventoKill, KillSwitch
from robowin.risco.motor import EventoAgenda, MotorRiscoPadrao

from .latencia_injetada import AmostradorLatencia

MAGIC_BASE = 1000
INTERVALO_MIN_SAIDA_NS = 1_000_000_000  # no maximo uma tentativa de saida automatica por segundo e por braco


class ExecucaoBacktest:
    def __init__(
        self, instrumento: Instrumento, relogio: Relogio, bracos: Sequence[str], cfg_risco: ConfigRisco,
        custos: Custos, slippage: ParamsSlippage, latencia: AmostradorLatencia, seed: int,
        banco: Banco | None = None, eventos_agenda: Iterable[EventoAgenda] = (),
        kill: KillSwitch | None = None, antimanipulacao: MonitorAntimanipulacao | None = None,
        politica: PoliticaExecucao | None = None, janela_duplicada_s: float = 2.0, fator_fila: float = 1.0,
        ao_ordem: Callable[[OrdemOMS], None] | None = None,
        ao_kill: Callable[[EventoKill], None] | None = None,
    ) -> None:
        self.ins, self.relogio, self.latencia = instrumento, relogio, latencia
        self.bracos = list(bracos)
        self.magics = {b: MAGIC_BASE + i for i, b in enumerate(self.bracos)}
        self.sim = SimuladorExecucao(instrumento, relogio, custos, latencia_ns=latencia, seed=seed,
                                     slippage=slippage, fator_fila=fator_fila)
        for b, m in self.magics.items():
            self.sim.registrar_braco(b, m)
        self.kill = kill or KillSwitch()
        self.motor = MotorRiscoPadrao(cfg_risco, instrumento, kill_switch=self.kill, eventos=eventos_agenda)
        self.oms = OMS(relogio, self.magics, banco=banco, janela_duplicada_s=janela_duplicada_s, simulada=True)
        self.rot = Roteador("simulado", instrumento, relogio, self.oms, self.motor, simulador=self.sim,
                            politica=politica or PoliticaPadrao(), antimanipulacao=antimanipulacao)
        self.kill.registrar_efeito(self.rot.efeito_kill)
        if ao_kill is not None:
            self.kill.registrar_efeito(ao_kill)
        if ao_ordem is not None:
            self.oms.ao_transicao.append(ao_ordem)
        self._ult_saida_ns: dict[str, int] = {}
        self.ultimo_tick: Tick | None = None

    # ------------------------------------------------------------------ mercado ------------------------
    def on_tick(self, tick: Tick) -> list[EventoSim]:
        self.ultimo_tick = tick
        eventos = self.sim.on_tick(tick)
        self.rot.processar_eventos(eventos, tick.t_utc_ns)
        return eventos

    def on_livro(self, livro: LivroSnapshot) -> None:
        self.sim.on_livro(livro)

    # ------------------------------------------------------------------ estado --------------------------
    def posicao(self, braco: str) -> Posicao:
        return self.sim.posicao(braco)

    def tem_posicao(self, braco: str) -> bool:
        return self.sim.carteira(braco).pos.qtd > 0

    def saida_pendente(self, braco: str) -> bool:
        return any(not o.eh_entrada for o in self.oms.abertas(braco))

    def pnl_aberto_brl(self, braco: str) -> float:
        """Marcacao a mercado pelo lado de saida (bid para comprado, ask para vendido)."""
        p = self.sim.carteira(braco).pos
        if p.qtd <= 0 or p.lado is None or self.ultimo_tick is None:
            return 0.0
        bid, ask = cotacao_valida(self.ultimo_tick)
        pv = self.ins.ponto_valor_brl
        return ((bid - p.preco_medio) if p.lado == Lado.COMPRA else (p.preco_medio - ask)) * p.qtd * pv

    def marcar(self, braco: str, t_ns: int) -> None:
        self.motor.marcar_a_mercado(braco, self.pnl_aberto_brl(braco), t_ns)

    # ------------------------------------------------------------------ acoes ---------------------------
    def executar(self, it: Intencao, t_ns: int, estado: dict[str, float] | None = None) -> ResultadoExecucao:
        return self.rot.executar_intencao(it, self.posicao(it.braco), t_ns, estado)

    def zerar_se_preciso(self, braco: str, t_ns: int) -> tuple[str, ResultadoEnvio] | None:
        """Zeragem forcada pelo motor de risco (horario, rolagem, kill...). Devolve (motivo, envio) se enviou."""
        if not self.tem_posicao(braco) or self.saida_pendente(braco):
            return None
        if t_ns - self._ult_saida_ns.get(braco, -(1 << 62)) < INTERVALO_MIN_SAIDA_NS:
            return None
        precisa, motivo = self.motor.precisa_zerar(self.posicao(braco), t_ns)
        if not precisa:
            return None
        self._ult_saida_ns[braco] = t_ns
        env = self.rot.zerar_braco(braco, t_ns, motivo)
        return (motivo, env) if env is not None else None

    def pode_tentar_saida(self, braco: str, t_ns: int) -> bool:
        if not self.tem_posicao(braco) or self.saida_pendente(braco):
            return False
        return t_ns - self._ult_saida_ns.get(braco, -(1 << 62)) >= INTERVALO_MIN_SAIDA_NS

    def marcar_tentativa_saida(self, braco: str, t_ns: int) -> None:
        self._ult_saida_ns[braco] = t_ns
