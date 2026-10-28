"""DUBLE DE TESTES do MT4 (`DubleMT4`): servidor FALSO da ponte, que fala o MESMO protocolo do EA.

*** ISTO NAO E O MERCADO NEM O TERMINAL REAL. Dados 100% SINTETICOS. `eh_duble = True` rotula o objeto. ***
Nao substitui o teste do .mq4 real (que NAO pode ser compilado nem testado neste projeto): ele existe para exercitar
o lado Python (`ConectorMT4`, gateway, guardiao, execucao) contra um servidor que obedece mql4/PROTOCOLO.md.

Fiel ao MT4:
- conta HEDGING: ordem oposta abre OUTRA posicao; cada posicao tem TICKET; fechamento so por ticket;
  fechamento parcial encerra o ticket e abre o restante com NOVO ticket ("from #...");
- SEM livro e SEM historico de ticks: so um buffer circular (anel) de ticks ao vivo com `seq`, `epoca`, `perdeu`;
- tempo em SEGUNDOS (`t_s`) + `gtc` (GetTickCount em ms, local); SL/TP vivem no "servidor" e fecham com "[sl]"/"[tp]";
- ordens so em conta DEMO (EA recusa conta real, erro 1006), erros GetLastError() e falhas injetadas
  (`Falha4`), inclusive "executou mas a resposta se perdeu" (`executa=True` + sem resposta);
- idempotencia por `id` nos comandos de ordem (cache das ultimas respostas), como o EA.

Dois modos de uso: `processar(msg)`/`processar_bytes(b)` (sem rede, via `TransporteLocal`) e `ServidorDubleMT4`
(thread com ZeroMQ REP real em 127.0.0.1, falando com `TransporteZMQ`).
"""
from __future__ import annotations

import bisect
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from robowin.core.relogio import Relogio, RelogioReal

from . import constantes as C
from . import protocolo_mt4 as P
from .duble import DubleMT5, TickSint, criar_duble_padrao

_CACHE_IDEM = 64


@dataclass
class Falha4:
    """Falha injetada para o proximo comando de ORDEM.

    codigo None => sem resposta (o cliente estoura o prazo). `executa=True` => a ordem EXECUTA no servidor mas a
    resposta se perde (caso classico de ordem INCERTA). codigo int => erro MQL4 (ex.: 138 requote, 6 sem conexao).
    """
    codigo: int | None
    executa: bool = False


class _Conta:
    def __init__(self, login: int, servidor: str, demo: bool) -> None:
        self.login, self.servidor, self.demo = login, servidor, demo
        self.saldo = 100_000.0
        self.expert = True
        self.trade_allowed = True
        self.somente_leitura = False


class DubleMT4:
    """Servidor falso da ponte do MT4 (SINTETICO; ver docstring do modulo)."""

    eh_duble = True

    def __init__(self, relogio: Relogio | None = None, *, login: int = 900001, servidor: str = "BROKERMT4-Demo",
                 demo: bool = True, offset_servidor_s: int = 10800, capacidade_ring: int = 20000,
                 slippage_pontos: float = 0.0, margem_por_lote: float = 0.0, token: str = "",
                 max_posicoes: int = 0, magics_permitidos: tuple[int, ...] = (), moeda_conta: str = "BRL") -> None:
        self.moeda_conta = moeda_conta.upper()
        self.token, self.max_posicoes, self.magics_permitidos = token, int(max_posicoes), tuple(magics_permitidos)
        self.relogio = relogio or RelogioReal()
        self.offset_ms = int(offset_servidor_s) * 1000
        self.slippage = slippage_pontos
        self.margem_por_lote = margem_por_lote
        self.conta = _Conta(login, servidor, demo)
        self.conectado = True
        self.capacidade_ring = int(capacidade_ring)
        self.epoca = 1
        self._specs: dict[str, dict[str, Any]] = {}
        self._ticks: dict[str, list[TickSint]] = {}
        self._tms: dict[str, list[int]] = {}
        self._idx: dict[str, int] = {}
        self._ring: dict[str, deque[tuple[int, int, int, float, float, int]]] = {}  # (seq, t_s, gtc, bid, ask, vol)
        self._seq: dict[str, int] = {}
        self._ult: dict[str, tuple[float, float, int]] = {}  # bid, ask, t_s
        self.posicoes: dict[int, dict[str, Any]] = {}
        self.pendentes: dict[int, dict[str, Any]] = {}
        self.historico: list[dict[str, Any]] = []
        self._ticket = 6_000_000
        self._falhas: list[Falha4] = []
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        # observabilidade para testes
        self.comandos: list[dict[str, Any]] = []
        self.envios: list[dict[str, Any]] = []  # comandos de ordem EXECUTADOS pelo servidor
        self.fora_do_ar = False

    # ------------------------------------------------------------------------------------------
    # configuracao (lado do teste)
    # ------------------------------------------------------------------------------------------
    def adicionar_simbolo(self, espec: dict[str, Any], ticks: list[TickSint] | tuple[TickSint, ...] = ()) -> None:
        nome = str(espec["nome"])
        self._specs[nome] = dict(espec)
        tk = sorted(ticks, key=lambda x: x[0])
        self._ticks[nome], self._tms[nome] = tk, [x[0] for x in tk]
        self._idx[nome], self._ring[nome], self._seq[nome] = 0, deque(), 0

    def injetar_falhas(self, *falhas: Falha4 | int | None) -> None:
        for f in falhas:
            self._falhas.append(f if isinstance(f, Falha4) else Falha4(f))

    def reiniciar_ea(self) -> None:
        """Simula recarregar o EA: nova epoca e buffers de ticks zerados (perda REAL de ticks para o cliente)."""
        self.epoca += 1
        for s in self._ring:
            self._ring[s].clear()
        self.cache_limpar()

    def cache_limpar(self) -> None:
        self._cache.clear()

    def definir_conta(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self.conta, k, v)

    # --- consultas auxiliares de teste -------------------------------------------------------------
    def tickets_abertos(self, simbolo: str | None = None) -> list[int]:
        self._sincronizar()
        return sorted(t for t, p in self.posicoes.items() if simbolo is None or p["simbolo"] == simbolo)

    def exposicao_liquida(self, simbolo: str) -> float:
        self._sincronizar()
        return sum(p["volume"] * (1 if p["tipo"] == C.POS_COMPRA else -1)
                   for p in self.posicoes.values() if p["simbolo"] == simbolo)

    def operar_manual(self, tipo: int, volume: float, simbolo: str) -> int:
        """Operacao MANUAL do usuario (magic 0, fora da ponte). Devolve o ticket."""
        self._sincronizar()
        return self._abrir_mercado(simbolo, tipo, volume, 0.0, 0.0, 0, "manual")["ticket"]

    def fechar_manual(self, ticket: int) -> None:
        self._sincronizar()
        p = self.posicoes[ticket]
        self._encerrar(ticket, p["volume"], self._preco_saida(p), "", fechado_ea=0)

    # ------------------------------------------------------------------------------------------
    # tempo e "servidor"
    # ------------------------------------------------------------------------------------------
    def _agora_ms(self) -> int:
        return self.relogio.agora_ns() // 1_000_000

    def _t_s(self, utc_ms: int) -> int:
        return (utc_ms + self.offset_ms) // 1000

    def _novo_ticket(self) -> int:
        self._ticket += 1
        return self._ticket

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
        t_ms, bid, ask = tk[0], tk[1], tk[2]
        t_s = self._t_s(t_ms)
        self._seq[sym] += 1
        anel = self._ring[sym]
        anel.append((self._seq[sym], t_s, t_ms & 0x7FFFFFFF, bid, ask, int(tk[4])))
        while len(anel) > self.capacidade_ring:
            anel.popleft()
        self._ult[sym] = (bid, ask, t_s)
        for t in list(self.pendentes):  # limitadas
            o = self.pendentes.get(t)
            if o is None or o["simbolo"] != sym:
                continue
            if (o["tipo"] == C.ORDEM_BUY_LIMIT and ask <= o["p_abertura"]) or (
                    o["tipo"] == C.ORDEM_SELL_LIMIT and bid >= o["p_abertura"]):
                del self.pendentes[t]
                self._abrir_posicao(sym, C.POS_COMPRA if o["tipo"] == C.ORDEM_BUY_LIMIT else C.POS_VENDA,
                                    o["volume"], o["p_abertura"], o["sl"], o["tp"], o["magic"], o["comentario"], t)
        for t, p in list(self.posicoes.items()):  # SL/TP vivem no servidor
            if p["simbolo"] != sym:
                continue
            compra = p["tipo"] == C.POS_COMPRA
            ref = bid if compra else ask
            bate_sl = p["sl"] > 0 and ((compra and bid <= p["sl"]) or (not compra and ask >= p["sl"]))
            bate_tp = p["tp"] > 0 and ((compra and bid >= p["tp"]) or (not compra and ask <= p["tp"]))
            if bate_sl:
                self._encerrar(t, p["volume"], ref, "[sl]", fechado_ea=0, t_s=t_s)
            elif bate_tp:
                self._encerrar(t, p["volume"], p["tp"], "[tp]", fechado_ea=0, t_s=t_s)

    def _valor_ponto(self, sym: str) -> float:
        e = self._specs[sym]
        return float(e["tick_value"]) / float(e["tick_size"])

    def _preco_entrada(self, sym: str, tipo: int) -> float:
        bid, ask, _ = self._ult[sym]
        return ask + self.slippage if tipo == C.POS_COMPRA else bid - self.slippage

    def _preco_saida(self, p: dict[str, Any]) -> float:
        bid, ask, _ = self._ult[p["simbolo"]]
        return bid if p["tipo"] == C.POS_COMPRA else ask

    # --- mutacoes de estado ---------------------------------------------------------------------------
    def _abrir_posicao(self, sym: str, tipo: int, volume: float, preco: float, sl: float, tp: float, magic: int,
                       comentario: str, ticket: int | None = None) -> dict[str, Any]:
        tk = ticket or self._novo_ticket()
        p = {"ticket": tk, "simbolo": sym, "tipo": tipo, "volume": volume, "p_abertura": preco, "sl": sl, "tp": tp,
             "magic": magic, "comentario": comentario, "t_abertura_s": self._t_s(self._agora_ms()), "comissao": 0.0,
             "swap": 0.0}
        self.posicoes[tk] = p
        return p

    def _abrir_mercado(self, sym: str, tipo: int, volume: float, sl: float, tp: float, magic: int,
                       comentario: str) -> dict[str, Any]:
        return self._abrir_posicao(sym, tipo, volume, self._preco_entrada(sym, tipo), sl, tp, magic, comentario)

    def _encerrar(self, ticket: int, volume: float, preco: float, sufixo: str, fechado_ea: int,
                  t_s: int | None = None) -> int | None:
        """Fecha `volume` da posicao. Parcial: o ticket encerra e o restante abre com NOVO ticket. Devolve o restante."""
        p = self.posicoes.pop(ticket)
        sinal = 1 if p["tipo"] == C.POS_COMPRA else -1
        fechado = min(volume, p["volume"])
        t_fe = t_s if t_s is not None else self._t_s(self._agora_ms())
        self.historico.append({
            "ticket": ticket, "tipo": p["tipo"], "simbolo": p["simbolo"], "volume": fechado,
            "p_abertura": p["p_abertura"], "p_fechamento": preco, "t_abertura_s": p["t_abertura_s"],
            "t_fechamento_s": t_fe, "sl": p["sl"], "tp": p["tp"],
            "lucro": (preco - p["p_abertura"]) * sinal * fechado * self._valor_ponto(p["simbolo"]),
            "comissao": p["comissao"], "swap": 0.0, "magic": p["magic"],
            "comentario": (p["comentario"] + sufixo).strip(), "fechado_ea": fechado_ea})
        self.conta.saldo += self.historico[-1]["lucro"]
        resto = p["volume"] - fechado
        if resto > 1e-12:
            novo = self._abrir_posicao(p["simbolo"], p["tipo"], resto, p["p_abertura"], p["sl"], p["tp"], p["magic"],
                                       f"from #{ticket}")
            return int(novo["ticket"])
        return None

    # ------------------------------------------------------------------------------------------
    # protocolo
    # ------------------------------------------------------------------------------------------
    def processar_bytes(self, dados: bytes) -> bytes:
        try:
            msg = P.decodificar(dados)
        except P.ErroProtocolo as e:
            return P.codificar(P.resposta_erro("", P.ERRO_JSON_INVALIDO, str(e)))
        r = self.processar(msg)
        if r is None:
            raise TimeoutError("DubleMT4: sem resposta (falha injetada)")
        return P.codificar(r)

    def processar(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Atende uma requisicao. None = sem resposta (falha injetada)."""
        with self._lock:
            if self.fora_do_ar or not self.conectado:
                return None
            id_ = str(msg.get("id", ""))
            cmd = str(msg.get("cmd", ""))
            self.comandos.append({"cmd": cmd, "args": dict(msg.get("args") or {})})
            if self.token and msg.get("tk") != self.token:  # como o EA com input Token preenchido
                return P.resposta_erro(id_, P.ERRO_TOKEN, "token ausente ou invalido")
            if msg.get("v") != P.VERSAO_PROTOCOLO:
                return P.resposta_erro(id_, P.ERRO_VERSAO, f"versao {msg.get('v')} nao suportada")
            if cmd not in P.COMANDOS:
                return P.resposta_erro(id_, P.ERRO_COMANDO_DESCONHECIDO, f"comando desconhecido: {cmd}")
            self._sincronizar()
            args = dict(msg.get("args") or {})
            if cmd in P.COMANDOS_ORDEM:
                if id_ in self._cache:  # idempotencia: mesmo id => mesma resposta, sem reexecutar
                    return self._cache[id_]
                falha = self._falhas.pop(0) if self._falhas else None
                if falha is not None and not falha.executa:
                    return None if falha.codigo is None else P.resposta_erro(id_, falha.codigo, "falha injetada", "mt4")
                r = self._ordem(id_, cmd, args)
                if falha is not None and falha.executa:
                    return None if falha.codigo is None else P.resposta_erro(id_, falha.codigo, "falha injetada", "mt4")
                self._cache[id_] = r
                while len(self._cache) > _CACHE_IDEM:
                    self._cache.popitem(last=False)
                return r
            try:
                return P.resposta_ok(id_, self._leitura(cmd, args))
            except KeyError as e:
                return P.resposta_erro(id_, P.ERRO_SIMBOLO, f"simbolo ou argumento desconhecido: {e}")

    # --- leituras ---------------------------------------------------------------------------------------
    def _leitura(self, cmd: str, a: dict[str, Any]) -> dict[str, Any]:
        c = self.conta
        if cmd == P.CMD_OLA:
            return {"versao_protocolo": P.VERSAO_PROTOCOLO, "ea": "RobowinBridge (DUBLE)", "ea_versao": "duble",
                    "epoca": self.epoca, "duble": True, "build": 0, "caminho": "DUBLE", "caminho_dados": "DUBLE",
                    "conta_demo": c.demo}
        if cmd == P.CMD_CONTA:
            margem = sum(p["volume"] for p in self.posicoes.values()) * self.margem_por_lote
            flut = sum(self._lucro(p) for p in self.posicoes.values())
            return {"login": c.login, "servidor": c.servidor, "empresa": "CORRETORA SINTETICA (DUBLE)", "moeda": self.moeda_conta,
                    "demo": c.demo, "trade_allowed": c.trade_allowed, "expert_enabled": c.expert,
                    "conectado": self.conectado, "dlls_permitidas": False, "saldo": c.saldo,
                    "patrimonio": c.saldo + flut, "lucro": flut, "margem": margem, "margem_livre": c.saldo + flut - margem,
                    "alavancagem": 1, "nome": "CONTA SINTETICA (DUBLE)", "hedging": True,
                    "t_servidor_s": self._t_s(self._agora_ms()), "gtc": self._agora_ms() & 0x7FFFFFFF}
        if cmd == P.CMD_SIMBOLOS:
            return {"simbolos": list(self._specs)}
        if cmd == P.CMD_SIMBOLO:
            return dict(self._specs[str(a["simbolo"])])
        if cmd == P.CMD_SELECIONAR:
            s = str(a["simbolo"])
            if s not in self._specs:
                return {"ok": False}
            self._specs[s]["visivel"] = bool(a.get("ativar", True))
            return {"ok": True}
        if cmd == P.CMD_TICK:
            s = str(a["simbolo"])
            if s not in self._specs:
                raise KeyError(s)
            if s not in self._ult:
                return {"t_s": 0, "gtc": 0, "bid": 0.0, "ask": 0.0, "seq": 0}
            bid, ask, t_s = self._ult[s]
            return {"t_s": t_s, "gtc": self._agora_ms() & 0x7FFFFFFF, "bid": bid, "ask": ask, "seq": self._seq[s]}
        if cmd == P.CMD_TICKS:
            return self._ticks_cmd(a)
        if cmd == P.CMD_POSICOES:
            f = str(a.get("simbolo") or "")
            return {"posicoes": [self._item_pos(p) for p in self.posicoes.values() if not f or p["simbolo"] == f]}
        if cmd == P.CMD_PENDENTES:
            f = str(a.get("simbolo") or "")
            return {"ordens": [self._item_pos(p) for p in self.pendentes.values() if not f or p["simbolo"] == f]}
        # historico
        de_s, ate_s = int(a.get("de_s", 0)), int(a.get("ate_s", 2_000_000_000))
        return {"itens": [h for h in self.historico if de_s <= h["t_fechamento_s"] <= ate_s or de_s <= h["t_abertura_s"] <= ate_s]}

    def _lucro(self, p: dict[str, Any]) -> float:
        bid, ask, _ = self._ult.get(p["simbolo"], (p["p_abertura"], p["p_abertura"], 0))
        atual = bid if p["tipo"] == C.POS_COMPRA else ask
        return (atual - p["p_abertura"]) * (1 if p["tipo"] == C.POS_COMPRA else -1) * p["volume"] * self._valor_ponto(p["simbolo"])

    def _item_pos(self, p: dict[str, Any]) -> dict[str, Any]:
        bid, ask, _ = self._ult.get(p["simbolo"], (p["p_abertura"], p["p_abertura"], 0))
        return {**{k: p[k] for k in ("ticket", "simbolo", "tipo", "volume", "p_abertura", "sl", "tp", "magic",
                                    "comentario", "t_abertura_s", "comissao", "swap")},
                "lucro": self._lucro(p) if p["ticket"] in self.posicoes else 0.0,
                "p_atual": bid if p["tipo"] in (C.POS_COMPRA, C.ORDEM_BUY_LIMIT) else ask}

    def _ticks_cmd(self, a: dict[str, Any]) -> dict[str, Any]:
        s = str(a["simbolo"])
        if s not in self._specs:
            raise KeyError(s)
        desde, epoca_cli, mx = int(a.get("desde_seq", -1)), int(a.get("epoca", 0)), int(a.get("max", 5000))
        reiniciou = epoca_cli != 0 and epoca_cli != self.epoca
        if reiniciou:
            desde = -1
        anel = self._ring[s]
        perdeu = bool(anel) and desde >= 0 and anel[0][0] > desde + 1
        sel = [r for r in anel if r[0] > desde]
        mais = len(sel) > mx
        sel = sel[:mx]
        return {"epoca": self.epoca, "ticks": [list(r) for r in sel], "seq_max": self._seq[s], "perdeu": perdeu,
                "reiniciou": reiniciou, "mais": mais}

    # --- ordens -------------------------------------------------------------------------------------------
    def _erro(self, id_: str, codigo: int, msg: str, origem: str = "mt4") -> dict[str, Any]:
        return P.resposta_erro(id_, codigo, msg, origem)

    def _ordem(self, id_: str, cmd: str, a: dict[str, Any]) -> dict[str, Any]:
        c = self.conta
        if not c.demo:
            return self._erro(id_, P.ERRO_CONTA_NAO_DEMO, "conta REAL: a ponte so negocia em conta DEMO", "ponte")
        if c.somente_leitura:
            return self._erro(id_, P.ERRO_SOMENTE_LEITURA, "EA em modo somente leitura", "ponte")
        if not c.expert or not c.trade_allowed:
            return self._erro(id_, P.M4_TRADE_NAO_PERMITIDO, "trade nao permitido (AutoTrading/EA)")
        bad = self._defesa_ea(id_, cmd, a)  # limites do EA (defesa em profundidade, espelham o mq4)
        if bad is not None:
            return bad
        if cmd == P.CMD_ORDEM_ABRIR:
            r = self._cmd_abrir(id_, a)
        elif cmd == P.CMD_ORDEM_FECHAR:
            r = self._cmd_fechar(id_, a)
        elif cmd == P.CMD_ORDEM_MODIFICAR:
            r = self._cmd_modificar(id_, a)
        else:
            r = self._cmd_apagar(id_, a)
        if r["ok"]:
            self.envios.append({"cmd": cmd, "args": dict(a)})
        return r

    def _defesa_ea(self, id_: str, cmd: str, a: dict[str, Any]) -> dict[str, Any] | None:
        if cmd == P.CMD_ORDEM_ABRIR:
            magic = int(a.get("magic", 0))
            if self.magics_permitidos and magic not in self.magics_permitidos:
                return self._erro(id_, P.ERRO_MAGIC, f"magic {magic} nao permitido", "ponte")
            if self.max_posicoes > 0:
                sym = str(a.get("simbolo", ""))
                n = sum(1 for o in (*self.posicoes.values(), *self.pendentes.values())
                        if o["simbolo"] == sym and int(o["magic"]) == magic)
                if n >= self.max_posicoes:
                    return self._erro(id_, P.ERRO_LIMITE_POSICOES, f"{n} posicoes+pendentes em {sym}/magic {magic} (max {self.max_posicoes})", "ponte")
        elif self.magics_permitidos:
            o = self.posicoes.get(int(a.get("ticket", 0))) or self.pendentes.get(int(a.get("ticket", 0)))
            if o is not None and int(o["magic"]) not in self.magics_permitidos:
                return self._erro(id_, P.ERRO_MAGIC, f"ticket de magic {o['magic']} nao permitido", "ponte")
        return None

    def _validar_abertura(self, id_: str, a: dict[str, Any]) -> dict[str, Any] | None:
        sym = str(a.get("simbolo", ""))
        e = self._specs.get(sym)
        if e is None:
            return self._erro(id_, P.M4_SIMBOLO_DESCONHECIDO, "simbolo desconhecido")
        vol = float(a.get("volume", 0.0))
        passo = float(e["lote_passo"])
        if vol < e["lote_min"] or vol > e["lote_max"] or abs(vol / passo - round(vol / passo)) > 1e-9:
            return self._erro(id_, P.M4_VOLUME_INVALIDO, "volume invalido")
        if sym not in self._ult:
            return self._erro(id_, P.M4_MERCADO_FECHADO, "sem cotacao")
        if self.margem_por_lote > 0 and vol * self.margem_por_lote > self._leitura(P.CMD_CONTA, {})["margem_livre"]:
            return self._erro(id_, P.M4_SEM_DINHEIRO, "margem insuficiente")
        bid, ask, _ = self._ult[sym]
        tipo, sl, tp = int(a["tipo"]), float(a.get("sl", 0.0)), float(a.get("tp", 0.0))
        nivel = float(e.get("stop_level", 0)) * float(e.get("ponto", 1.0))
        if tipo == C.ORDEM_BUY and ((sl and sl > bid - nivel + 1e-9) or (tp and tp < bid + nivel - 1e-9)):
            return self._erro(id_, P.M4_STOPS_INVALIDOS, "stops invalidos")
        if tipo == C.ORDEM_SELL and ((sl and sl < ask + nivel - 1e-9) or (tp and tp > ask - nivel + 1e-9)):
            return self._erro(id_, P.M4_STOPS_INVALIDOS, "stops invalidos")
        preco = float(a.get("preco", 0.0))
        if tipo == C.ORDEM_BUY_LIMIT and preco >= ask:
            return self._erro(id_, P.M4_PRECO_INVALIDO, "preco invalido")
        if tipo == C.ORDEM_SELL_LIMIT and preco <= bid:
            return self._erro(id_, P.M4_PRECO_INVALIDO, "preco invalido")
        if tipo not in (C.ORDEM_BUY, C.ORDEM_SELL, C.ORDEM_BUY_LIMIT, C.ORDEM_SELL_LIMIT):
            return self._erro(id_, P.ERRO_ARGUMENTO, "tipo de ordem nao suportado", "ponte")
        return None

    def _cmd_abrir(self, id_: str, a: dict[str, Any]) -> dict[str, Any]:
        bad = self._validar_abertura(id_, a)
        if bad is not None:
            return bad
        sym, tipo, vol = str(a["simbolo"]), int(a["tipo"]), float(a["volume"])
        sl, tp, magic, com = float(a.get("sl", 0.0)), float(a.get("tp", 0.0)), int(a.get("magic", 0)), str(a.get("comentario", ""))
        if tipo in (C.ORDEM_BUY, C.ORDEM_SELL):
            p = self._abrir_mercado(sym, tipo, vol, sl, tp, magic, com)
            return P.resposta_ok(id_, {"ticket": p["ticket"], "preco": p["p_abertura"], "volume": vol,
                                       "t_s": p["t_abertura_s"]})
        tk = self._novo_ticket()
        self.pendentes[tk] = {"ticket": tk, "simbolo": sym, "tipo": tipo, "volume": vol, "p_abertura": float(a["preco"]),
                              "sl": sl, "tp": tp, "magic": magic, "comentario": com,
                              "t_abertura_s": self._t_s(self._agora_ms()), "comissao": 0.0, "swap": 0.0}
        return P.resposta_ok(id_, {"ticket": tk, "preco": float(a["preco"]), "volume": vol,
                                   "t_s": self.pendentes[tk]["t_abertura_s"]})

    def _cmd_fechar(self, id_: str, a: dict[str, Any]) -> dict[str, Any]:
        tk = int(a.get("ticket", 0))
        p = self.posicoes.get(tk)
        if p is None:
            return self._erro(id_, P.M4_TICKET_INVALIDO, f"ticket {tk} nao e uma posicao aberta")
        vol = float(a.get("volume", p["volume"]))
        if vol <= 0 or vol > p["volume"] + 1e-9:
            return self._erro(id_, P.M4_VOLUME_INVALIDO, "volume de fechamento invalido")
        preco = self._preco_saida(p)
        resto = self._encerrar(tk, vol, preco, "", fechado_ea=1)
        return P.resposta_ok(id_, {"ticket": tk, "preco": preco, "volume_fechado": min(vol, p["volume"]),
                                   "ticket_restante": resto or 0})

    def _cmd_modificar(self, id_: str, a: dict[str, Any]) -> dict[str, Any]:
        tk = int(a.get("ticket", 0))
        o = self.posicoes.get(tk) or self.pendentes.get(tk)
        if o is None:
            return self._erro(id_, P.M4_TICKET_INVALIDO, f"ticket {tk} inexistente")
        sl, tp = float(a.get("sl", 0.0)), float(a.get("tp", 0.0))
        bid, ask, _ = self._ult.get(o["simbolo"], (0.0, 0.0, 0))
        if tk in self.posicoes and ((o["tipo"] == C.POS_COMPRA and ((sl and sl >= bid) or (tp and tp <= bid)))
                                    or (o["tipo"] == C.POS_VENDA and ((sl and sl <= ask) or (tp and tp >= ask)))):
            return self._erro(id_, P.M4_STOPS_INVALIDOS, "stops invalidos")
        o["sl"], o["tp"] = sl, tp
        if tk in self.pendentes and float(a.get("preco", 0.0)) > 0:
            o["p_abertura"] = float(a["preco"])
        return P.resposta_ok(id_, {"ticket": tk})

    def _cmd_apagar(self, id_: str, a: dict[str, Any]) -> dict[str, Any]:
        tk = int(a.get("ticket", 0))
        if tk not in self.pendentes:
            return self._erro(id_, P.M4_TICKET_INVALIDO, f"ticket {tk} nao e ordem pendente")
        del self.pendentes[tk]
        return P.resposta_ok(id_, {"ticket": tk})


class ServidorDubleMT4:
    """Thread com ZeroMQ REP real servindo um `DubleMT4` (testa o transporte, o timeout e a reconexao)."""

    def __init__(self, duble: DubleMT4, endereco: str = "tcp://127.0.0.1:*", atraso_sem_resposta_s: float = 0.4) -> None:
        import zmq
        self._zmq = zmq
        self.duble = duble
        self._pedido = endereco
        self.atraso_sem_resposta_s = atraso_sem_resposta_s
        self._ctx = zmq.Context()
        self._sock: Any = None
        self._thread: threading.Thread | None = None
        self._parar = threading.Event()
        self.endereco = ""

    def iniciar(self) -> str:
        z = self._zmq
        self._sock = self._ctx.socket(z.REP)
        self._sock.setsockopt(z.LINGER, 0)
        if self._pedido.endswith(":*"):
            porta = self._sock.bind_to_random_port(self._pedido[:-2], min_port=20000, max_port=60000)
            self.endereco = f"{self._pedido[:-2]}:{porta}"
        else:
            self._sock.bind(self._pedido)
            self.endereco = self._pedido
        self._thread = threading.Thread(target=self._laco, name="DubleMT4", daemon=True)
        self._thread.start()
        return self.endereco

    def _laco(self) -> None:
        z, s = self._zmq, self._sock
        while not self._parar.is_set():
            if not s.poll(50):
                continue
            try:
                dados = s.recv()
            except z.ZMQError:
                break
            try:
                resp = self.duble.processar_bytes(dados)
            except TimeoutError:  # falha injetada "sem resposta": o EA real so responderia tarde demais
                time.sleep(self.atraso_sem_resposta_s)
                resp = P.codificar(P.resposta_erro("", P.M4_TIMEOUT, "resposta tardia", "mt4"))
            try:
                s.send(resp)
            except z.ZMQError:
                break

    def parar(self) -> None:
        self._parar.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._sock is not None:
            self._sock.close(0)
        self._ctx.term()

    def __enter__(self) -> ServidorDubleMT4:
        self.iniciar()
        return self

    def __exit__(self, *a: object) -> None:
        self.parar()


def _espec_mt4(e: Any) -> dict[str, Any]:
    """Converte o symbol_info sintetico do DubleMT5 para a especificacao do protocolo do MT4."""
    return {
        "nome": e.name, "caminho": e.path, "descricao": e.description, "digits": e.digits, "ponto": e.point,
        "visivel": True, "spread": e.spread, "negociavel": True, "execucao": C.EXEC_MARKET,
        "tick_size": e.trade_tick_size, "tick_value": e.trade_tick_value, "lot_size": e.trade_contract_size,
        "lote_min": e.volume_min, "lote_max": e.volume_max, "lote_passo": e.volume_step,
        "expiracao_s": e.expiration_time, "moeda_lucro": e.currency_profit, "moeda_base": e.currency_base,
        "stop_level": 0, "freeze_level": 0, "margem_requerida": 0.0,
        **_espec_mt4_generica(e),
    }


def _espec_mt4_generica(e: Any) -> dict[str, Any]:
    """Campos de qualquer mercado (forex/CFD) no formato do EA (MT4: modo de lucro e tipo de swap proprios)."""
    if getattr(e, "trade_calc_mode", None) is None:
        return {}
    mt5_para_mt4_calc = {0: 0, 2: 1, 1: 2}
    mt5_para_mt4_swap = {1: 0, 2: 1, 5: 2, 3: 3}
    return {"moeda_margem": e.currency_margin, "modo_lucro": mt5_para_mt4_calc.get(int(e.trade_calc_mode), 1),
            "swap_long": e.swap_long, "swap_short": e.swap_short,
            "swap_tipo": mt5_para_mt4_swap.get(int(e.swap_mode), 0), "swap_triplo": e.swap_rollover3days}


def criar_duble_mt4_padrao(relogio: Relogio | None = None, *, n_ticks: int = 6000, **kw: Any) -> DubleMT4:
    """DubleMT4 com os mesmos simbolos/series SINTETICOS do `criar_duble_padrao` (sem livro; tempo em segundos)."""
    moeda_conta = str(kw.pop("moeda_conta", "BRL"))
    base: DubleMT5 = criar_duble_padrao(relogio, n_ticks=n_ticks, semente=1, moeda_conta=moeda_conta)
    d = DubleMT4(base.relogio, moeda_conta=moeda_conta, **kw)
    for nome, esp in base.especificacoes.items():
        d.adicionar_simbolo(_espec_mt4(esp), base._ticks[nome])
    return d
