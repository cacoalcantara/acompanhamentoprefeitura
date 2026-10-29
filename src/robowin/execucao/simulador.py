"""Simulador interno de execucao, com carteira por braco (netting).

Regras (pessimistas por construcao, ver plano 5.6):
* mercado/saida: ponta contraria da cotacao vigente em t_envio + latencia (injetada);
* limitada: so executa quando um negocio ATRAVESSA o preco, ou quando o volume negociado NO preco supera a
  fila estimada a frente; limitada que cruza o spread na chegada executa como agressora;
* stop: dispara pelo ULTIMO NEGOCIO e executa no PROXIMO preco (primeiro tick posterior, apos a latencia)
  no pior entre o nivel do stop e a cotacao, mais slippage estocastico de cauda pesada com seed;
* lacunas (feed parado/backfill): o stop e aplicado ao pior preco da lacuna;
* saidas nunca invertem a posicao (clamp ao tamanho da posicao; sem posicao => retcode 10036);
* SL/TP do lado errado da cotacao (ou do preco limite) sao recusados na entrada (``RET_INVALID_STOPS``) e na
  modificacao (``modificar_protecao``); a ordem de alvo acompanha a quantidade da posicao (parciais inclusive).
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from ..core.ids import novo_id
from ..core.moedas import motivo_sem_cotacao
from ..core.relogio import Relogio
from ..core.sessoes import SessaoInstrumento, sessao_especifica
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
from .custos import CustoLado, Custos
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


def _sessao_auto(ins: Instrumento) -> SessaoInstrumento | None:
    try:
        from ..core.instrumentos import perfil_de_instrumento
        return sessao_especifica(perfil_de_instrumento(ins))
    except Exception:  # noqa: BLE001 - instrumento sem perfil (testes): sem rollover
        return None


def _sem_negocios(ins: Instrumento) -> bool:
    try:
        from ..core.instrumentos import perfil_de_instrumento
        return not perfil_de_instrumento(ins).tem_negocios
    except Exception:  # noqa: BLE001
        return False


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
    intencao_id: str = ""  # intencao da ordem que abriu a posicao (episodio)


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
    restante: int = 0  # trade: quantidade que continua aberta depois dele (>0 => parcial; 0 => episodio fechado)


MotivoProtecao = Literal["sem_posicao", "ticket_divergente", "stop_pendente", "stop_afrouxaria",
                         "stop_lado_errado", "alvo_lado_errado", "fora_da_grade"]


class SimuladorExecucao:
    """Implementa core.protocolos.Executor (`enviar`) e consome ticks (`on_tick`) e livro (`on_livro`)."""

    def __init__(
        self, instrumento: Instrumento, relogio: Relogio, custos: Custos | None = None,
        latencia_ns: int | Callable[[], int] = 0, seed: int = 0, slippage: ParamsSlippage | None = None,
        fila_padrao: float = 10.0, fator_fila: float = 1.0,
        conversor: Callable[[int], float | None] | None = None, sessao: SessaoInstrumento | None | str = "auto",
    ) -> None:
        """`conversor(t_ns)`: fator moeda de lucro -> moeda da conta no instante (P&L e custos dos trades saem NA
        MOEDA DA CONTA; entrada sem cotacao e rejeitada). `sessao`: rollovers para o swap ("auto" = do perfil)."""
        self.ins = instrumento
        self.relogio = relogio
        self.custos = custos or Custos.do_instrumento(instrumento)
        self.conversor = conversor
        self._ultimo_fator: float | None = None
        self._sessao = _sessao_auto(instrumento) if sessao == "auto" else (
            sessao if isinstance(sessao, SessaoInstrumento) else None)
        self.avisos: list[str] = []
        self.por_cotacao = _sem_negocios(instrumento)  # forex/CFD: stops e limitadas pela cotacao bid/ask
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

    def fator(self, t_ns: int) -> float | None:
        """Fator para a moeda da conta no instante (1.0 sem conversao); guarda o ultimo valido."""
        if not self.ins.precisa_conversao:
            return 1.0
        f = None
        if self.conversor is not None:
            try:
                f = self.conversor(t_ns)
            except Exception:  # noqa: BLE001
                f = None
        if f is not None and f > 0 and math.isfinite(f):
            self._ultimo_fator = f
            return f
        return None

    def _fator_saida(self, t_ns: int) -> float:
        """Saidas nunca sao bloqueadas: sem cotacao no instante, usa a ULTIMA cotacao observada (registrado)."""
        f = self.fator(t_ns)
        if f is not None:
            return f
        if self._ultimo_fator is not None:
            self.avisos.append(f"conversao {self.ins.moeda}->{self.ins.moeda_conta} sem cotacao em {t_ns}: usada a "
                               "ultima observada")
            return self._ultimo_fator
        self.avisos.append(f"conversao {self.ins.moeda}->{self.ins.moeda_conta} nunca cotada: valores na moeda de lucro")
        return 1.0

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
        if cmd.tipo != "saida" and self.fator(t) is None:
            return AckOrdem(retcode=RET_REJECT, comentario=motivo_sem_cotacao(self.ins.moeda, self.ins.moeda_conta))
        if cmd.tipo == "limitada":
            if cmd.preco is None or cmd.preco <= 0 or not self._no_grid(cmd.preco):
                return AckOrdem(retcode=RET_INVALID_PRICE)
        if cmd.tipo == "saida":
            if c.pos.qtd <= 0 or (cmd.position_ticket is not None and cmd.position_ticket != c.pos.ticket):
                return AckOrdem(retcode=RET_POSITION_CLOSED, comentario="posicao inexistente ou ticket velho")
        for nivel in (cmd.sl, cmd.tp):
            if nivel is not None and (nivel <= 0 or not self._no_grid(nivel)):
                return AckOrdem(retcode=RET_INVALID_STOPS)
        if cmd.tipo != "saida" and (m := self._lado_errado_entrada(cmd)):
            return AckOrdem(retcode=RET_INVALID_STOPS, comentario=m)
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

    def _referencias_saida(self, lado: Lado, preco: float | None = None) -> tuple[float, float] | None:
        """(referencia do SL, referencia do TP) de uma posicao ``lado``: a cotacao de saida (bid na compra, ask na
        venda) ou, para limitada ainda nao executada, o proprio preco limite. Sem cotacao conhecida: ``None``."""
        if preco is not None:
            return preco, preco
        if self._ult is None:
            return None
        bid, ask = cotacao_valida(self._ult)
        ref = bid if lado == Lado.COMPRA else ask
        return ref, ref

    @staticmethod
    def _sl_ok(lado: Lado, sl: float, ref: float) -> bool:
        return sl < ref if lado == Lado.COMPRA else sl > ref

    @staticmethod
    def _tp_ok(lado: Lado, tp: float, ref: float) -> bool:
        return tp > ref if lado == Lado.COMPRA else tp < ref

    def _lado_errado_entrada(self, cmd: ComandoOrdem) -> str:
        """Entrada com SL/TP do lado errado (compra: SL < bid < TP; venda: TP < ask < SL), como a corretora."""
        refs = self._referencias_saida(cmd.lado, cmd.preco if cmd.tipo == "limitada" else None)
        if refs is None:
            return ""
        if cmd.sl is not None and not self._sl_ok(cmd.lado, cmd.sl, refs[0]):
            return "stop_lado_errado"
        if cmd.tp is not None and not self._tp_ok(cmd.lado, cmd.tp, refs[1]):
            return "alvo_lado_errado"
        return ""

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

    def modificar_protecao(self, braco: str, ticket: int | None, sl: float | None,
                           tp: float | None) -> MotivoProtecao | None:
        """Modifica SL/TP da posicao do braco (``None`` = nivel inalterado; nunca remove protecao).

        Devolve ``None`` quando aplicado; senao o motivo da recusa (nada e alterado: tudo ou nada). O stop so aperta
        e fica do lado certo da cotacao de saida; o alvo fica do lado do lucro. Um TP novo cancela e recria a ordem
        de alvo (a fila recomeca do fim)."""
        c = self.carteira(braco)
        p = c.pos
        if p.qtd <= 0 or p.lado is None:
            return "sem_posicao"
        if ticket is not None and ticket != p.ticket:
            return "ticket_divergente"
        if p.stop_pendente:
            return "stop_pendente"
        for nivel in (sl, tp):
            if nivel is not None and (nivel <= 0 or not self._no_grid(nivel)):
                return "fora_da_grade"
        refs = self._referencias_saida(p.lado)
        if sl is not None:
            if not stop_so_aperta(p.lado, p.sl, sl):
                return "stop_afrouxaria"
            if refs is not None and not self._sl_ok(p.lado, sl, refs[0]):
                return "stop_lado_errado"
        if tp is not None and refs is not None and not self._tp_ok(p.lado, tp, refs[1]):
            return "alvo_lado_errado"
        if sl is not None:
            p.sl = sl
        if tp is not None and tp != p.tp:
            for tk in [k for k, o in self._ordens.items() if o.magic == c.magic and o.kind == "alvo"]:
                self._ordens.pop(tk, None)
            p.tp = tp
            self._criar_alvo(c, p)
        return None

    def modificar_stop(self, braco: str, novo_sl: float) -> bool:
        """Stop so aperta; qualquer afrouxamento (ou stop do lado errado) e recusado."""
        return self.modificar_protecao(braco, None, novo_sl, None) is None

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
        # 2) limitadas ativas consomem o negocio deste tick (mercado de corretora, sem negocios: a COTACAO precisa
        #    atravessar o preco - estritamente - para preencher no preco limite)
        if self.por_cotacao:
            bid, ask = cotacao_valida(tick)
            for o in sorted(self._ordens.values(), key=lambda x: x.ticket):
                if o.ticket in self._ordens and o.kind in ("limitada", "alvo") and o.ativada and o.fila:
                    px = o.preco or 0.0
                    cruzou = ask < px if o.lado == Lado.COMPRA else bid > px
                    if cruzou and o.restante > 0:
                        ev += self._preencher(o, o.restante, px, tick.t_utc_ns, simulada_slip=0.0)
        elif eh_negocio(tick):
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
        if not self.por_cotacao and not eh_negocio(tick):
            return []
        bid, ask = cotacao_valida(tick)
        for c in self._carteiras.values():
            p = c.pos
            if p.qtd <= 0 or p.sl is None or p.stop_pendente or p.lado is None:
                continue
            if self.por_cotacao:  # sem negocios (forex/CFD): o stop do servidor dispara pela cotacao de saida
                disparou = bid <= p.sl if p.lado == Lado.COMPRA else ask >= p.sl
            else:
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
        bruto_custo = self.custos.lado(d, qtd, zeragem=o.zeragem)
        f = self._fator_saida(t_ns)  # custos na moeda da conta
        custo = CustoLado(tarifas_brl=bruto_custo.tarifas_brl * f, corretagem_brl=bruto_custo.corretagem_brl * f)
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
                p.intencao_id = o.cmd.intencao_id
                p.qtd = novo  # antes do alvo: a ordem de alvo nasce com a quantidade inteira (q>1)
                self._criar_alvo(c, p)
            else:
                p.tarifa_unit = (p.tarifa_unit * p.qtd + tarifa) / novo
                p.corretagem_unit = (p.corretagem_unit * p.qtd + corr) / novo
                p.qtd = novo
                self._redimensionar_saidas(c, crescer=qtd)  # entrada limitada preenchida em partes
            return ev
        # reduz (e possivelmente inverte, apenas via ordem de entrada oposta: netting)
        fecha = min(qtd, p.qtd)
        sinal = 1 if p.lado == Lado.COMPRA else -1
        f = self._fator_saida(t_ns)  # P&L na MOEDA DA CONTA (cotacao do instante da saida)
        bruto = (preco - p.preco_medio) * sinal * fecha * self.ins.ponto_valor_brl * f
        frac = fecha / qtd
        swap = self._swap(p, fecha, t_ns) * f
        tr = Trade(
            id=novo_id("trd"), braco=c.braco, lado=p.lado or Lado.COMPRA, entrada=p.preco_medio, saida=preco,
            t_entrada_ns=p.aberta_ns, t_saida_ns=t_ns, bruto_brl=bruto,
            tarifas_brl=p.tarifa_unit * fecha + tarifa * frac, corretagem_brl=p.corretagem_unit * fecha + corr * frac,
            slippage_brl=0.0,  # slippage ja embutido nos precos de execucao (ver c.slippage_pontos_total)
            motivo_saida=o.motivo or "saida", swap_brl=swap)
        c.trades.append(tr)
        c.saldo_brl += tr.liquido_brl
        p.qtd -= fecha
        ev.append(EventoSim("trade", c.braco, o.ticket, trade=tr, quantidade=fecha, restante=p.qtd))
        resto = qtd - fecha
        if p.qtd > 0:  # parcial: alvo/stop pendentes agem so sobre o restante
            self._redimensionar_saidas(c, outra=o.ticket)
        if p.qtd == 0:
            ev += self._limpar_protecao(c)
            p.lado, p.preco_medio, p.ticket, p.sl, p.tp, p.stop_pendente = None, 0.0, None, None, None, False
            p.intencao_id = ""
            if resto > 0:  # inversao por ordem de ENTRADA oposta (nunca por saida)
                p.lado, p.qtd, p.preco_medio, p.aberta_ns, p.ticket = lado, resto, preco, t_ns, self._novo_ticket()
                p.sl, p.tp = o.cmd.sl, o.cmd.tp
                p.intencao_id = o.cmd.intencao_id
                p.tarifa_unit, p.corretagem_unit = tarifa * (resto / qtd) / resto, corr * (resto / qtd) / resto
                self._criar_alvo(c, p)
        return ev

    def _swap(self, p: PosicaoSim, qtd: int, t_ns: int) -> float:
        """Swap (moeda de lucro) se a posicao atravessou o rollover; no day trade com zeragem, normalmente 0."""
        if self._sessao is None or p.lado is None:
            return 0.0
        noites = sum(m for _, m in self._sessao.rollovers_entre(p.aberta_ns, t_ns, self.custos.swap.dia_triplo
                                                                 if self.custos.swap else ""))
        v = self.custos.swap_custo(p.lado, qtd, noites)
        if v is None:
            self.avisos.append(f"posicao atravessou {noites} rollover(s) e o swap do perfil nao e calculavel "
                               "(swap_long/short/modo a confirmar): swap NAO incluido")
            return 0.0
        return v

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

    def _redimensionar_saidas(self, c: Carteira, crescer: int = 0, outra: int | None = None) -> None:
        """Mantem alvo/stop pendentes do braco do tamanho da posicao: ``crescer`` soma unidades (entrada preenchida em
        partes); sem ``crescer``, ``restante = min(restante, qtd)`` (parcial). ``outra``: ordem em preenchimento,
        que ja desconta o proprio restante em ``_preencher``."""
        p = c.pos
        for o in self._ordens.values():
            if o.magic != c.magic or o.kind not in ("alvo", "stop") or o.ticket == outra:
                continue
            novo = o.restante + crescer if crescer else min(o.restante, p.qtd)
            if o.fila is not None:
                o.fila.restante = max(0, o.fila.restante + (novo - o.restante))
            o.restante = novo

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
