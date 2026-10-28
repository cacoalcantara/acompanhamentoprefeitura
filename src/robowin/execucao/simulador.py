"""Simulador interno de execucao, com carteira por braco (netting).

Regras (pessimistas por construcao, ver plano 5.6):
* mercado/saida: ponta contraria da cotacao vigente em t_envio + latencia (injetada);
* limitada: so executa quando um negocio ATRAVESSA o preco, ou quando o volume negociado NO preco supera a
  fila estimada a frente; limitada que cruza o spread na chegada executa como agressora;
* stop: dispara pelo ULTIMO NEGOCIO e executa no PROXIMO preco (primeiro tick posterior, apos a latencia)
  no pior entre o nivel do stop e a cotacao, mais slippage estocastico de cauda pesada com seed;
* lacunas (feed parado/backfill): o stop e aplicado ao pior preco da lacuna;
* saidas nunca invertem a posicao (clamp ao tamanho da posicao; sem posicao => retcode 10036).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from ..core.ids import novo_id
from ..core.relogio import Relogio
from ..core.tipos import (
    AckOrdem,
    ComandoOrdem,
    Execucao,
    Instrumento,
    Lado,
    LivroSnapshot,
    Posicao,
    Tick,
    Trade,
)
from ..risco.invariantes import stop_so_aperta
from ..risco.tempo import dia_sp
from .custos import Custos
from .preenchimento import (
    FilaLimitada,
    ParamsSlippage,
    cotacao_valida,
    eh_negocio,
    limitada_marcavel,
    slippage_pontos,
    volume_no_nivel,
)

RET_DONE, RET_PLACED, RET_REJECT, RET_INVALID, RET_INVALID_VOLUME = 10009, 10008, 10006, 10013, 10014
RET_INVALID_PRICE, RET_INVALID_STOPS, RET_MARKET_CLOSED, RET_CLIENT_DISABLES_AT, RET_POSITION_CLOSED = (
    10015, 10016, 10018, 10027, 10036)


@dataclass
class OrdemSim:
    ticket: int
    magic: int
    cmd: ComandoOrdem
    t_envio_ns: int
    ativa_ns: int
    restante: int
    kind: Literal["mercado", "limitada", "saida", "stop", "alvo"]
    lado: Lado
    preco: float | None = None
    fila: FilaLimitada | None = None
    apos_ns: int = 0  # so executa com tick estritamente posterior (stop)
    ativada: bool = False
    motivo: str = ""
    zeragem: bool = False


@dataclass
class PosicaoSim:
    lado: Lado | None = None
    qtd: int = 0
    preco_medio: float = 0.0
    aberta_ns: int = 0
    ticket: int | None = None
    sl: float | None = None
    tp: float | None = None
    tarifa_unit: float = 0.0
    corretagem_unit: float = 0.0
    stop_pendente: bool = False


@dataclass
class Carteira:
    braco: str
    magic: int
    pos: PosicaoSim = field(default_factory=PosicaoSim)
    trades: list[Trade] = field(default_factory=list)
    execucoes: list[Execucao] = field(default_factory=list)
    slippage_pontos_total: float = 0.0
    saldo_brl: float = 0.0

    @property
    def liquido_brl(self) -> float:
        return sum(t.liquido_brl for t in self.trades)


@dataclass(frozen=True)
class EventoSim:
    tipo: Literal["execucao", "trade", "cancelada", "rejeitada"]
    braco: str
    ticket: int
    execucao: Execucao | None = None
    trade: Trade | None = None
    motivo: str = ""
    quantidade: int = 0


class SimuladorExecucao:
    """Implementa core.protocolos.Executor (`enviar`) e consome ticks (`on_tick`) e livro (`on_livro`)."""

    def __init__(
        self, instrumento: Instrumento, relogio: Relogio, custos: Custos | None = None,
        latencia_ns: int | Callable[[], int] = 0, seed: int = 0, slippage: ParamsSlippage | None = None,
        fila_padrao: float = 10.0, fator_fila: float = 1.0,
    ) -> None:
        self.ins = instrumento
        self.relogio = relogio
        self.custos = custos or Custos.do_instrumento(instrumento)
        self._lat = latencia_ns
        self.rng = np.random.default_rng(seed)
        self.slip = slippage or ParamsSlippage()
        self.fila_padrao = fila_padrao
        self.fator_fila = fator_fila
        self.mercado_aberto = True
        self.algotrading = True
        self._carteiras: dict[int, Carteira] = {}
        self._ordens: dict[int, OrdemSim] = {}
        self._tk = 1_000_000
        self._ult: Tick | None = None
        self._livro: LivroSnapshot | None = None

    # ------------------------------------------------------------------ configuracao ---------------
    def registrar_braco(self, braco: str, magic: int) -> Carteira:
        c = Carteira(braco=braco, magic=magic)
        self._carteiras[magic] = c
        return c

    def carteira(self, braco: str) -> Carteira:
        for c in self._carteiras.values():
            if c.braco == braco:
                return c
        raise KeyError(braco)

    def posicao(self, braco: str) -> Posicao:
        p = self.carteira(braco).pos
        return Posicao(simbolo=self.ins.simbolo, lado=p.lado, quantidade=p.qtd, preco_medio=p.preco_medio,
                       ticket=p.ticket, stop=p.sl, alvo=p.tp, aberta_ns=p.aberta_ns)

    def ordens_abertas(self, braco: str | None = None) -> list[OrdemSim]:
        return [o for o in self._ordens.values()
                if braco is None or self._carteiras[o.magic].braco == braco]

    def _latencia(self) -> int:
        return int(self._lat() if callable(self._lat) else self._lat)

    def _novo_ticket(self) -> int:
        self._tk += 1
        return self._tk

    # ------------------------------------------------------------------ envio ------------------------
    def enviar(self, cmd: ComandoOrdem) -> AckOrdem:
        t = self.relogio.agora_ns()
        c = self._carteiras.get(cmd.magic)
        if not self.algotrading:
            return AckOrdem(retcode=RET_CLIENT_DISABLES_AT, comentario="algotrading desligado")
        if c is None:
            return AckOrdem(retcode=RET_INVALID, comentario="magic desconhecido")
        if not self.mercado_aberto:
            return AckOrdem(retcode=RET_MARKET_CLOSED)
        if cmd.quantidade < 1:
            return AckOrdem(retcode=RET_INVALID_VOLUME)
        if cmd.tipo == "limitada":
            if cmd.preco is None or cmd.preco <= 0 or not self._no_grid(cmd.preco):
                return AckOrdem(retcode=RET_INVALID_PRICE)
        if cmd.tipo == "saida":
            if c.pos.qtd <= 0 or (cmd.position_ticket is not None and cmd.position_ticket != c.pos.ticket):
                return AckOrdem(retcode=RET_POSITION_CLOSED, comentario="posicao inexistente ou ticket velho")
        for nivel in (cmd.sl, cmd.tp):
            if nivel is not None and (nivel <= 0 or not self._no_grid(nivel)):
                return AckOrdem(retcode=RET_INVALID_STOPS)
        qtd = cmd.quantidade
        if cmd.tipo == "saida":
            qtd = min(qtd, c.pos.qtd)
            lado = Lado.VENDA if c.pos.lado == Lado.COMPRA else Lado.COMPRA
        else:
            lado = cmd.lado
        tk = self._novo_ticket()
        o = OrdemSim(
            ticket=tk, magic=cmd.magic, cmd=cmd, t_envio_ns=t, ativa_ns=t + self._latencia(), restante=qtd,
            kind="saida" if cmd.tipo == "saida" else cmd.tipo, lado=lado, preco=cmd.preco, motivo="saida" if cmd.tipo == "saida" else "")
        self._ordens[tk] = o
        return AckOrdem(retcode=RET_PLACED, order=tk, t_setup_msc=t // 1_000_000)

    def _no_grid(self, preco: float) -> bool:
        r = preco / self.ins.tick_pontos
        return abs(r - round(r)) < 1e-6

    def cancelar(self, ticket: int) -> AckOrdem:
        o = self._ordens.get(ticket)
        if o is None or o.kind in ("stop",):
            return AckOrdem(retcode=RET_INVALID, comentario="ordem inexistente")
        return self._remover(o, "cancelada")

    def _remover(self, o: OrdemSim, motivo: str) -> AckOrdem:
        self._ordens.pop(o.ticket, None)
        return AckOrdem(retcode=RET_DONE, order=o.ticket)

    def modificar_stop(self, braco: str, novo_sl: float) -> bool:
        """Stop so aperta; qualquer afrouxamento e recusado."""
        p = self.carteira(braco).pos
        if p.qtd <= 0 or p.lado is None or p.stop_pendente:
            return False
        if not stop_so_aperta(p.lado, p.sl, novo_sl):
            return False
        p.sl = novo_sl
        return True

    # ------------------------------------------------------------------ mercado ----------------------
    def on_livro(self, livro: LivroSnapshot) -> None:
        self._livro = livro

    def on_tick(self, tick: Tick) -> list[EventoSim]:
        ev: list[EventoSim] = []
        prev = self._ult
        t = tick.t_utc_ns
        # 1) ordens a mercado / saidas / stops ja disparados
        for o in sorted(self._ordens.values(), key=lambda x: (x.ativa_ns, x.ticket)):
            if o.ticket not in self._ordens or o.ativa_ns > t:
                continue
            if o.kind in ("mercado", "saida"):
                ev += self._exec_mercado(o, prev or tick, tick)
            elif o.kind == "stop" and t > o.apos_ns:
                ev += self._exec_stop(o, tick)
            elif o.kind in ("limitada", "alvo"):
                ev += self._ativar_limitada(o, prev or tick, tick)
        # 2) limitadas ativas consomem o negocio deste tick
        if eh_negocio(tick):
            for o in sorted(self._ordens.values(), key=lambda x: x.ticket):
                if o.ticket in self._ordens and o.kind in ("limitada", "alvo") and o.ativada and o.fila:
                    q = o.fila.processar_negocio(tick.last, tick.volume)
                    if q > 0:
                        ev += self._preencher(o, q, o.preco or tick.last, tick.t_utc_ns, simulada_slip=0.0)
        # 3) gatilho dos stops pelo ultimo negocio
        ev += self._checar_stops(tick)
        self._ult = tick
        return ev

    # ------------------------------------------------------------------ execucoes --------------------
    def _exec_mercado(self, o: OrdemSim, ref: Tick, tick: Tick) -> list[EventoSim]:
        bid, ask = cotacao_valida(ref)
        preco = ask if o.lado == Lado.COMPRA else bid
        return self._preencher(o, o.restante, preco, o.ativa_ns, 0.0)

    def _exec_stop(self, o: OrdemSim, tick: Tick) -> list[EventoSim]:
        c = self._carteiras[o.magic]
        if c.pos.qtd <= 0:
            self._ordens.pop(o.ticket, None)
            return []
        bid, ask = cotacao_valida(tick)
        sl = o.preco or 0.0
        slip = slippage_pontos(self.rng, self.slip, self.ins.tick_pontos)
        if o.lado == Lado.VENDA:  # stop de compra liquidada: vende no pior entre stop e cotacao
            preco = min(sl, bid) - slip
        else:
            preco = max(sl, ask) + slip
        preco = self._arredondar(preco, o.lado)
        return self._preencher(o, o.restante, preco, tick.t_utc_ns, slip)

    def _arredondar(self, preco: float, lado: Lado) -> float:
        tk = self.ins.tick_pontos
        return math.floor(preco / tk + 1e-9) * tk if lado == Lado.VENDA else math.ceil(preco / tk - 1e-9) * tk

    def _ativar_limitada(self, o: OrdemSim, ref: Tick, tick: Tick) -> list[EventoSim]:
        if o.ativada:
            return []
        o.ativada = True
        bid, ask = cotacao_valida(ref)
        preco = o.preco or 0.0
        if limitada_marcavel(o.lado, preco, bid, ask) and o.kind == "limitada":
            px = ask if o.lado == Lado.COMPRA else bid  # executa como agressora (sem melhora alem da cotacao)
            return self._preencher(o, o.restante, px, max(tick.t_utc_ns, o.ativa_ns), 0.0)
        vol = volume_no_nivel(self._livro, o.lado, preco)
        fila = (vol if vol is not None else self.fila_padrao) * self.fator_fila
        o.fila = FilaLimitada(lado=o.lado, preco=preco, restante=o.restante, fila_a_frente=fila)
        return []

    def _checar_stops(self, tick: Tick) -> list[EventoSim]:
        if not eh_negocio(tick):
            return []
        for c in self._carteiras.values():
            p = c.pos
            if p.qtd <= 0 or p.sl is None or p.stop_pendente or p.lado is None:
                continue
            disparou = tick.last <= p.sl if p.lado == Lado.COMPRA else tick.last >= p.sl
            if disparou:
                p.stop_pendente = True
                tk = self._novo_ticket()
                lado = Lado.VENDA if p.lado == Lado.COMPRA else Lado.COMPRA
                self._ordens[tk] = OrdemSim(
                    ticket=tk, magic=c.magic, cmd=ComandoOrdem(intencao_id="stop", magic=c.magic, lado=lado,
                                                               tipo="saida", quantidade=p.qtd),
                    t_envio_ns=tick.t_utc_ns, ativa_ns=tick.t_utc_ns + self._latencia(), restante=p.qtd,
                    kind="stop", lado=lado, preco=p.sl, apos_ns=tick.t_utc_ns, motivo="stop")
        return []

    def _preencher(self, o: OrdemSim, qtd: int, preco: float, t_ns: int, simulada_slip: float) -> list[EventoSim]:
        c = self._carteiras[o.magic]
        eventos: list[EventoSim] = []
        if o.kind in ("saida", "stop", "alvo"):
            qtd = min(qtd, c.pos.qtd)  # saida nunca inverte a posicao
            if qtd <= 0:
                self._ordens.pop(o.ticket, None)
                return [EventoSim("rejeitada", c.braco, o.ticket, motivo="posicao_inexistente")]
        d = dia_sp(t_ns)
        custo = self.custos.lado(d, qtd, zeragem=o.zeragem)
        ex = Execucao(ordem_id=str(o.ticket), preco=preco, quantidade=qtd, t_ns=t_ns,
                      taxas_brl=custo.total, simulada=True)
        c.execucoes.append(ex)
        c.slippage_pontos_total += simulada_slip * qtd
        eventos.append(EventoSim("execucao", c.braco, o.ticket, execucao=ex))
        eventos += self._aplicar(c, o, qtd, preco, t_ns, custo.tarifas_brl, custo.corretagem_brl)
        o.restante -= qtd
        if o.restante <= 0:
            self._ordens.pop(o.ticket, None)
        return eventos

    def _aplicar(self, c: Carteira, o: OrdemSim, qtd: int, preco: float, t_ns: int, tarifa: float, corr: float) -> list[EventoSim]:
        p = c.pos
        ev: list[EventoSim] = []
        lado = o.lado
        if p.qtd == 0 or p.lado == lado:
            novo = p.qtd + qtd
            p.preco_medio = (p.preco_medio * p.qtd + preco * qtd) / novo if p.qtd else preco
            if p.qtd == 0:
                p.lado, p.aberta_ns, p.ticket = lado, t_ns, self._novo_ticket()
                p.sl, p.tp, p.stop_pendente = o.cmd.sl, o.cmd.tp, False
                p.tarifa_unit, p.corretagem_unit = tarifa / qtd, corr / qtd
                self._criar_alvo(c, p)
            else:
                p.tarifa_unit = (p.tarifa_unit * p.qtd + tarifa) / novo
                p.corretagem_unit = (p.corretagem_unit * p.qtd + corr) / novo
            p.qtd = novo
            return ev
        # reduz (e possivelmente inverte, apenas via ordem de entrada oposta: netting)
        fecha = min(qtd, p.qtd)
        sinal = 1 if p.lado == Lado.COMPRA else -1
        bruto = (preco - p.preco_medio) * sinal * fecha * self.ins.ponto_valor_brl
        frac = fecha / qtd
        tr = Trade(
            id=novo_id("trd"), braco=c.braco, lado=p.lado or Lado.COMPRA, entrada=p.preco_medio, saida=preco,
            t_entrada_ns=p.aberta_ns, t_saida_ns=t_ns, bruto_brl=bruto,
            tarifas_brl=p.tarifa_unit * fecha + tarifa * frac, corretagem_brl=p.corretagem_unit * fecha + corr * frac,
            slippage_brl=0.0,  # slippage ja embutido nos precos de execucao (ver c.slippage_pontos_total)
            motivo_saida=o.motivo or "saida")
        c.trades.append(tr)
        c.saldo_brl += tr.liquido_brl
        ev.append(EventoSim("trade", c.braco, o.ticket, trade=tr, quantidade=fecha))
        p.qtd -= fecha
        resto = qtd - fecha
        if p.qtd == 0:
            ev += self._limpar_protecao(c)
            p.lado, p.preco_medio, p.ticket, p.sl, p.tp, p.stop_pendente = None, 0.0, None, None, None, False
            if resto > 0:  # inversao por ordem de ENTRADA oposta (nunca por saida)
                p.lado, p.qtd, p.preco_medio, p.aberta_ns, p.ticket = lado, resto, preco, t_ns, self._novo_ticket()
                p.sl, p.tp = o.cmd.sl, o.cmd.tp
                p.tarifa_unit, p.corretagem_unit = tarifa * (resto / qtd) / resto, corr * (resto / qtd) / resto
                self._criar_alvo(c, p)
        return ev

    def _criar_alvo(self, c: Carteira, p: PosicaoSim) -> None:
        if p.tp is None or p.lado is None:
            return
        tk = self._novo_ticket()
        lado = Lado.VENDA if p.lado == Lado.COMPRA else Lado.COMPRA
        t = self._ult.t_utc_ns if self._ult else self.relogio.agora_ns()
        self._ordens[tk] = OrdemSim(
            ticket=tk, magic=c.magic, cmd=ComandoOrdem(intencao_id="alvo", magic=c.magic, lado=lado, tipo="saida",
                                                       quantidade=p.qtd or 1, preco=p.tp),
            t_envio_ns=t, ativa_ns=t, restante=p.qtd or 1, kind="alvo", lado=lado, preco=p.tp, motivo="alvo")

    def _limpar_protecao(self, c: Carteira) -> list[EventoSim]:
        """Posicao zerada: cancela alvo/stop/saidas pendentes do braco (OCO); saida nunca vira inversao."""
        out: list[EventoSim] = []
        for tk in [k for k, o in self._ordens.items() if o.magic == c.magic and o.kind in ("alvo", "stop", "saida")]:
            self._ordens.pop(tk, None)
            out.append(EventoSim("cancelada", c.braco, tk, motivo="posicao_fechada"))
        return out

    def marcar_zeragem(self, ticket: int) -> None:
        """Marca uma saida pendente como zeragem forcada (cobra taxa de zeragem)."""
        o = self._ordens.get(ticket)
        if o is not None:
            o.zeragem = True
            o.motivo = "zeragem"
