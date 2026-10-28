"""Roteador de execucao: decide simulador interno vs gateway (demo) e liga risco -> OMS -> executor.

Este modulo NAO importa MetaTrader5 e NAO chama `order_send`: no modo `gateway_demo` apenas entrega o
`ComandoOrdem` ao gateway por callback (o gateway, unico dono de `order_send`, valida conta DEMO etc.).
O modo `real` nao existe nesta versao: construir o roteador nele levanta `ExecucaoRealNaoAutorizada`.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..core.protocolos import MotorRisco
from ..core.relogio import Relogio
from ..core.tipos import Acao, AckOrdem, ComandoOrdem, DecisaoRisco, Instrumento, Intencao, Lado, Posicao
from ..risco.antimanipulacao import MonitorAntimanipulacao
from ..risco.kill_switch import EventoKill
from ..risco.motor import MotorRiscoPadrao
from .aprovacao import RegistroAprovacoes
from .oms import OMS, TERMINAIS, OrdemOMS, ResultadoEnvio
from .politicas import PoliticaExecucao, PoliticaPadrao, tipo_de_ordem
from .simulador import EventoSim, SimuladorExecucao


class ExecucaoRealNaoAutorizada(Exception):
    """Ordens reais nao sao implementadas; habilitar exige etapa aprovada e autorizacao expressa."""


@dataclass
class ResultadoExecucao:
    decisao: DecisaoRisco
    cmd: ComandoOrdem | None = None
    envio: ResultadoEnvio | None = None
    motivo: str = ""


class Roteador:
    """Implementa core.protocolos.Executor (`enviar`) e o pipeline intencao -> risco -> OMS."""

    def __init__(
        self, modo: Literal["simulado", "gateway_demo", "real"], instrumento: Instrumento, relogio: Relogio,
        oms: OMS, motor: MotorRisco, simulador: SimuladorExecucao | None = None,
        emissor_gateway: Callable[[ComandoOrdem], AckOrdem | None] | None = None,
        politica: PoliticaExecucao | None = None, antimanipulacao: MonitorAntimanipulacao | None = None,
        bloqueio_externo: Callable[[], str] | None = None,
        posicao_fn: Callable[[str], Posicao] | None = None, exigir_aprovacao: bool = False,
    ) -> None:
        if modo == "real":
            raise ExecucaoRealNaoAutorizada("ordens reais nao implementadas na v1")
        if modo == "simulado" and simulador is None:
            raise ValueError("modo simulado exige simulador")
        if modo == "gateway_demo" and emissor_gateway is None:
            raise ValueError("modo gateway_demo exige emissor_gateway")
        self.modo, self.ins, self.relogio, self.oms, self.motor = modo, instrumento, relogio, oms, motor
        self.sim, self.emissor = simulador, emissor_gateway
        self.politica = politica or PoliticaPadrao()
        self.anti = antimanipulacao
        self.bloqueio_externo = bloqueio_externo
        self._posicao_fn = posicao_fn or (self.sim.posicao if self.sim else None)
        # com ``exigir_aprovacao``, toda ENTRADA enviada precisa ter a impressao (hash) aprovada pelo risco
        self.exigir_aprovacao = exigir_aprovacao
        self.aprovacoes = RegistroAprovacoes()
        self.oms.ao_transicao.append(self._ao_transicao)

    # ------------------------------------------------------------------ Executor -----------------------
    def enviar(self, cmd: ComandoOrdem) -> AckOrdem:
        if self.exigir_aprovacao and cmd.tipo != "saida":  # saidas e protecao nunca esperam aprovacao
            self.aprovacoes.conferir(cmd)
        if self.modo == "simulado":
            assert self.sim is not None
            return self.sim.enviar(cmd)
        assert self.emissor is not None
        ack = self.emissor(cmd)  # o gateway executa; aqui so entregamos o comando
        return ack if ack is not None else AckOrdem(retcode=None, comentario="sem ack sincrono do gateway")

    # ------------------------------------------------------------------ pipeline -----------------------
    def _arred(self, preco: float, para_cima: bool) -> float:
        tk = self.ins.tick_pontos
        return (math.ceil(preco / tk - 1e-9) if para_cima else math.floor(preco / tk + 1e-9)) * tk

    def _montar_entrada(self, it: Intencao, estado: dict[str, float]) -> ComandoOrdem:
        lado = Lado.COMPRA if it.acao == Acao.COMPRAR else Lado.VENDA
        ref = it.preco_referencia
        sp = it.stop_pontos or 0.0
        if lado == Lado.COMPRA:  # arredonda sempre em direcao ao preco (stop mais curto, nunca mais largo)
            sl = self._arred(ref - sp, True)
            tp = self._arred(ref + it.alvo_pontos, False) if it.alvo_pontos else None
        else:
            sl = self._arred(ref + sp, False)
            tp = self._arred(ref - it.alvo_pontos, True) if it.alvo_pontos else None
        tipo = tipo_de_ordem(self.politica.escolher(estado))
        preco = self._arred(ref, lado == Lado.COMPRA) if tipo == "limitada" else None
        return ComandoOrdem(intencao_id=it.id, magic=self.oms.magic(it.braco), lado=lado,
                            tipo="limitada" if tipo == "limitada" else "mercado", quantidade=it.quantidade,
                            preco=preco, sl=sl, tp=tp)

    def _montar_saida(self, intencao_id: str, braco: str, pos: Posicao) -> ComandoOrdem | None:
        if pos.quantidade <= 0 or pos.lado is None:
            return None
        lado = Lado.VENDA if pos.lado == Lado.COMPRA else Lado.COMPRA
        return ComandoOrdem(intencao_id=intencao_id, magic=self.oms.magic(braco), lado=lado, tipo="saida",
                            quantidade=pos.quantidade, position_ticket=pos.ticket)

    def executar_intencao(self, it: Intencao, posicao: Posicao, t_ns: int,
                          estado: dict[str, float] | None = None) -> ResultadoExecucao:
        self.oms.registrar_intencao(it)
        entrada = it.acao in (Acao.COMPRAR, Acao.VENDER)
        if entrada and self.bloqueio_externo is not None and (b := self.bloqueio_externo()):
            return ResultadoExecucao(DecisaoRisco(intencao_id=it.id, aprovada=False, motivos=(f"bloqueio:{b}",)),
                                     motivo=b)
        dec = self.motor.avaliar(it, posicao, t_ns)
        if not dec.aprovada:
            return ResultadoExecucao(dec, motivo="vetada")
        cmd: ComandoOrdem | None = None
        if entrada:
            if (b := self.oms.bloqueado(it.braco, t_ns)):
                return ResultadoExecucao(DecisaoRisco(intencao_id=it.id, aprovada=False, motivos=(f"oms:{b}",)), motivo=b)
            if self.anti is not None:
                ok, mot = self.anti.pode_enviar(it.braco, t_ns)
                if not ok:
                    return ResultadoExecucao(DecisaoRisco(intencao_id=it.id, aprovada=False, motivos=(f"antimanip:{mot}",)),
                                             motivo=mot)
            cmd = self._montar_entrada(it, estado or {})
        elif it.acao == Acao.ENCERRAR:
            cmd = self._montar_saida(it.id, it.braco, posicao)
        elif it.acao == Acao.MANTER and it.stop_pontos is not None and self.sim is not None and posicao.lado:
            novo = it.preco_referencia - it.stop_pontos if posicao.lado == Lado.COMPRA else it.preco_referencia + it.stop_pontos
            self.sim.modificar_stop(it.braco, self._arred(novo, posicao.lado == Lado.COMPRA))
        if cmd is None:
            return ResultadoExecucao(dec, motivo="nada_a_enviar")
        if entrada:
            self.aprovacoes.aprovar(cmd, dec)  # a ordem montada e a que o risco acabou de aprovar
        env = self.oms.enviar(cmd, self)
        if entrada and env.enviado and env.ordem.estado not in TERMINAIS and isinstance(self.motor, MotorRiscoPadrao):
            self.motor.reservar_pendente(it, cmd.lado, t_ns)
        if self.anti is not None and env.enviado and env.ordem.estado not in TERMINAIS:
            self.anti.registrar_nova(env.ordem.id, it.braco, cmd.lado, t_ns)
        return ResultadoExecucao(dec, cmd, env, env.motivo)

    def _ao_transicao(self, o: OrdemOMS) -> None:
        if o.eh_entrada and o.estado in TERMINAIS and isinstance(self.motor, MotorRiscoPadrao):
            self.motor.liberar_pendente(o.intencao_id)

    # ------------------------------------------------------------------ eventos do simulador -------------
    def processar_eventos(self, eventos: list[EventoSim], t_ns: int) -> None:
        for e in eventos:
            if e.tipo == "execucao" and e.execucao is not None:
                o = self.oms.ao_execucao(e.ticket, e.execucao)
                if self.anti is not None and o is not None:
                    self.anti.registrar_execucao(o.id, o.braco, o.cmd.lado, e.execucao.t_ns)
            elif e.tipo == "cancelada":
                self.oms.ao_cancelada(e.ticket)
            elif e.tipo == "rejeitada":
                self.oms.ao_rejeitada(e.ticket, e.motivo)
            elif e.tipo == "trade" and e.trade is not None and isinstance(self.motor, MotorRiscoPadrao):
                self.motor.registrar_trade(e.braco, e.trade.liquido_brl, e.quantidade or 1, e.trade.t_saida_ns)

    def cancelar(self, ordem_id: str, t_ns: int, forcar: bool = False) -> bool:
        """Cancela ordem; respeita tempo minimo no livro, exceto cancelamento forcado (kill/seguranca)."""
        assert self.sim is not None
        if self.anti is not None and not forcar and not self.anti.pode_cancelar(ordem_id, t_ns)[0]:
            return False
        ok = self.oms.cancelar(ordem_id, self.sim.cancelar)
        if ok and self.anti is not None:
            self.anti.registrar_cancelamento(ordem_id, t_ns)
        return ok

    # ------------------------------------------------------------------ zeragem / kill -------------------
    def zerar_braco(self, braco: str, t_ns: int, motivo: str) -> ResultadoEnvio | None:
        if self._posicao_fn is None:
            return None
        cmd = self._montar_saida(f"zerar-{motivo}-{braco}-{t_ns}", braco, self._posicao_fn(braco))
        if cmd is None:
            return None
        env = self.oms.enviar(cmd, self)
        if self.sim is not None and env.ack is not None and env.ack.order:
            self.sim.marcar_zeragem(env.ack.order)
        return env

    def verificar_zeragem(self, braco: str, t_ns: int) -> ResultadoEnvio | None:
        if not isinstance(self.motor, MotorRiscoPadrao) or self._posicao_fn is None:
            return None
        precisa, motivo = self.motor.precisa_zerar(self._posicao_fn(braco), t_ns)
        return self.zerar_braco(braco, t_ns, motivo) if precisa else None

    def efeito_kill(self, ev: EventoKill) -> None:
        """Efeito do kill switch: cancela ordens abertas e zera todos os bracos."""
        if self.sim is not None and ev.cancelar:
            for o in list(self.oms.abertas()):
                if o.ticket and o.estado.value in ("ACEITA", "PARCIAL"):
                    self.cancelar(o.id, ev.t_ns, forcar=True)
        if ev.zerar:
            for braco in self.oms.magic_por_braco:
                self.zerar_braco(braco, ev.t_ns, "kill")
