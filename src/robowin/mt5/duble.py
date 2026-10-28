"""DUBLE DE TESTES do terminal MetaTrader 5 (`DubleMT5`).

*** ISTO NAO E O MERCADO NEM O TERMINAL REAL. E um dublê de testes, alimentado por series
SINTETICAS passadas pelo chamador. Nenhum preco, livro ou resultado produzido aqui representa o WIN. ***
O atributo de classe `eh_duble = True` rotula o objeto; relatorios devem exibi-lo.

Comportamento fiel ao terminal (ver tests/integracao/DUBLE_SPEC.md):
- conta netting (uma posicao por simbolo; ordem oposta reduz/inverte; inversao gera novo ticket);
- SL/TP da posicao vivem no "servidor" e disparam ordem a mercado mesmo sem cliente conectado;
- order_send com None / 10012 / 10031 / 10024 / 10030 / 10027 / 10008 / 10009 / 10010 por injecao de falha,
  inclusive o caso perigoso "executou mas a resposta se perdeu" (`Falha(executa=True)`);
- execucao parcial, troca de conta no meio da sessao, offset nao nulo do relogio do servidor;
- ticks com flags BUY/SELL, backfill (copy_ticks_range apos queda), livro L2 agregado por preco;
- symbols_get com WINZ26/WING27/INDZ26/WDOX26/BOVA11 sinteticos; account_info com demo/contest/real.

O tempo vem do `Relogio` injetado: a cada chamada o dublê "processa" os ticks cujo instante UTC <= agora.
"""
from __future__ import annotations

import bisect
import datetime as dt
import fnmatch
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace as NS
from typing import Any

import numpy as np

from robowin.core.instrumentos import PerfilInstrumento, carregar_perfil
from robowin.core.relogio import Relogio, RelogioReal

from . import constantes as C

_DTYPE_TICK = np.dtype([
    ("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
    ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8"),
])

# (t_utc_ms, bid, ask, last, volume, flags)
TickSint = tuple[int, float, float, float, float, int]
# (t_utc_ms, [(tipo BOOK_*, preco, volume), ...])  -- pode haver varias linhas no mesmo preco
LivroSint = tuple[int, list[tuple[int, float, float]]]


@dataclass
class Falha:
    """Falha injetada para o proximo order_send.

    retcode None => order_send devolve None. `executa=True` => a ordem EXECUTA no servidor mas a
    resposta (None / retcode) chega ao cliente como falha (caso classico de estado INCERTA).
    retcode 10010 => execucao parcial de `volume` (padrao: metade, minimo 1 passo).
    """
    retcode: int | None
    executa: bool = False
    volume: float | None = None


def gerar_ticks_sinteticos(
    inicio_utc_ms: int, n: int, passo_ms: int = 100, preco_inicial: float = 100000.0, semente: int = 0,
    tick: float = 5.0, spread_ticks: int = 1, p_negocio: float = 0.5, p_mesmo_ms: float = 0.1,
    passo_max_ticks: int = 2,
) -> list[TickSint]:
    """Serie SINTETICA (passeio aleatorio em grade de `tick`). Nao representa nenhum mercado real."""
    rng = random.Random(semente)
    n_tk = round(preco_inicial / tick)
    t = inicio_utc_ms
    ultimo = preco_inicial
    out: list[TickSint] = []
    for _ in range(n):
        if out and rng.random() >= p_mesmo_ms:
            t += passo_ms
        n_tk += rng.randint(-passo_max_ticks, passo_max_ticks)
        bid = n_tk * tick
        ask = bid + spread_ticks * tick
        if rng.random() < p_negocio:
            compra = rng.random() < 0.5
            ultimo = ask if compra else bid
            fl = C.FLAG_LAST | C.FLAG_VOLUME | (C.FLAG_BUY if compra else C.FLAG_SELL)
            out.append((t, bid, ask, ultimo, float(rng.randint(1, 5)), fl))
        else:
            out.append((t, bid, ask, ultimo, 0.0, C.FLAG_BID | C.FLAG_ASK))
    return out


def gerar_livros_sinteticos(ticks: Sequence[TickSint], a_cada: int = 10, niveis: int = 5, tick: float = 5.0,
                            semente: int = 0) -> list[LivroSint]:
    """Livros SINTETICOS derivados da serie; repete precos de proposito para exercitar a agregacao."""
    rng = random.Random(semente)
    out: list[LivroSint] = []
    for i in range(0, len(ticks), a_cada):
        t, bid, ask = ticks[i][0], ticks[i][1], ticks[i][2]
        linhas: list[tuple[int, float, float]] = []
        for j in range(niveis):
            linhas.append((C.BOOK_VENDA, ask + j * tick, float(rng.randint(1, 30))))
            linhas.append((C.BOOK_COMPRA, bid - j * tick, float(rng.randint(1, 30))))
        linhas.append((C.BOOK_VENDA, ask, float(rng.randint(1, 5))))  # duplicata de preco (agregacao)
        out.append((t, linhas))
    return out


def especificacao(perfil: PerfilInstrumento, nome: str, vencimento_ts: int = 0) -> NS:
    """symbol_info sintetico de um simbolo do PERFIL (tick, valor do tick por unidade de volume e lote do perfil)."""
    return NS(
        name=nome, path=f"{perfil.classe}\\{nome}", description=f"{perfil.descricao} {nome} (SINTETICO)", digits=0,
        point=1.0, visible=True, select=True, spread=5, spread_float=True,
        trade_mode=4, trade_exemode=C.EXEC_EXCHANGE, filling_mode=C.SIMB_FILL_FOK | C.SIMB_FILL_IOC,
        trade_tick_size=perfil.tick_pontos, trade_tick_value=perfil.tick_valor_brl / perfil.lote,
        trade_contract_size=perfil.ponto_valor_brl / perfil.lote,
        volume_min=float(perfil.lote), volume_max=1000.0 * perfil.lote, volume_step=float(perfil.lote),
        ticks_bookdepth=10 if perfil.aceita_book else 0,
        expiration_time=vencimento_ts, currency_profit="BRL", currency_base="BRL",
    )


def _ts_venc(ano: int, mes: int, dia: int) -> int:
    return int(dt.datetime(ano, mes, dia, 21, tzinfo=dt.UTC).timestamp())


def _para_epoch_s(x: Any) -> float:
    if isinstance(x, dt.datetime):
        if x.tzinfo is None:
            x = x.replace(tzinfo=dt.UTC)  # a API do MT5 trata a hora de parede do servidor como UTC
        return x.timestamp()
    return float(x)


class _Conta:
    def __init__(self, login: int, servidor: str, trade_mode: int, margin_mode: int) -> None:
        self.login, self.servidor, self.trade_mode, self.margin_mode = login, servidor, trade_mode, margin_mode
        self.trade_allowed = True
        self.trade_expert = True
        self.saldo = 100_000.0
        self.pos: dict[str, dict[str, Any]] = {}
        self.ordens: dict[int, dict[str, Any]] = {}
        self.deals: list[NS] = []
        self.ordens_hist: list[NS] = []


class DubleMT5:
    """Dublê de testes do terminal MT5 (SINTETICO; ver docstring do modulo)."""

    eh_duble = True

    def __init__(
        self, relogio: Relogio | None = None, *, login: int = 900001, servidor: str = "XPMT5-DEMO",
        trade_mode: str = "demo", margin_mode: int = C.MARGEM_EXCHANGE, offset_servidor_s: int = 10800,
        rejeita_ticket_velho: bool = True, slippage_pontos: float = 0.0, inicializado: bool = False,
    ) -> None:
        self.relogio = relogio or RelogioReal()
        self.offset_ms = int(offset_servidor_s) * 1000
        self.rejeita_ticket_velho = rejeita_ticket_velho
        self.slippage_pontos = slippage_pontos
        modo = {"demo": C.CONTA_DEMO, "contest": C.CONTA_CONTEST, "real": C.CONTA_REAL}[trade_mode]
        self._contas: dict[int, _Conta] = {login: _Conta(login, servidor, modo, margin_mode)}
        self._conta = self._contas[login]
        self.inicializado = inicializado
        self.conectado = True
        self.terminal_algo_ligado = True
        self._ultimo_erro = C.ERR_OK
        self._especs: dict[str, NS] = {}
        self._ticks: dict[str, list[TickSint]] = {}
        self._tms: dict[str, list[int]] = {}
        self._idx: dict[str, int] = {}
        self._ult: dict[str, TickSint] = {}
        self._livros: dict[str, list[LivroSint]] = {}
        self._livro_tms: dict[str, list[int]] = {}
        self._assinados: set[str] = set()
        self._id = 5_000_000
        self._falhas: list[Falha] = []
        self._falhas_check: list[int | None] = []
        # observabilidade para testes
        self.envios: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.chamadas_initialize: list[dict[str, Any]] = []
        self.gancho_antes_order_send: Callable[[DubleMT5], None] | None = None

    # ------------------------------------------------------------------------------------------
    # configuracao (lado do teste)
    # ------------------------------------------------------------------------------------------
    def adicionar_simbolo(self, espec: NS, ticks: Iterable[TickSint] = (), livros: Iterable[LivroSint] = ()) -> None:
        nome = espec.name
        self._especs[nome] = espec
        tk = sorted(ticks, key=lambda x: x[0])
        self._ticks[nome] = tk
        self._tms[nome] = [x[0] for x in tk]
        self._idx[nome] = 0
        lv = sorted(livros, key=lambda x: x[0])
        self._livros[nome] = lv
        self._livro_tms[nome] = [x[0] for x in lv]

    @property
    def especificacoes(self) -> dict[str, NS]:
        """symbol_info mutavel por simbolo (o teste pode adulterar para simular divergencias)."""
        return self._especs

    def injetar_falhas(self, *falhas: Falha | int | None) -> None:
        for f in falhas:
            self._falhas.append(f if isinstance(f, Falha) else Falha(f))

    def injetar_falhas_check(self, *retcodes: int | None) -> None:
        self._falhas_check.extend(retcodes)

    def trocar_conta(self, login: int, servidor: str | None = None, trade_mode: str | None = None) -> None:
        """Troca a conta logada no meio da sessao (o estado de posicoes/ordens e por conta)."""
        c = self._contas.get(login)
        if c is None:
            c = _Conta(login, servidor or self._conta.servidor, self._conta.trade_mode, self._conta.margin_mode)
            self._contas[login] = c
        if servidor:
            c.servidor = servidor
        if trade_mode:
            c.trade_mode = {"demo": C.CONTA_DEMO, "contest": C.CONTA_CONTEST, "real": C.CONTA_REAL}[trade_mode]
        self._conta = c

    def definir_conta(self, **kw: Any) -> None:
        """Ajusta atributos da conta atual (trade_allowed, trade_expert, trade_mode, servidor ...)."""
        for k, v in kw.items():
            if k == "trade_mode" and isinstance(v, str):
                v = {"demo": C.CONTA_DEMO, "contest": C.CONTA_CONTEST, "real": C.CONTA_REAL}[v]
            setattr(self._conta, k, v)

    def desconectar(self) -> None:
        self.conectado = False

    def reconectar(self) -> None:
        self.conectado = True

    def operar_manual(self, lado: int, volume: float, simbolo: str, razao: int = C.RAZAO_CLIENT) -> NS:
        """Simula intervencao manual do usuario no terminal/celular/web (deal com reason CLIENT/MOBILE/WEB)."""
        self._sincronizar()
        return self._executar_deal(self._conta, simbolo, lado, volume, self._preco_exec(simbolo, lado), 0, razao, 0.0, 0.0)

    # consultas auxiliares de teste -------------------------------------------------------------
    def posicao_liquida(self, simbolo: str) -> float:
        self._sincronizar()
        p = self._conta.pos.get(simbolo)
        if not p:
            return 0.0
        return p["volume"] * (1 if p["type"] == C.POS_COMPRA else -1)

    def volume_pendente(self, simbolo: str) -> float:
        self._sincronizar()
        return sum(o["volume_current"] for o in self._conta.ordens.values() if o["symbol"] == simbolo)

    @property
    def deals_todos(self) -> list[NS]:
        self._sincronizar()
        return list(self._conta.deals)

    # ------------------------------------------------------------------------------------------
    # tempo e processamento do "servidor"
    # ------------------------------------------------------------------------------------------
    def _agora_ms(self) -> int:
        return self.relogio.agora_ns() // 1_000_000

    def _srv_ms(self, utc_ms: int) -> int:
        return utc_ms + self.offset_ms

    def _novo_id(self) -> int:
        self._id += 1
        return self._id

    def _sincronizar(self) -> None:
        agora = self._agora_ms()
        for sym, tms in self._tms.items():
            i = self._idx[sym]
            n = bisect.bisect_right(tms, agora)
            while i < n:
                self._aplicar_tick(sym, self._ticks[sym][i])
                i += 1
            self._idx[sym] = i

    def _aplicar_tick(self, sym: str, tk: TickSint) -> None:
        self._ult[sym] = tk
        _, bid, ask, _, _, _ = tk
        for conta in self._contas.values():
            # ordens pendentes limitadas
            for t in list(conta.ordens):
                o = conta.ordens.get(t)
                if o is None or o["symbol"] != sym:
                    continue
                cruza = (o["type"] == C.ORDEM_BUY_LIMIT and ask <= o["price_open"]) or (
                    o["type"] == C.ORDEM_SELL_LIMIT and bid >= o["price_open"])
                if cruza:
                    del conta.ordens[t]
                    lado = C.DEAL_COMPRA if o["type"] == C.ORDEM_BUY_LIMIT else C.DEAL_VENDA
                    self._executar_deal(conta, sym, lado, o["volume_current"], o["price_open"], o["magic"],
                                        C.RAZAO_EXPERT, o["sl"], o["tp"], ordem=t, t_ms=tk[0])
            # SL/TP da posicao: vivem no servidor
            p = conta.pos.get(sym)
            if not p:
                continue
            if p["type"] == C.POS_COMPRA:
                saida_lado, ref = C.DEAL_VENDA, bid
                bate_sl = p["sl"] > 0 and bid <= p["sl"]
                bate_tp = p["tp"] > 0 and bid >= p["tp"]
            else:
                saida_lado, ref = C.DEAL_COMPRA, ask
                bate_sl = p["sl"] > 0 and ask >= p["sl"]
                bate_tp = p["tp"] > 0 and ask <= p["tp"]
            if bate_sl:  # ordem a mercado: executa ao preco do tick (pior preco da lacuna)
                self._executar_deal(conta, sym, saida_lado, p["volume"], ref, p["magic"], C.RAZAO_SL, 0.0, 0.0,
                                    t_ms=tk[0])
            elif bate_tp:
                self._executar_deal(conta, sym, saida_lado, p["volume"], p["tp"], p["magic"], C.RAZAO_TP, 0.0, 0.0,
                                    t_ms=tk[0])

    def _preco_exec(self, sym: str, lado: int) -> float:
        tk = self._ult.get(sym)
        if tk is None:
            return 0.0
        slip = self.slippage_pontos
        return tk[2] + slip if lado == C.DEAL_COMPRA else tk[1] - slip

    def _valor_ponto(self, sym: str) -> float:
        e = self._especs[sym]
        return e.trade_tick_value / e.trade_tick_size

    def _executar_deal(
        self, conta: _Conta, sym: str, lado: int, volume: float, preco: float, magic: int, razao: int,
        sl: float, tp: float, ordem: int | None = None, t_ms: int | None = None,
    ) -> NS:
        """Aplica um negocio na conta (NETTING). Retorna o deal."""
        t_ms = self._agora_ms() if t_ms is None else t_ms
        srv_ms = self._srv_ms(t_ms)
        ordem = ordem or self._novo_id()
        pos = conta.pos.get(sym)
        sinal_ordem = 1 if lado == C.DEAL_COMPRA else -1
        profit = 0.0
        entrada = C.ENTRADA_IN
        pos_id = 0

        def nova_posicao(vol: float) -> dict[str, Any]:
            tid = self._novo_id()
            p = dict(ticket=tid, identifier=tid, symbol=sym, type=C.POS_COMPRA if lado == C.DEAL_COMPRA else C.POS_VENDA,
                     volume=vol, price_open=preco, sl=sl, tp=tp, magic=magic, time_msc=srv_ms, reason=razao)
            conta.pos[sym] = p
            return p

        if pos is None:
            pos_id = nova_posicao(volume)["ticket"]
        else:
            sinal_pos = 1 if pos["type"] == C.POS_COMPRA else -1
            if sinal_pos == sinal_ordem:
                tot = pos["volume"] + volume
                pos["price_open"] = (pos["price_open"] * pos["volume"] + preco * volume) / tot
                pos["volume"] = tot
                if sl:
                    pos["sl"] = sl
                if tp:
                    pos["tp"] = tp
                pos_id = pos["ticket"]
            else:
                fecha = min(volume, pos["volume"])
                profit = (preco - pos["price_open"]) * sinal_pos * fecha * self._valor_ponto(sym)
                pos_id = pos["ticket"]
                entrada = C.ENTRADA_OUT
                pos["volume"] -= fecha
                if pos["volume"] <= 1e-12:
                    del conta.pos[sym]
                resto = volume - fecha
                if resto > 1e-12:  # inversao (netting): um unico deal INOUT e uma NOVA posicao
                    entrada = C.ENTRADA_INOUT
                    nova_posicao(resto)
        deal = NS(
            ticket=self._novo_id(), order=ordem, time=srv_ms // 1000, time_msc=srv_ms, type=lado, entry=entrada,
            magic=magic, position_id=pos_id, reason=razao, volume=volume, price=preco, commission=0.0, swap=0.0,
            profit=profit, fee=0.0, symbol=sym, comment="", external_id="",
        )
        conta.deals.append(deal)
        conta.saldo += profit
        conta.ordens_hist.append(NS(
            ticket=ordem, time_setup=srv_ms // 1000, time_setup_msc=srv_ms, time_done=srv_ms // 1000,
            time_done_msc=srv_ms, type=lado, state=4, magic=magic, position_id=pos_id, symbol=sym,
            volume_initial=volume, volume_current=0.0, price_open=preco, sl=sl, tp=tp, reason=razao,
        ))
        return deal

    # ------------------------------------------------------------------------------------------
    # Protocolo TerminalMT5
    # ------------------------------------------------------------------------------------------
    def _ok(self) -> bool:
        if not self.inicializado:
            self._ultimo_erro = (-10004, "No IPC connection")
            return False
        if not self.conectado:
            self._ultimo_erro = C.ERR_SEM_CONEXAO
            return False
        return True

    def initialize(self, **kw: Any) -> bool:
        self.chamadas_initialize.append({k: v for k, v in kw.items() if k != "password"})
        self.inicializado = True
        self._ultimo_erro = C.ERR_OK
        return True

    def shutdown(self) -> None:
        self.inicializado = False

    def last_error(self) -> tuple[int, str]:
        return self._ultimo_erro

    def account_info(self) -> Any:
        self._sincronizar()
        if not self._ok():
            return None
        c = self._conta
        return NS(
            login=c.login, server=c.servidor, trade_mode=c.trade_mode, margin_mode=c.margin_mode,
            trade_allowed=c.trade_allowed, trade_expert=c.trade_expert, leverage=1, currency="BRL",
            balance=c.saldo, equity=c.saldo, profit=0.0, margin=0.0, margin_free=c.saldo,
            name="CONTA SINTETICA (DUBLE)", company="CORRETORA SINTETICA (DUBLE)", limit_orders=0,
        )

    def terminal_info(self) -> Any:
        if not self.inicializado:
            return None
        return NS(
            connected=self.conectado, trade_allowed=self.terminal_algo_ligado, tradeapi_disabled=False,
            community_account=False, dlls_allowed=False, path="DUBLE", data_path="DUBLE",
            name="DUBLE MT5 (TESTES)", company="SINTETICO", build=0, ping_last=1000,
        )

    def symbols_get(self, group: str | None = None) -> Sequence[Any] | None:
        if not self._ok():
            return None
        nomes = list(self._especs)
        if group:
            pads = [p.strip() for p in group.split(",") if p.strip()]
            inc = [p for p in pads if not p.startswith("!")]
            exc = [p[1:] for p in pads if p.startswith("!")]
            nomes = [n for n in nomes
                     if (not inc or any(fnmatch.fnmatchcase(n, p) for p in inc))
                     and not any(fnmatch.fnmatchcase(n, p) for p in exc)]
        return tuple(NS(**vars(self._especs[n])) for n in nomes)

    def symbol_info(self, symbol: str) -> Any:
        if not self._ok() or symbol not in self._especs:
            return None
        return NS(**vars(self._especs[symbol]))

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        if symbol not in self._especs:
            return False
        self._especs[symbol].select = self._especs[symbol].visible = bool(enable)
        return True

    def symbol_info_tick(self, symbol: str) -> Any:
        self._sincronizar()
        if not self._ok():
            return None
        tk = self._ult.get(symbol)
        if tk is None:
            return None
        srv = self._srv_ms(tk[0])
        return NS(time=srv // 1000, bid=tk[1], ask=tk[2], last=tk[3], volume=int(tk[4]), time_msc=srv,
                  flags=tk[5], volume_real=tk[4])

    def _linhas_ticks(self, symbol: str, ini_srv_ms: float, fim_srv_ms: float | None, limite: int | None, flags: int) -> Any:
        self._sincronizar()
        agora = self._agora_ms()
        if symbol not in self._ticks:
            return None
        tk = self._ticks[symbol]
        tms = self._tms[symbol]
        i0 = bisect.bisect_left(tms, int(ini_srv_ms) - self.offset_ms)
        i1 = bisect.bisect_right(tms, agora)
        if fim_srv_ms is not None:
            i1 = min(i1, bisect.bisect_right(tms, int(fim_srv_ms) - self.offset_ms))
        mascara = {C.COPY_TICKS_ALL: 0, C.COPY_TICKS_INFO: C.FLAG_BID | C.FLAG_ASK,
                   C.COPY_TICKS_TRADE: C.FLAG_LAST | C.FLAG_VOLUME}.get(flags, 0)
        sel = [x for x in tk[i0:i1] if not mascara or (x[5] & mascara)]
        if limite is not None:
            sel = sel[:limite]
        arr = np.zeros(len(sel), dtype=_DTYPE_TICK)
        for k, (t, bid, ask, last, vol, fl) in enumerate(sel):
            srv = self._srv_ms(t)
            arr[k] = (srv // 1000, bid, ask, last, int(vol), srv, fl, vol)
        return arr

    def copy_ticks_from(self, symbol: str, date_from: Any, count: int, flags: int) -> Any:
        if not self._ok():
            return None
        return self._linhas_ticks(symbol, _para_epoch_s(date_from) * 1000, None, int(count), flags)

    def copy_ticks_range(self, symbol: str, date_from: Any, date_to: Any, flags: int) -> Any:
        if not self._ok():
            return None
        return self._linhas_ticks(symbol, _para_epoch_s(date_from) * 1000, _para_epoch_s(date_to) * 1000 + 999, None, flags)

    def market_book_add(self, symbol: str) -> bool:
        if not self._ok() or symbol not in self._especs or self._especs[symbol].ticks_bookdepth <= 0:
            return False
        self._assinados.add(symbol)
        return True

    def market_book_release(self, symbol: str) -> bool:
        self._assinados.discard(symbol)
        return True

    def market_book_get(self, symbol: str) -> Any:
        self._sincronizar()
        if not self._ok() or symbol not in self._assinados:
            return None
        tms = self._livro_tms.get(symbol, [])
        i = bisect.bisect_right(tms, self._agora_ms())
        if i == 0:
            return None
        agg: dict[tuple[int, float], float] = {}
        for tipo, preco, vol in self._livros[symbol][i - 1][1]:
            agg[(tipo, preco)] = agg.get((tipo, preco), 0.0) + vol
        prof = int(self._especs[symbol].ticks_bookdepth)
        vendas = sorted([(p, v) for (t, p), v in agg.items() if t in (C.BOOK_VENDA, C.BOOK_VENDA_MERCADO)])[:prof]
        compras = sorted([(p, v) for (t, p), v in agg.items() if t in (C.BOOK_COMPRA, C.BOOK_COMPRA_MERCADO)],
                         reverse=True)[:prof]
        saida = [NS(type=C.BOOK_VENDA, price=p, volume=int(v), volume_dbl=v) for p, v in sorted(vendas, reverse=True)]
        saida += [NS(type=C.BOOK_COMPRA, price=p, volume=int(v), volume_dbl=v) for p, v in compras]
        return tuple(saida)

    # --- estado de posicoes/ordens/historico ---------------------------------------------------
    def _ns_posicao(self, p: dict[str, Any]) -> NS:
        tk = self._ult.get(p["symbol"])
        atual = (tk[1] if p["type"] == C.POS_COMPRA else tk[2]) if tk else p["price_open"]
        sinal = 1 if p["type"] == C.POS_COMPRA else -1
        lucro = (atual - p["price_open"]) * sinal * p["volume"] * self._valor_ponto(p["symbol"])
        return NS(ticket=p["ticket"], time=p["time_msc"] // 1000, time_msc=p["time_msc"], time_update=p["time_msc"] // 1000,
                  time_update_msc=p["time_msc"], type=p["type"], magic=p["magic"], identifier=p["identifier"],
                  reason=p["reason"], volume=p["volume"], price_open=p["price_open"], sl=p["sl"], tp=p["tp"],
                  price_current=atual, swap=0.0, profit=lucro, symbol=p["symbol"], comment="")

    def positions_get(self, **kw: Any) -> Any:
        self._sincronizar()
        if not self._ok():
            return None
        ps = list(self._conta.pos.values())
        if kw.get("symbol"):
            ps = [p for p in ps if p["symbol"] == kw["symbol"]]
        if kw.get("group"):
            ps = [p for p in ps if fnmatch.fnmatchcase(p["symbol"], kw["group"])]
        if kw.get("ticket"):
            ps = [p for p in ps if p["ticket"] == kw["ticket"]]
        return tuple(self._ns_posicao(p) for p in ps)

    def orders_get(self, **kw: Any) -> Any:
        self._sincronizar()
        if not self._ok():
            return None
        os_ = list(self._conta.ordens.values())
        if kw.get("symbol"):
            os_ = [o for o in os_ if o["symbol"] == kw["symbol"]]
        if kw.get("ticket"):
            os_ = [o for o in os_ if o["ticket"] == kw["ticket"]]
        return tuple(NS(**o) for o in os_)

    def _filtrar_hist(self, lista: list[NS], a: tuple[Any, ...], kw: dict[str, Any]) -> tuple[NS, ...]:
        out = lista
        if kw.get("ticket"):
            out = [x for x in out if x.ticket == kw["ticket"]]
        if kw.get("position"):
            out = [x for x in out if x.position_id == kw["position"]]
        if len(a) >= 2:
            d0, d1 = _para_epoch_s(a[0]), _para_epoch_s(a[1])
            out = [x for x in out if d0 <= x.time_msc / 1000.0 <= d1 + 0.999]
        return tuple(out)

    def history_deals_get(self, *a: Any, **kw: Any) -> Any:
        self._sincronizar()
        if not self._ok():
            return None
        return self._filtrar_hist(self._conta.deals, a, kw)

    def history_orders_get(self, *a: Any, **kw: Any) -> Any:
        self._sincronizar()
        if not self._ok():
            return None
        return self._filtrar_hist([NS(**{**vars(o), "time_msc": o.time_done_msc}) for o in self._conta.ordens_hist], a, kw)

    # --- ordens --------------------------------------------------------------------------------
    def _validar(self, req: dict[str, Any]) -> tuple[int, str]:
        """Validacoes do servidor, sem alterar estado. (0, '') se aceitavel."""
        c = self._conta
        if not self.terminal_algo_ligado:
            return C.RET_ALGO_DESLIGADO_CLIENTE, "AutoTrading desligado no terminal"
        if not c.trade_allowed:
            return C.RET_TRADE_DESABILITADO, "negociacao desabilitada na conta"
        acao = req.get("action")
        sym = req.get("symbol")
        if acao in (C.ACAO_DEAL, C.ACAO_PENDING):
            esp = self._especs.get(sym)
            if esp is None:
                return C.RET_INVALIDO, "simbolo desconhecido"
            vol = float(req.get("volume", 0))
            if vol < esp.volume_min or vol > esp.volume_max or abs(vol / esp.volume_step - round(vol / esp.volume_step)) > 1e-9:
                return C.RET_VOLUME_INVALIDO, "volume invalido"
            fill = req.get("type_filling", C.PREENCHE_FOK)
            ok_fill = ((fill == C.PREENCHE_FOK and esp.filling_mode & C.SIMB_FILL_FOK)
                       or (fill == C.PREENCHE_IOC and esp.filling_mode & C.SIMB_FILL_IOC)
                       or (fill == C.PREENCHE_RETURN and esp.trade_exemode != C.EXEC_MARKET))
            if not ok_fill:
                return C.RET_PREENCHIMENTO_INVALIDO, "modo de preenchimento nao suportado"
            tk = self._ult.get(sym)
            if tk is None:
                return C.RET_MERCADO_FECHADO, "sem cotacao"
            tipo = req.get("type")
            sl, tp = float(req.get("sl", 0) or 0), float(req.get("tp", 0) or 0)
            if acao == C.ACAO_DEAL:
                if tipo not in (C.ORDEM_BUY, C.ORDEM_SELL):
                    return C.RET_INVALIDO, "tipo invalido para DEAL"
                pos_req = req.get("position", 0)
                if pos_req and self.rejeita_ticket_velho:
                    p = c.pos.get(sym)
                    if p is None or p["ticket"] != pos_req:
                        return C.RET_POSICAO_FECHADA, "posicao ja encerrada (ticket velho)"
                    mesmo_lado = (p["type"] == C.POS_COMPRA) == (tipo == C.ORDEM_BUY)
                    if mesmo_lado or vol > p["volume"] + 1e-12:
                        return C.RET_INVALIDO, "saida incompativel com a posicao"
                if tipo == C.ORDEM_BUY and ((sl and sl >= tk[1]) or (tp and tp <= tk[2])):
                    return C.RET_STOPS_INVALIDOS, "stops invalidos"
                if tipo == C.ORDEM_SELL and ((sl and sl <= tk[2]) or (tp and tp >= tk[1])):
                    return C.RET_STOPS_INVALIDOS, "stops invalidos"
            else:
                preco = float(req.get("price", 0))
                if tipo == C.ORDEM_BUY_LIMIT and preco >= tk[2]:
                    return C.RET_PRECO_INVALIDO, "preco de compra limitada deve ficar abaixo do ask"
                if tipo == C.ORDEM_SELL_LIMIT and preco <= tk[1]:
                    return C.RET_PRECO_INVALIDO, "preco de venda limitada deve ficar acima do bid"
                if tipo not in (C.ORDEM_BUY_LIMIT, C.ORDEM_SELL_LIMIT):
                    return C.RET_INVALIDO, "tipo pendente nao suportado"
        elif acao == C.ACAO_SLTP:
            p = c.pos.get(sym)
            if p is None or (req.get("position") and p["ticket"] != req["position"]):
                return C.RET_POSICAO_FECHADA, "posicao inexistente"
        elif acao == C.ACAO_REMOVE:
            if req.get("order") not in c.ordens:
                return C.RET_INVALIDO, "ordem inexistente"
        elif acao == C.ACAO_MODIFY:
            if req.get("order") not in c.ordens:
                return C.RET_INVALIDO, "ordem inexistente"
        else:
            return C.RET_INVALIDO, "acao desconhecida"
        return 0, ""

    def order_check(self, request: dict[str, Any]) -> Any:
        self._sincronizar()
        self.checks.append(dict(request))
        if self._falhas_check:
            f = self._falhas_check.pop(0)
            if f is None:
                return None
            return NS(retcode=f, comment="falha injetada", request=NS(**request))
        if not self._ok():
            return None
        ret, com = self._validar(request)
        return NS(retcode=ret, comment=com or "Done", balance=self._conta.saldo, request=NS(**request))

    def _resultado(self, ret: int, req: dict[str, Any], com: str = "", deal: int = 0, order: int = 0,
                   vol: float = 0.0, preco: float = 0.0) -> NS:
        tk = self._ult.get(req.get("symbol", ""), (0, 0.0, 0.0, 0.0, 0.0, 0))
        return NS(retcode=ret, deal=deal, order=order, volume=vol, price=preco, bid=tk[1], ask=tk[2], comment=com,
                  request_id=self._novo_id(), retcode_external=0, request=NS(**req))

    def order_send(self, request: dict[str, Any]) -> Any:
        self._sincronizar()
        self.envios.append(dict(request))
        if self.gancho_antes_order_send is not None:
            self.gancho_antes_order_send(self)
        falha = self._falhas.pop(0) if self._falhas else None
        if falha is not None and not falha.executa and falha.retcode != C.RET_PARCIAL:
            return None if falha.retcode is None else self._resultado(falha.retcode, request, "falha injetada")
        if not self._ok():
            return self._resultado(C.RET_SEM_CONEXAO, request, "sem conexao")
        res = self._processar(request, falha)
        if falha is not None and falha.executa:  # executou no servidor, mas a resposta se perdeu
            return None if falha.retcode is None else self._resultado(falha.retcode, request, "falha injetada")
        return res

    def _processar(self, req: dict[str, Any], falha: Falha | None) -> NS:
        ret, com = self._validar(req)
        if ret != 0:
            return self._resultado(ret, req, com)
        c = self._conta
        acao, sym = req["action"], req.get("symbol")
        magic = int(req.get("magic", 0))
        if acao == C.ACAO_DEAL:
            vol = float(req["volume"])
            lado = C.DEAL_COMPRA if req["type"] == C.ORDEM_BUY else C.DEAL_VENDA
            parcial = falha is not None and falha.retcode == C.RET_PARCIAL
            if parcial:
                step = self._especs[sym].volume_step
                vol_exec = falha.volume if falha.volume is not None else max(step, float(int(vol / 2 / step)) * step)
                vol_exec = min(vol_exec, vol)
            else:
                vol_exec = vol
            sl, tp = float(req.get("sl", 0) or 0), float(req.get("tp", 0) or 0)
            deal = self._executar_deal(c, sym, lado, vol_exec, self._preco_exec(sym, lado), magic, C.RAZAO_EXPERT, sl, tp)
            ret = C.RET_PARCIAL if vol_exec < vol else C.RET_EXECUTADA
            return self._resultado(ret, req, "Request executed", deal.ticket, deal.order, vol_exec, deal.price)
        if acao == C.ACAO_PENDING:
            tid = self._novo_id()
            agora_srv = self._srv_ms(self._agora_ms())
            c.ordens[tid] = dict(
                ticket=tid, time_setup=agora_srv // 1000, time_setup_msc=agora_srv, type=req["type"], state=1,
                magic=magic, position_id=0, symbol=sym, volume_initial=float(req["volume"]),
                volume_current=float(req["volume"]), price_open=float(req["price"]), sl=float(req.get("sl", 0) or 0),
                tp=float(req.get("tp", 0) or 0), type_filling=req.get("type_filling", 0), reason=C.RAZAO_EXPERT)
            return self._resultado(C.RET_COLOCADA, req, "Request placed", 0, tid, float(req["volume"]), float(req["price"]))
        if acao == C.ACAO_SLTP:
            p = c.pos[sym]
            p["sl"], p["tp"] = float(req.get("sl", 0) or 0), float(req.get("tp", 0) or 0)
            return self._resultado(C.RET_EXECUTADA, req, "Request executed")
        if acao == C.ACAO_REMOVE:
            del c.ordens[req["order"]]
            return self._resultado(C.RET_EXECUTADA, req, "Request executed", 0, req["order"])
        o = c.ordens[req["order"]]  # MODIFY
        o["price_open"] = float(req.get("price", o["price_open"]))
        o["sl"], o["tp"] = float(req.get("sl", o["sl"]) or 0), float(req.get("tp", o["tp"]) or 0)
        return self._resultado(C.RET_EXECUTADA, req, "Request executed", 0, req["order"])


def criar_duble_padrao(
    relogio: Relogio | None = None, *, inicio_utc_ms: int | None = None, n_ticks: int = 6000, passo_ms: int = 100,
    semente: int = 1, **kw: Any,
) -> DubleMT5:
    """DubleMT5 pronto para uso com dados 100% SINTETICOS (WINZ26, WING27, INDZ26, WDOX26, BOVA11).

    A serie do WINZ26 comeca em `inicio_utc_ms` (padrao: 1 s antes de agora) e avanca no tempo do relogio.
    """
    d = DubleMT5(relogio, **kw)
    ini = inicio_utc_ms if inicio_utc_ms is not None else d._agora_ms() - 1000

    def perfil(a: str) -> PerfilInstrumento:
        return carregar_perfil(a, permitir_a_confirmar=True)  # o duble simula a corretora de qualquer perfil

    def serie(p: PerfilInstrumento, n: int, passo: int, sem: int) -> list[TickSint]:
        return gerar_ticks_sinteticos(ini, n, passo, p.preco_sintetico_ref, sem, tick=p.tick_pontos)

    win, ind, wdo, dol, acao, etf = (perfil(x) for x in ("WIN", "IND", "WDO", "DOL", "PETR4", "BOVA11"))
    ticks = serie(win, n_ticks, passo_ms, semente)
    d.adicionar_simbolo(especificacao(win, "WINZ26", _ts_venc(2026, 12, 16)), ticks,
                        gerar_livros_sinteticos(ticks, 10, 5, win.tick_pontos, semente=semente))
    d.adicionar_simbolo(especificacao(win, "WING27", _ts_venc(2027, 2, 17)), serie(win, 50, 1000, semente + 1))
    d.adicionar_simbolo(especificacao(ind, "INDZ26"), serie(ind, 50, 1000, semente + 2))
    d.adicionar_simbolo(especificacao(wdo, "WDOX26"), serie(wdo, 50, 1000, semente + 3))
    d.adicionar_simbolo(especificacao(dol, "DOLX26"), serie(dol, 50, 1000, semente + 5))
    d.adicionar_simbolo(especificacao(acao, "PETR4"), serie(acao, 50, 1000, semente + 6))
    d.adicionar_simbolo(especificacao(etf, "BOVA11"), serie(etf, 50, 1000, semente + 4))
    return d
