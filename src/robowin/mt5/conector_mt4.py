"""Conector MT4: cliente da ponte (EA mql4/RobowinBridge.mq4) que implementa o Protocolo `TerminalMT5`.

O MT4 NAO tem pacote Python oficial. Este conector fala com o Expert Advisor por ZeroMQ REQ/REP em 127.0.0.1
(protocolo em mql4/PROTOCOLO.md e mt5/protocolo_mt4.py) e devolve os MESMOS objetos/atributos do `ConectorMT5`,
para o gateway, o `ExecucaoDemo` e o guardiao nao saberem a diferenca. Diferencas tratadas aqui:

- Conta HEDGING: `margin_mode` = RETAIL_HEDGING; posicoes sao identificadas por TICKET.
- Saida SEMPRE por ticket: uma requisicao de negocio com `position` vira `ordem_fechar(ticket)` no EA; ela
  NUNCA abre ordem oposta. Sem `position`, e uma abertura.
- Sem livro (`market_book_*` indisponiveis) e sem historico de ticks (`copy_ticks_range` => None; `copy_ticks_from`
  devolve so os ticks que o EA viu ao vivo, em ordem, com `seq` e `gtc`).
- Tempo em SEGUNDOS: `time_msc = time * 1000` (sem inventar milissegundos); a ordem real dos ticks do mesmo segundo
  vem de `seq`; `gtc` (GetTickCount do EA, ms) fica disponivel para medir intervalos.
- `order_check` e SIMULADO localmente (volume, stops, margem, permissoes) porque o MT4 nao tem OrderCheck.
- Historico: ordens fechadas (e posicoes abertas) viram `deals` de entrada (IN) e saida (OUT) com `reason` derivado.

Comandos de ordem NUNCA sao reenviados: sem resposta no prazo o retorno e `None` (ordem INCERTA, reconciliar).
Leituras tentam de novo uma vez com o socket recriado. `order_send` existe so como METODO (assim o contrato do
Protocolo e cumprido); o unico CHAMADOR continua sendo mt5/execucao_demo.py (teste estatico por AST).
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import itertools
import os
import threading
import time
import uuid
from types import SimpleNamespace as NS
from typing import Any, Protocol

import numpy as np

from . import constantes as C
from . import protocolo_mt4 as P
from .capacidades import CAPACIDADES_MT4, CapacidadesTerminal


class MT4IndisponivelErro(RuntimeError):
    """A ponte do MT4 nao respondeu (EA nao carregado, porta errada ou AutoTrading/DLL desligados)."""


class Transporte(Protocol):
    def requisitar(self, dados: bytes) -> bytes | None: ...
    def fechar(self) -> None: ...


class TransporteZMQ:
    """ZeroMQ REQ com timeout curto e reconexao (padrao 'Lazy Pirate': timeout => recria o socket)."""

    def __init__(self, endereco: str = "tcp://127.0.0.1:5560", timeout_ms: int = 1500) -> None:
        import zmq
        self._zmq = zmq
        self.endereco, self.timeout_ms = endereco, int(timeout_ms)
        self._ctx = zmq.Context()
        self._sock: Any = None
        self.reconexoes = 0

    def _abrir(self) -> Any:
        z = self._zmq
        s = self._ctx.socket(z.REQ)
        s.setsockopt(z.LINGER, 0)
        s.setsockopt(z.RCVTIMEO, self.timeout_ms)
        s.setsockopt(z.SNDTIMEO, self.timeout_ms)
        s.connect(self.endereco)
        return s

    def _descartar(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close(0)
            except Exception:  # noqa: BLE001
                pass
            self._sock = None

    def requisitar(self, dados: bytes) -> bytes | None:
        try:
            if self._sock is None:
                self._sock = self._abrir()
                self.reconexoes += 1
            self._sock.send(dados)
            return bytes(self._sock.recv())
        except self._zmq.ZMQError:
            self._descartar()  # o REQ fica preso apos timeout: recria no proximo pedido
            return None

    def fechar(self) -> None:
        self._descartar()
        self._ctx.term()


class TransporteLocal:
    """Sem rede: entrega os bytes direto ao servidor (DubleMT4) e devolve os bytes da resposta (mesmo JSON)."""

    def __init__(self, servidor: Any) -> None:
        self.servidor = servidor
        self.fora_do_ar = False

    def requisitar(self, dados: bytes) -> bytes | None:
        if self.fora_do_ar:
            return None
        try:
            return bytes(self.servidor.processar_bytes(dados))
        except TimeoutError:  # o servidor falso "nao respondeu"
            return None

    def fechar(self) -> None:
        return None


class _Resp:
    __slots__ = ("ok", "dados", "codigo", "msg")

    def __init__(self, ok: bool, dados: dict[str, Any], codigo: int = 0, msg: str = "") -> None:
        self.ok, self.dados, self.codigo, self.msg = ok, dados, codigo, msg


def _epoch_s(x: Any) -> int:
    if isinstance(x, dt.datetime):
        if x.tzinfo is None:
            x = x.replace(tzinfo=dt.UTC)  # a API trata a hora de parede do servidor como UTC
        return int(x.timestamp())
    return int(float(x))


# MODE_PROFITCALCMODE do MT4 (0 forex, 1 CFD, 2 futuros) -> SYMBOL_CALC_MODE do MT5 (0 forex, 2 CFD, 1 futuros)
CALC_MT4_PARA_MT5 = {0: 0, 1: 2, 2: 1}
# MODE_SWAPTYPE do MT4 (0 pontos, 1 moeda base, 2 juros, 3 moeda de margem) -> SYMBOL_SWAP_MODE do MT5
SWAP_MT4_PARA_MT5 = {0: 1, 1: 2, 2: 5, 3: 3}

_DTYPE_TICK = np.dtype([
    ("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"), ("volume", "<u8"),
    ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8"), ("seq", "<i8"), ("gtc", "<i8"),
])


def razao_do_deal_saida(item: dict[str, Any]) -> int:
    """DEAL_REASON da SAIDA de uma ordem fechada do historico do MT4.

    [sl]/[tp]/so: no comentario => SL/TP/SO. Senao: fechada pelo EA => EXPERT; sem informacao (EA reiniciou,
    `fechado_ea == -1`) e com magic => EXPERT (nao ha como saber); fechada fora da ponte => manual (CLIENT)."""
    com = str(item.get("comentario", "")).lower()
    if "[sl]" in com:
        return C.RAZAO_SL
    if "[tp]" in com:
        return C.RAZAO_TP
    if com.startswith("so:"):
        return C.RAZAO_SO
    magic = int(item.get("magic", 0))
    fechado_ea = int(item.get("fechado_ea", -1))
    if fechado_ea == 1 or (fechado_ea == -1 and magic != 0):
        return C.RAZAO_EXPERT
    return C.RAZAO_CLIENT


class ConectorMT4:
    """Terminal MT4 via ponte EA. Implementa o Protocolo `TerminalMT5` (ver docstring do modulo)."""

    plataforma = "mt4"
    capacidades: CapacidadesTerminal = CAPACIDADES_MT4
    suporta_backfill = False  # sem historico de ticks: lacuna so BLOQUEIA entradas

    def __init__(self, endereco: str = "tcp://127.0.0.1:5560", timeout_ms: int = 1500,
                 transporte: Transporte | None = None, ttl_spec_s: float = 5, token: str | None = None) -> None:
        self.endereco = endereco
        # token compartilhado com o input Token do EA (opcional): [mt5].mt4_token ou ROBOWIN_MT4_TOKEN
        self._token = (os.environ.get("ROBOWIN_MT4_TOKEN", "") if token is None else token).strip()
        self._tr: Transporte = transporte if transporte is not None else TransporteZMQ(endereco, timeout_ms)
        self._ttl_spec_s = ttl_spec_s
        self._pref = uuid.uuid4().hex[:8]
        self._n = itertools.count(1)
        self._lock = threading.RLock()
        self._ultimo_erro: tuple[int, str] = C.ERR_OK
        self._ola: dict[str, Any] = {}
        self._specs: dict[str, tuple[float, NS]] = {}
        self._cursor: dict[str, tuple[int, int]] = {}  # simbolo -> (epoca do EA, ultimo seq entregue)
        self.lacunas_ticks = 0  # perdas REAIS de ticks (buffer do EA estourou ou EA reiniciou)
        self.falhas_transporte = 0

    def __repr__(self) -> str:
        return f"ConectorMT4({self.endereco!r})"

    @property
    def eh_duble(self) -> bool:
        return bool(self._ola.get("duble", False))

    @property
    def ola(self) -> dict[str, Any]:
        """Copia do handshake do EA (versao do protocolo, nome/versao do EA, build, caminho); vazio antes do initialize."""
        return dict(self._ola)

    # ------------------------------------------------------------------------------------------
    # transporte
    # ------------------------------------------------------------------------------------------
    def _rpc(self, cmd: str, args: dict[str, Any] | None = None) -> _Resp | None:
        """Uma chamada. Leituras tentam 2x (socket recriado); ordens NUNCA sao reenviadas. None = sem resposta."""
        tentativas = 2 if cmd in P.COMANDOS_LEITURA else 1
        with self._lock:
            for _ in range(tentativas):
                id_ = f"{self._pref}-{next(self._n)}"
                req = P.requisicao(id_, cmd, args)
                if self._token:
                    req["tk"] = self._token
                bruto = self._tr.requisitar(P.codificar(req))
                if bruto is None:
                    self.falhas_transporte += 1
                    self._ultimo_erro = C.ERR_SEM_CONEXAO
                    continue
                try:
                    msg = P.decodificar(bruto)
                except P.ErroProtocolo as e:
                    self._ultimo_erro = (-10002, f"resposta invalida da ponte: {e}")
                    continue
                if msg.get("id") != id_ or msg.get("v") != P.VERSAO_PROTOCOLO:
                    self._ultimo_erro = (-10002, "resposta fora de sincronia ou de outra versao do protocolo")
                    continue
                if msg.get("ok") is True:
                    return _Resp(True, dict(msg.get("dados") or {}))
                er = msg.get("erro") or {}
                return _Resp(False, {}, int(er.get("codigo", P.ERRO_INTERNO)), str(er.get("msg", "")))
        return None

    def _dados(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any] | None:
        r = self._rpc(cmd, args)
        if r is None:
            return None
        if not r.ok:
            self._ultimo_erro = (-r.codigo, r.msg)
            return None
        return r.dados

    # ------------------------------------------------------------------------------------------
    # ciclo de vida
    # ------------------------------------------------------------------------------------------
    def initialize(self, **kw: Any) -> bool:
        d = self._dados(P.CMD_OLA, {"versao_cliente": P.VERSAO_PROTOCOLO})
        if d is None:
            return False
        if int(d.get("versao_protocolo", -1)) != P.VERSAO_PROTOCOLO:
            self._ultimo_erro = (-10003, f"versao do protocolo do EA ({d.get('versao_protocolo')}) != {P.VERSAO_PROTOCOLO}")
            return False
        self._ola = d
        self._ultimo_erro = C.ERR_OK
        return True

    def shutdown(self) -> None:
        self._tr.fechar()

    def last_error(self) -> tuple[int, str]:
        return self._ultimo_erro

    def limpar_cache(self) -> None:
        self._specs.clear()

    # ------------------------------------------------------------------------------------------
    # conta e terminal
    # ------------------------------------------------------------------------------------------
    def _conta(self) -> dict[str, Any] | None:
        return self._dados(P.CMD_CONTA)

    def account_info(self) -> Any:
        d = self._conta()
        if d is None:
            return None
        # MT4: so ha demo/real (IsDemo). Qualquer coisa que nao seja demo e tratada como REAL (falha segura).
        modo = C.CONTA_DEMO if d.get("demo") is True else C.CONTA_REAL
        return NS(
            login=int(d["login"]), server=str(d["servidor"]), trade_mode=modo, margin_mode=C.MARGEM_HEDGING,
            trade_allowed=bool(d.get("trade_allowed", False)), trade_expert=bool(d.get("expert_enabled", False)),
            leverage=int(d.get("alavancagem", 0)), currency=str(d.get("moeda", "")),
            balance=float(d.get("saldo", 0.0)), equity=float(d.get("patrimonio", 0.0)),
            profit=float(d.get("lucro", 0.0)), margin=float(d.get("margem", 0.0)),
            margin_free=float(d.get("margem_livre", 0.0)), name=str(d.get("nome", "")),
            company=str(d.get("empresa", "")), limit_orders=0,
        )

    def terminal_info(self) -> Any:
        d = self._conta()
        if d is None:
            return None
        return NS(
            connected=bool(d.get("conectado", False)),
            trade_allowed=bool(d.get("expert_enabled", False)) and bool(d.get("conectado", False)),
            tradeapi_disabled=False, community_account=False, dlls_allowed=bool(d.get("dlls_permitidas", False)),
            path=str(self._ola.get("caminho", "")), data_path=str(self._ola.get("caminho_dados", "")),
            name="MetaTrader 4 (ponte EA)", company=str(d.get("empresa", "")), build=int(self._ola.get("build", 0)),
            ping_last=0,
        )

    # ------------------------------------------------------------------------------------------
    # simbolos
    # ------------------------------------------------------------------------------------------
    def _ns_spec(self, s: dict[str, Any]) -> NS:
        return NS(
            name=str(s["nome"]), path=str(s.get("caminho", "")), description=str(s.get("descricao", "")),
            digits=int(s.get("digits", 0)), point=float(s.get("ponto", 1.0)), visible=bool(s.get("visivel", True)),
            select=bool(s.get("visivel", True)), spread=int(s.get("spread", 0)), spread_float=True,
            trade_mode=4 if s.get("negociavel", True) else 0, trade_exemode=int(s.get("execucao", C.EXEC_MARKET)),
            filling_mode=0, trade_tick_size=float(s["tick_size"]), trade_tick_value=float(s["tick_value"]),
            trade_contract_size=float(s.get("lot_size", 0.0)), volume_min=float(s["lote_min"]),
            volume_max=float(s["lote_max"]), volume_step=float(s["lote_passo"]), ticks_bookdepth=0,
            expiration_time=int(s.get("expiracao_s", 0)), currency_profit=str(s.get("moeda_lucro", "")),
            currency_base=str(s.get("moeda_base", "")), stop_level=int(s.get("stop_level", 0)),
            freeze_level=int(s.get("freeze_level", 0)), margin_required=float(s.get("margem_requerida", 0.0)),
            # qualquer mercado: campos do MT4 traduzidos para os nomes do MT5 (ausentes no EA antigo => None)
            currency_margin=str(s.get("moeda_margem", "")),
            trade_calc_mode=CALC_MT4_PARA_MT5.get(int(s["modo_lucro"])) if "modo_lucro" in s else None,
            swap_long=float(s["swap_long"]) if "swap_long" in s else None,
            swap_short=float(s["swap_short"]) if "swap_short" in s else None,
            swap_mode=SWAP_MT4_PARA_MT5.get(int(s["swap_tipo"])) if "swap_tipo" in s else None,
            swap_rollover3days=int(s["swap_triplo"]) if "swap_triplo" in s else None,
        )

    def symbols_get(self, group: str | None = None) -> Any:
        d = self._dados(P.CMD_SIMBOLOS)
        if d is None:
            return None
        nomes = [str(n) for n in d.get("simbolos", [])]
        if group:
            pads = [p.strip() for p in group.split(",") if p.strip()]
            inc = [p for p in pads if not p.startswith("!")]
            exc = [p[1:] for p in pads if p.startswith("!")]
            nomes = [n for n in nomes if (not inc or any(fnmatch.fnmatchcase(n, p) for p in inc))
                     and not any(fnmatch.fnmatchcase(n, p) for p in exc)]
        return tuple(NS(name=n) for n in nomes)  # leve: so o nome (detalhes via symbol_info, 1 chamada por simbolo)

    def symbol_info(self, symbol: str) -> Any:
        agora = time.monotonic()
        hit = self._specs.get(symbol)
        if hit is not None and agora - hit[0] < self._ttl_spec_s:
            return NS(**vars(hit[1]))
        d = self._dados(P.CMD_SIMBOLO, {"simbolo": symbol})
        if d is None:
            return None
        ns = self._ns_spec(d)
        self._specs[symbol] = (agora, ns)
        return NS(**vars(ns))

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        d = self._dados(P.CMD_SELECIONAR, {"simbolo": symbol, "ativar": bool(enable)})
        if d is not None:
            self._specs.pop(symbol, None)
        return bool(d and d.get("ok", True))

    def symbol_info_tick(self, symbol: str) -> Any:
        d = self._dados(P.CMD_TICK, {"simbolo": symbol})
        if d is None or not d.get("t_s"):
            return None
        t_s = int(d["t_s"])
        return NS(time=t_s, bid=float(d["bid"]), ask=float(d["ask"]), last=0.0, volume=0, time_msc=t_s * 1000,
                  flags=C.FLAG_BID | C.FLAG_ASK, volume_real=0.0, seq=int(d.get("seq", 0)), gtc=int(d.get("gtc", 0)))

    # ------------------------------------------------------------------------------------------
    # dados de mercado (sem historico de ticks e sem livro)
    # ------------------------------------------------------------------------------------------
    def copy_ticks_from(self, symbol: str, date_from: Any, count: int, flags: int) -> Any:
        """Ticks que o EA viu ao vivo, em ordem, a partir do cursor interno (a data so descarta ticks antigos).

        None => sem resposta. Perda real (buffer do EA estourou/EA reiniciou) incrementa `lacunas_ticks`."""
        cur = self._cursor.get(symbol)
        d = self._dados(P.CMD_TICKS, {"simbolo": symbol, "desde_seq": -1 if cur is None else cur[1],
                                      "epoca": 0 if cur is None else cur[0], "max": int(count)})
        if d is None:
            return None
        if d.get("perdeu") or d.get("reiniciou"):
            self.lacunas_ticks += 1
        corte_s = _epoch_s(date_from)
        linhas = [r for r in d.get("ticks", []) if int(r[1]) >= corte_s]  # [seq, t_s, gtc, bid, ask, vol_tick]
        todos = d.get("ticks", [])
        if todos:
            self._cursor[symbol] = (int(d["epoca"]), int(todos[-1][0]))
        elif cur is None:
            self._cursor[symbol] = (int(d["epoca"]), int(d.get("seq_max", 0)))
        arr = np.zeros(len(linhas), dtype=_DTYPE_TICK)
        for k, (seq, t_s, gtc, bid, ask, vol) in enumerate(linhas):
            arr[k] = (int(t_s), float(bid), float(ask), 0.0, int(vol), int(t_s) * 1000, C.FLAG_BID | C.FLAG_ASK,
                      float(vol), int(seq), int(gtc))
        return arr

    def copy_ticks_range(self, symbol: str, date_from: Any, date_to: Any, flags: int) -> Any:
        return None  # o MT4 nao tem historico de ticks (capacidade `historico_ticks` ausente)

    def market_book_add(self, symbol: str) -> bool:
        return False  # sem livro

    def market_book_get(self, symbol: str) -> Any:
        return None

    def market_book_release(self, symbol: str) -> bool:
        return True

    # ------------------------------------------------------------------------------------------
    # posicoes, ordens pendentes e historico
    # ------------------------------------------------------------------------------------------
    def _ns_posicao(self, p: dict[str, Any]) -> NS:
        t_s = int(p["t_abertura_s"])
        return NS(ticket=int(p["ticket"]), time=t_s, time_msc=t_s * 1000, time_update=t_s, time_update_msc=t_s * 1000,
                  type=int(p["tipo"]), magic=int(p.get("magic", 0)), identifier=int(p["ticket"]),
                  reason=C.RAZAO_EXPERT if int(p.get("magic", 0)) else C.RAZAO_CLIENT, volume=float(p["volume"]),
                  price_open=float(p["p_abertura"]), sl=float(p.get("sl", 0.0)), tp=float(p.get("tp", 0.0)),
                  price_current=float(p.get("p_atual", p["p_abertura"])), swap=float(p.get("swap", 0.0)),
                  profit=float(p.get("lucro", 0.0)), commission=float(p.get("comissao", 0.0)),
                  symbol=str(p["simbolo"]), comment=str(p.get("comentario", "")))

    def positions_get(self, **kw: Any) -> Any:
        d = self._dados(P.CMD_POSICOES, {"simbolo": kw.get("symbol") or ""})
        if d is None:
            return None
        ps = [self._ns_posicao(p) for p in d.get("posicoes", [])]
        if kw.get("group"):
            ps = [p for p in ps if fnmatch.fnmatchcase(p.symbol, kw["group"])]
        if kw.get("ticket"):
            ps = [p for p in ps if p.ticket == kw["ticket"]]
        return tuple(ps)

    def orders_get(self, **kw: Any) -> Any:
        d = self._dados(P.CMD_PENDENTES, {"simbolo": kw.get("symbol") or ""})
        if d is None:
            return None
        out = []
        for o in d.get("ordens", []):
            t_s = int(o["t_abertura_s"])
            out.append(NS(ticket=int(o["ticket"]), time_setup=t_s, time_setup_msc=t_s * 1000, type=int(o["tipo"]),
                          state=1, magic=int(o.get("magic", 0)), position_id=0, symbol=str(o["simbolo"]),
                          volume_initial=float(o["volume"]), volume_current=float(o["volume"]),
                          price_open=float(o["p_abertura"]), sl=float(o.get("sl", 0.0)), tp=float(o.get("tp", 0.0)),
                          type_filling=0, reason=C.RAZAO_EXPERT if int(o.get("magic", 0)) else C.RAZAO_CLIENT))
        if kw.get("ticket"):
            out = [o for o in out if o.ticket == kw["ticket"]]
        return tuple(out)

    def _historico(self, a: tuple[Any, ...]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
        de_s = _epoch_s(a[0]) if len(a) >= 1 else 0
        ate_s = _epoch_s(a[1]) if len(a) >= 2 else 2_000_000_000
        d = self._dados(P.CMD_HISTORICO, {"de_s": de_s, "ate_s": ate_s})
        pos = self._dados(P.CMD_POSICOES, {"simbolo": ""})
        if d is None or pos is None:
            return None
        return list(d.get("itens", [])), list(pos.get("posicoes", []))

    def history_deals_get(self, *a: Any, **kw: Any) -> Any:
        """Deals sinteticos: IN (abertura) e OUT (fechamento) das ordens fechadas + IN das posicoes abertas.

        Ids: ticket*2 (IN) e ticket*2+1 (OUT). Comissao na entrada; lucro e swap na saida."""
        h = self._historico(a)
        if h is None:
            return None
        itens, abertas = h
        de_s = _epoch_s(a[0]) if len(a) >= 1 else 0
        ate_s = _epoch_s(a[1]) if len(a) >= 2 else 2_000_000_000
        deals: list[NS] = []

        def deal(tid: int, t_s: int, tipo: int, entrada: int, razao: int, it: dict[str, Any], preco: float,
                 profit: float, comissao: float, swap: float) -> NS:
            return NS(ticket=tid, order=int(it["ticket"]), time=t_s, time_msc=t_s * 1000, type=tipo, entry=entrada,
                      magic=int(it.get("magic", 0)), position_id=int(it["ticket"]), reason=razao,
                      volume=float(it["volume"]), price=preco, commission=comissao, swap=swap, profit=profit, fee=0.0,
                      symbol=str(it["simbolo"]), comment=str(it.get("comentario", "")), external_id="")

        for it in itens:
            tipo = int(it["tipo"])
            if tipo not in (C.DEAL_COMPRA, C.DEAL_VENDA):
                continue  # pendentes canceladas e saldo/credito nao sao negocios
            t_ab, t_fe = int(it["t_abertura_s"]), int(it["t_fechamento_s"])
            tk = int(it["ticket"])
            razao_in = C.RAZAO_EXPERT if int(it.get("magic", 0)) else C.RAZAO_CLIENT
            if de_s <= t_ab <= ate_s:
                deals.append(deal(tk * 2, t_ab, tipo, C.ENTRADA_IN, razao_in, it, float(it["p_abertura"]), 0.0,
                                  float(it.get("comissao", 0.0)), 0.0))
            if de_s <= t_fe <= ate_s:
                deals.append(deal(tk * 2 + 1, t_fe, 1 - tipo, C.ENTRADA_OUT, razao_do_deal_saida(it), it,
                                  float(it["p_fechamento"]), float(it.get("lucro", 0.0)), 0.0,
                                  float(it.get("swap", 0.0))))
        for p in abertas:
            t_ab = int(p["t_abertura_s"])
            if de_s <= t_ab <= ate_s:
                deals.append(deal(int(p["ticket"]) * 2, t_ab, int(p["tipo"]), C.ENTRADA_IN,
                                  C.RAZAO_EXPERT if int(p.get("magic", 0)) else C.RAZAO_CLIENT, p,
                                  float(p["p_abertura"]), 0.0, float(p.get("comissao", 0.0)), 0.0))
        deals.sort(key=lambda x: (x.time_msc, x.ticket))
        if kw.get("ticket"):
            deals = [x for x in deals if x.ticket == kw["ticket"]]
        if kw.get("position"):
            deals = [x for x in deals if x.position_id == kw["position"]]
        return tuple(deals)

    def history_orders_get(self, *a: Any, **kw: Any) -> Any:
        h = self._historico(a)
        if h is None:
            return None
        out = []
        for it in h[0]:
            t_ab, t_fe = int(it["t_abertura_s"]), int(it["t_fechamento_s"])
            out.append(NS(ticket=int(it["ticket"]), time_setup=t_ab, time_setup_msc=t_ab * 1000, time_done=t_fe,
                          time_done_msc=t_fe * 1000, time_msc=t_fe * 1000, type=int(it["tipo"]), state=4,
                          magic=int(it.get("magic", 0)), position_id=int(it["ticket"]), symbol=str(it["simbolo"]),
                          volume_initial=float(it["volume"]), volume_current=0.0, price_open=float(it["p_abertura"]),
                          sl=float(it.get("sl", 0.0)), tp=float(it.get("tp", 0.0)),
                          reason=C.RAZAO_EXPERT if int(it.get("magic", 0)) else C.RAZAO_CLIENT))
        if kw.get("ticket"):
            out = [o for o in out if o.ticket == kw["ticket"]]
        return tuple(out)

    # ------------------------------------------------------------------------------------------
    # order_check SIMULADO (o MT4 nao tem OrderCheck): regras do EA aplicadas localmente
    # ------------------------------------------------------------------------------------------
    def order_check(self, request: dict[str, Any]) -> Any:
        res = self._checar(request)
        if res is None:
            return None
        ret, com, saldo = res
        return NS(retcode=ret, comment=com or "Done", balance=saldo, request=NS(**request))

    def _checar(self, req: dict[str, Any]) -> tuple[int, str, float] | None:
        conta = self._conta()
        if conta is None:
            return None
        saldo = float(conta.get("saldo", 0.0))
        if not conta.get("expert_enabled", False):
            return C.RET_ALGO_DESLIGADO_CLIENTE, "AutoTrading desligado no terminal", saldo
        if not conta.get("trade_allowed", False):
            return C.RET_TRADE_DESABILITADO, "negociacao nao permitida (conta/EA/contexto)", saldo
        if not conta.get("demo", False):
            return C.RET_TRADE_DESABILITADO, "conta nao e DEMO", saldo
        acao, sym = req.get("action"), str(req.get("symbol", ""))
        if acao in (C.ACAO_DEAL, C.ACAO_PENDING):
            return self._checar_ordem(req, sym, conta, saldo)
        if acao in (C.ACAO_SLTP, C.ACAO_REMOVE, C.ACAO_MODIFY):
            return self._checar_existente(req, sym, saldo)
        return C.RET_INVALIDO, "acao desconhecida", saldo

    def _checar_existente(self, req: dict[str, Any], sym: str, saldo: float) -> tuple[int, str, float] | None:
        acao = req["action"]
        if acao == C.ACAO_SLTP:
            ps = self.positions_get(symbol=sym)
            if ps is None:
                return None
            if req.get("position") not in {p.ticket for p in ps}:
                return C.RET_POSICAO_FECHADA, "posicao inexistente", saldo
        else:
            os_ = self.orders_get(symbol=sym)
            if os_ is None:
                return None
            if req.get("order") not in {o.ticket for o in os_}:
                return C.RET_INVALIDO, "ordem inexistente", saldo
        return 0, "", saldo

    def _checar_ordem(self, req: dict[str, Any], sym: str, conta: dict[str, Any],
                      saldo: float) -> tuple[int, str, float] | None:
        info = self.symbol_info(sym)
        if info is None:
            return C.RET_INVALIDO, "simbolo desconhecido", saldo
        if info.trade_mode == 0:
            return C.RET_MERCADO_FECHADO, "negociacao do simbolo desabilitada", saldo
        vol = float(req.get("volume", 0))
        if vol < info.volume_min or vol > info.volume_max or abs(vol / info.volume_step - round(vol / info.volume_step)) > 1e-9:
            return C.RET_VOLUME_INVALIDO, "volume invalido", saldo
        tk = self.symbol_info_tick(sym)
        if tk is None:
            return C.RET_MERCADO_FECHADO, "sem cotacao", saldo
        tipo, acao = req.get("type"), req["action"]
        nivel = info.stop_level * info.point
        sl, tp = float(req.get("sl", 0) or 0), float(req.get("tp", 0) or 0)
        if acao == C.ACAO_DEAL:
            if tipo not in (C.ORDEM_BUY, C.ORDEM_SELL):
                return C.RET_INVALIDO, "tipo invalido para negocio a mercado", saldo
            if "position" in req:  # saida por ticket: confere ticket, sentido oposto e volume
                ps = self.positions_get(symbol=sym)
                if ps is None:
                    return None
                p = next((x for x in ps if x.ticket == req.get("position")), None)
                if p is None:
                    return C.RET_POSICAO_FECHADA, "posicao ja encerrada (ticket velho)", saldo
                if (p.type == C.POS_COMPRA) == (tipo == C.ORDEM_BUY) or vol > p.volume + 1e-9:
                    return C.RET_INVALIDO, "saida incompativel com a posicao", saldo
                return 0, "", saldo
            if tipo == C.ORDEM_BUY and ((sl and sl > tk.bid - nivel + 1e-9) or (tp and tp < tk.bid + nivel - 1e-9)):
                return C.RET_STOPS_INVALIDOS, "stops invalidos (nivel minimo de stop)", saldo
            if tipo == C.ORDEM_SELL and ((sl and sl < tk.ask + nivel - 1e-9) or (tp and tp > tk.ask - nivel + 1e-9)):
                return C.RET_STOPS_INVALIDOS, "stops invalidos (nivel minimo de stop)", saldo
        else:
            preco = float(req.get("price", 0))
            if tipo == C.ORDEM_BUY_LIMIT and preco > tk.ask - nivel + 1e-9:
                return C.RET_PRECO_INVALIDO, "compra limitada deve ficar abaixo do ask (nivel de stop)", saldo
            if tipo == C.ORDEM_SELL_LIMIT and preco < tk.bid + nivel - 1e-9:
                return C.RET_PRECO_INVALIDO, "venda limitada deve ficar acima do bid (nivel de stop)", saldo
            if tipo not in (C.ORDEM_BUY_LIMIT, C.ORDEM_SELL_LIMIT):
                return C.RET_INVALIDO, "tipo pendente nao suportado", saldo
        exigida = info.margin_required * vol
        if exigida > 0 and exigida > float(conta.get("margem_livre", 0.0)):
            return C.RET_SEM_DINHEIRO, "margem livre insuficiente", saldo
        return 0, "", saldo

    # ------------------------------------------------------------------------------------------
    # envio (METODO do Protocolo; o unico CHAMADOR do projeto e mt5/execucao_demo.py)
    # ------------------------------------------------------------------------------------------
    def _resultado(self, ret: int, req: dict[str, Any], com: str = "", deal: int = 0, order: int = 0,
                   vol: float = 0.0, preco: float = 0.0) -> NS:
        return NS(retcode=ret, deal=deal, order=order, volume=vol, price=preco, bid=0.0, ask=0.0, comment=com,
                  request_id=next(self._n), retcode_external=0, request=NS(**req))

    def _resultado_erro(self, r: _Resp, req: dict[str, Any]) -> NS:
        return self._resultado(P.retcode_de_erro(r.codigo), req, f"[mt4 {r.codigo}] {r.msg}")

    def order_send(self, request: dict[str, Any]) -> Any:
        acao = request.get("action")
        if acao == C.ACAO_DEAL:
            if "position" in request:
                return self._fechar_por_ticket(request)
            return self._abrir(request, pendente=False)
        if acao == C.ACAO_PENDING:
            return self._abrir(request, pendente=True)
        if acao == C.ACAO_SLTP:
            return self._modificar(request, int(request.get("position", 0) or 0))
        if acao == C.ACAO_MODIFY:
            return self._modificar(request, int(request.get("order", 0) or 0))
        if acao == C.ACAO_REMOVE:
            ticket = int(request.get("order", 0) or 0)
            r = self._rpc(P.CMD_ORDEM_APAGAR, {"ticket": ticket})
            if r is None:
                return None
            if not r.ok:
                return self._resultado_erro(r, request)
            return self._resultado(C.RET_EXECUTADA, request, "Request executed", 0, ticket)
        return self._resultado(C.RET_INVALIDO, request, "acao desconhecida")

    def _abrir(self, req: dict[str, Any], pendente: bool) -> Any:
        args = {
            "simbolo": str(req.get("symbol", "")), "tipo": int(req["type"]), "volume": float(req["volume"]),
            "preco": float(req.get("price", 0.0) or 0.0), "desvio": int(req.get("deviation", 0) or 0),
            "sl": float(req.get("sl", 0.0) or 0.0), "tp": float(req.get("tp", 0.0) or 0.0),
            "magic": int(req.get("magic", 0)), "comentario": str(req.get("comment", ""))[:31],
            "validade": "dia" if req.get("type_time") == C.VALIDADE_DIA else "gtc",
        }
        r = self._rpc(P.CMD_ORDEM_ABRIR, args)
        if r is None:
            return None  # INCERTA: nunca reenviar
        if not r.ok:
            return self._resultado_erro(r, req)
        tk = int(r.dados["ticket"])
        preco = float(r.dados.get("preco", 0.0))
        if pendente:
            return self._resultado(C.RET_COLOCADA, req, "Request placed", 0, tk, float(req["volume"]), preco)
        return self._resultado(C.RET_EXECUTADA, req, "Request executed", tk, tk, float(req["volume"]), preco)

    def _fechar_por_ticket(self, req: dict[str, Any]) -> Any:
        """Saida: `ordem_fechar(ticket)`. NUNCA vira ordem oposta (que em conta HEDGING abriria outra posicao)."""
        ticket = int(req.get("position") or 0)
        if ticket <= 0:
            return self._resultado(C.RET_INVALIDO, req, "saida sem ticket valido: nada enviado")
        r = self._rpc(P.CMD_ORDEM_FECHAR, {"ticket": ticket, "volume": float(req["volume"]),
                                           "desvio": int(req.get("deviation", 0) or 0)})
        if r is None:
            return None
        if not r.ok:
            return self._resultado_erro(r, req)
        vol = float(r.dados.get("volume_fechado", req["volume"]))
        ret = C.RET_EXECUTADA if vol >= float(req["volume"]) - 1e-9 else C.RET_PARCIAL
        return self._resultado(ret, req, "Request executed", ticket, ticket, vol, float(r.dados.get("preco", 0.0)))

    def _modificar(self, req: dict[str, Any], ticket: int) -> Any:
        args = {"ticket": ticket, "sl": float(req.get("sl", 0.0) or 0.0), "tp": float(req.get("tp", 0.0) or 0.0),
                "preco": float(req.get("price", 0.0) or 0.0)}
        r = self._rpc(P.CMD_ORDEM_MODIFICAR, args)
        if r is None:
            return None
        if not r.ok:
            return self._resultado_erro(r, req)
        return self._resultado(C.RET_EXECUTADA, req, "Request executed", 0, ticket)
