"""Execucao na conta DEMO do MT5: o UNICO ponto de `order_send` de todo o projeto.

Garantido por teste estatico (AST) em tests/unit/test_mt5_ast.py. Guardiao e kill switch tambem passam por
aqui. Ordens reais NAO existem: `ExecucaoReal.enviar` sempre levanta `ExecucaoRealNaoAutorizada`.

Antes de CADA envio (e de cada order_check) confere: modo `demo_mt5` na configuracao, login e servidor
exatos esperados, `trade_mode == demo`, `trade_allowed` e `trade_expert`. Qualquer falha levanta excecao e
NENHUM `order_send` acontece.

Convencoes
- `ComandoOrdem.lado` e o lado da ORDEM enviada. Para `tipo="saida"` o lado deve ser o oposto ao da posicao
  (venda para fechar compra); a saida sempre leva `position=<ticket>`.
- Retorno local de recusa: `AckOrdem(retcode=RET_LOCAL_RECUSADO (-1))`; nada foi enviado.
- INCERTA (order_send None / 10012 / 10031): `AckOrdem.retcode` e o recebido (None ou o codigo) e o
  comentario comeca com "INCERTA". Nunca ha reenvio; entradas ficam bloqueadas e saidas do mesmo ticket
  tambem, ate `reconciliar_incertas()` encontrar evidencia ou vencer `espera_incerta_s`.
- t_setup_msc / t_done_msc: epoch UTC em ms do relogio local antes/depois do envio (o terminal nao os devolve).
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from robowin.core.relogio import Relogio, RelogioReal
from robowin.core.tipos import AckOrdem, ComandoOrdem, EstadoConta, EstadoOrdem, Lado

from . import constantes as C
from .conta import ler_estado_conta
from .specs import preenchimento_do_simbolo
from .tempo_servidor import RelogioServidor

Alerta = Callable[[str, dict[str, Any]], None]


class GuardaDemoViolada(Exception):
    """A guarda de conta demo falhou: nenhum envio e permitido."""


class ContaNaoEsperada(GuardaDemoViolada):
    """Login ou servidor diferentes dos esperados (troca de conta no meio da sessao): o gateway deve parar."""


class ExecucaoRealNaoAutorizada(ContaNaoEsperada):
    """Tentativa de operar conta nao-demo (real/contest) ou de usar o ponto de extensao de ordens reais."""


def violacao_de_identidade(e: BaseException) -> bool:
    """True se a violacao e de IDENTIDADE da conta (real/contest/login/servidor) e nao de permissao transitoria."""
    return isinstance(e, ContaNaoEsperada)


@dataclass
class ConfigDemo:
    login: int
    servidor: str
    simbolo: str
    modo_config: str = "demo_mt5"  # valor de [geral].conta em config/robowin.toml
    contratos_max: int = 1
    msgs_por_min: int = 20
    desvio: int = 20
    magic_guardiao: int = 990001
    recuo_base_s: float = 1.0
    recuo_max_s: float = 30.0
    espera_incerta_s: float = 5.0
    lote: int = 1  # unidades de volume do MT5 por "contrato" do robo (do perfil: 1 em futuros, 100 em acoes)


class OrcamentoMensagens:
    """Teto de mensagens por minuto (novas + modificacoes + cancelamentos), janela deslizante de 60 s."""

    def __init__(self, teto_por_min: int, relogio: Relogio) -> None:
        self.teto, self.relogio = teto_por_min, relogio
        self._ts: deque[int] = deque()

    def _podar(self) -> None:
        corte = self.relogio.agora_ns() - 60_000_000_000
        while self._ts and self._ts[0] <= corte:
            self._ts.popleft()

    def usadas(self) -> int:
        self._podar()
        return len(self._ts)

    def disponivel(self) -> bool:
        return self.usadas() < self.teto

    def registrar(self) -> None:
        self._podar()
        self._ts.append(self.relogio.agora_ns())


class _Recusa(Exception):
    def __init__(self, ack: AckOrdem) -> None:
        super().__init__(ack.comentario)
        self.ack = ack


def estado_do_retcode(retcode: int | None) -> EstadoOrdem:
    """Mapa retcode -> estado do OMS (None/10012/10031 => INCERTA; 10008 = colocada, nao executada)."""
    if retcode is None or retcode in (C.RET_TIMEOUT, C.RET_SEM_CONEXAO):
        return EstadoOrdem.INCERTA
    return {C.RET_COLOCADA: EstadoOrdem.ACEITA, C.RET_EXECUTADA: EstadoOrdem.EXECUTADA,
            C.RET_PARCIAL: EstadoOrdem.PARCIAL}.get(retcode, EstadoOrdem.REJEITADA)


def _sinal(tipo: int) -> int:
    return 1 if tipo == C.POS_COMPRA else -1


class ExecucaoDemo:
    """Implementa o Protocolo `Executor` sobre uma conta DEMO."""

    def __init__(
        self, terminal: Any, cfg: ConfigDemo, relogio: Relogio | None = None, alerta: Alerta | None = None,
        registrar: Callable[[str, dict[str, Any]], None] | None = None, relogio_srv: RelogioServidor | None = None,
    ) -> None:
        self.t, self.cfg = terminal, cfg
        self.relogio = relogio or RelogioReal()
        self.rs = relogio_srv or RelogioServidor()
        self._alerta = alerta or (lambda tipo, dados: None)
        self._registrar = registrar or (lambda ev, dados: None)
        self.orcamento = OrcamentoMensagens(cfg.msgs_por_min, self.relogio)
        self.estados: dict[str, EstadoOrdem] = {}
        self._enviadas: set[str] = set()
        self._incertas: dict[str, dict[str, Any]] = {}
        self._donos: dict[int, str] = {}
        self._recuo_ate_ns = 0
        self._recuos = 0
        self.travado: str | None = None  # motivo do travamento por violacao de netting/limite
        self.envios = 0  # numero de order_send efetivamente chamados
        self._override_simbolo: str | None = None

    @property
    def _sym(self) -> str:
        return self._override_simbolo or self.cfg.simbolo

    # ------------------------------------------------------------------------------------------
    # guarda
    # ------------------------------------------------------------------------------------------
    def verificar_guarda(self) -> EstadoConta:
        """Confere a conta ANTES de cada envio. Levanta `GuardaDemoViolada` se algo diverge."""
        if self.cfg.modo_config != "demo_mt5":
            raise GuardaDemoViolada(f"modo de configuracao '{self.cfg.modo_config}' != 'demo_mt5'")
        est = ler_estado_conta(self.t)
        if est is None:
            raise GuardaDemoViolada("account_info indisponivel: terminal nao responde")
        if est.trade_mode != "demo":
            raise ExecucaoRealNaoAutorizada(f"conta {est.trade_mode} (login {est.login}): ordens so em conta DEMO")
        if est.login != self.cfg.login:
            raise ContaNaoEsperada(f"login {est.login} difere do esperado {self.cfg.login} (troca de conta)")
        if est.servidor != self.cfg.servidor or "DEMO" not in est.servidor.upper():
            raise ContaNaoEsperada(f"servidor '{est.servidor}' difere do esperado '{self.cfg.servidor}'")
        if not est.trade_allowed:
            raise GuardaDemoViolada("trade_allowed falso (negociacao desabilitada)")
        if not est.trade_expert:
            raise GuardaDemoViolada("trade_expert falso (algotrading desligado na conta)")
        if est.margin_mode == C.NOMES_MARGEM[C.MARGEM_HEDGING]:
            raise GuardaDemoViolada("conta em modo hedging; o projeto exige netting")
        return est

    def verificar_identidade(self) -> None:
        """Somente identidade da conta (modo, login, servidor), sem exigir permissoes de negociacao."""
        est = ler_estado_conta(self.t)
        if est is None:
            return  # terminal sem resposta: tratado pela reconciliacao
        if est.trade_mode != "demo":
            raise ExecucaoRealNaoAutorizada(f"conta {est.trade_mode} (login {est.login}): ordens so em conta DEMO")
        if est.login != self.cfg.login or est.servidor != self.cfg.servidor:
            raise ContaNaoEsperada(f"conta {est.login}@{est.servidor} difere da esperada {self.cfg.login}@{self.cfg.servidor}")

    # ------------------------------------------------------------------------------------------
    # leituras
    # ------------------------------------------------------------------------------------------
    def _recusa(self, motivo: str, retcode: int = C.RET_LOCAL_RECUSADO) -> _Recusa:
        return _Recusa(AckOrdem(retcode=retcode, comentario=f"recusado localmente: {motivo}"))

    def _posicao(self) -> tuple[int | None, float, Any]:
        ps = self.t.positions_get(symbol=self._sym)
        if ps is None:
            raise self._recusa("positions_get sem resposta")
        if len(ps) > 1:
            raise self._recusa("mais de uma posicao no simbolo (conta nao e netting?)")
        if not ps:
            return None, 0.0, None
        p = ps[0]
        return int(p.ticket), _sinal(int(p.type)) * float(p.volume) / self.cfg.lote, p  # em lotes do robo

    def _pendentes(self) -> tuple[Any, ...]:
        os_ = self.t.orders_get(symbol=self._sym)
        if os_ is None:
            raise self._recusa("orders_get sem resposta")
        return tuple(os_)

    def _info(self) -> Any:
        info = self.t.symbol_info(self._sym)
        if info is None:
            raise self._recusa("symbol_info indisponivel")
        return info

    @staticmethod
    def _alinhar(preco: float, tick: float) -> float:
        return round(round(preco / tick) * tick, 10)

    def _agora_ms(self) -> int:
        return self.relogio.agora_ns() // 1_000_000

    @property
    def bloqueada_incerta(self) -> bool:
        return bool(self._incertas)

    @property
    def incertas(self) -> dict[str, dict[str, Any]]:
        return dict(self._incertas)

    # ------------------------------------------------------------------------------------------
    # API publica de envio
    # ------------------------------------------------------------------------------------------
    def enviar(self, cmd: ComandoOrdem, dono: str = "nucleo", prioritaria: bool = False,
               forcar_dono: bool = False) -> AckOrdem:
        """Envia um comando ao terminal DEMO. Excecoes de guarda se propagam; demais recusas viram Ack local."""
        self.verificar_guarda()
        try:
            return self._enviar(cmd, dono, prioritaria, forcar_dono, corretivo=False)
        except _Recusa as r:
            if cmd.intencao_id not in self._enviadas:  # duplicata nao sobrescreve o estado da intencao original
                self.estados[cmd.intencao_id] = EstadoOrdem.REJEITADA
            self._registrar("recusa_local", {"intencao_id": cmd.intencao_id, "motivo": r.ack.comentario})
            return r.ack

    def zerar_posicao(self, ticket: int, dono: str = "guardiao", motivo: str = "", intencao_id: str | None = None,
                      forcar_dono: bool = True, simbolo: str | None = None) -> AckOrdem:
        """Saida a mercado pelo ticket (kill switch, guardiao, zeragem). Prioritaria: ignora recuo/orcamento.

        `simbolo` permite zerar posicao em simbolo diferente do configurado (ex.: intervencao manual)."""
        self._override_simbolo = simbolo
        try:
            return self._zerar_posicao(ticket, dono, motivo, intencao_id, forcar_dono)
        finally:
            self._override_simbolo = None

    def _zerar_posicao(self, ticket: int, dono: str, motivo: str, intencao_id: str | None, forcar_dono: bool) -> AckOrdem:
        self.verificar_guarda()
        p = None
        ps = self.t.positions_get(symbol=self._sym) if self._sym else None
        if ps:
            p = next((x for x in ps if int(x.ticket) == ticket), None)
        if p is None:
            return AckOrdem(retcode=C.RET_LOCAL_RECUSADO, comentario="recusado localmente: ticket inexistente (ja encerrado)")
        lado = Lado.VENDA if int(p.type) == C.POS_COMPRA else Lado.COMPRA
        iid = intencao_id or f"zerar-{ticket}-{self._agora_ms()}-{len(self._enviadas)}"
        cmd = ComandoOrdem(intencao_id=iid, magic=self.cfg.magic_guardiao, lado=lado, tipo="saida",
                           quantidade=max(1, int(p.volume / self.cfg.lote)), position_ticket=ticket)
        self._registrar("zerar_posicao", {"ticket": ticket, "dono": dono, "motivo": motivo})
        return self.enviar(cmd, dono=dono, prioritaria=True, forcar_dono=forcar_dono)

    def modificar_sltp(self, ticket: int, sl: float | None, tp: float | None, dono: str = "nucleo",
                       prioritaria: bool = False) -> AckOrdem:
        """Altera SL/TP da posicao. O stop so pode APERTAR (compra: sobe; venda: desce)."""
        self.verificar_guarda()
        iid = f"sltp-{ticket}-{self._agora_ms()}-{len(self._enviadas)}"
        try:
            tk, signed, p = self._posicao()
            if p is None or tk != ticket:
                raise self._recusa("ticket inexistente para modificar SL/TP")
            info = self._info()
            tick = float(info.trade_tick_size)
            novo_sl = self._alinhar(sl, tick) if sl else 0.0
            novo_tp = self._alinhar(tp, tick) if tp else float(p.tp)
            sl_at = float(p.sl)
            if novo_sl == 0.0:
                novo_sl = sl_at
            elif sl_at > 0 and ((signed > 0 and novo_sl < sl_at) or (signed < 0 and novo_sl > sl_at)):
                raise self._recusa("stop so pode apertar, nunca afrouxar")
            req = {"action": C.ACAO_SLTP, "symbol": self._sym, "position": ticket, "sl": novo_sl,
                   "tp": novo_tp, "magic": int(p.magic)}
            res, t0, t1 = self._transmitir(req, iid, checar=True, prioritaria=prioritaria)
            return self._ack(res, t0, t1)
        except _Recusa as r:
            return r.ack

    def cancelar_ordem(self, ticket: int, dono: str = "nucleo", prioritaria: bool = False,
                       simbolo: str | None = None) -> AckOrdem:
        """Cancela ordem pendente (conta na franquia de mensagens)."""
        self.verificar_guarda()
        self._override_simbolo = simbolo
        try:
            return self._cancelar(ticket, prioritaria)
        finally:
            self._override_simbolo = None

    def _cancelar(self, ticket: int, prioritaria: bool) -> AckOrdem:
        iid = f"cancel-{ticket}-{self._agora_ms()}-{len(self._enviadas)}"
        try:
            if not any(int(o.ticket) == ticket for o in self._pendentes()):
                raise self._recusa("ordem pendente inexistente")
            req = {"action": C.ACAO_REMOVE, "order": ticket, "symbol": self._sym}
            res, t0, t1 = self._transmitir(req, iid, checar=False, prioritaria=prioritaria)
            return self._ack(res, t0, t1)
        except _Recusa as r:
            return r.ack

    def checar_sem_enviar(self, lado: Lado = Lado.COMPRA) -> dict[str, Any]:
        """Diagnostico: monta uma ordem a mercado de 1 lote e roda SOMENTE order_check (nunca order_send)."""
        est = self.verificar_guarda()
        info = self._info()
        tk = self.t.symbol_info_tick(self._sym)
        if tk is None:
            return {"ok": False, "motivo": "sem cotacao para montar a requisicao", "conta": est.model_dump()}
        compra = lado == Lado.COMPRA
        req = {"action": C.ACAO_DEAL, "symbol": self._sym, "volume": float(info.volume_min),
               "type": C.ORDEM_BUY if compra else C.ORDEM_SELL, "price": float(tk.ask if compra else tk.bid),
               "deviation": self.cfg.desvio, "magic": self.cfg.magic_guardiao, "comment": "rw:diagnostico",
               "type_filling": preenchimento_do_simbolo(info)}
        res = self.t.order_check(req)
        if res is None:
            return {"ok": False, "motivo": "order_check sem resposta", "requisicao": req}
        return {"ok": int(res.retcode) in C.RETS_CHECK_OK, "retcode": int(res.retcode),
                "comentario": str(getattr(res, "comment", "")), "requisicao": req}

    # ------------------------------------------------------------------------------------------
    # nucleo do envio
    # ------------------------------------------------------------------------------------------
    def _enviar(self, cmd: ComandoOrdem, dono: str, prioritaria: bool, forcar_dono: bool, corretivo: bool) -> AckOrdem:
        saida = cmd.tipo == "saida"
        if cmd.quantidade < 1 or (not saida and cmd.quantidade > self.cfg.contratos_max):
            raise self._recusa(f"quantidade {cmd.quantidade} fora de 1..{self.cfg.contratos_max}")
        if cmd.intencao_id in self._enviadas:
            raise self._recusa("intencao ja enviada (idempotencia: nunca reenviar)")
        if not saida and self.travado:
            raise self._recusa(f"executor travado: {self.travado}")
        if not saida and self._incertas:
            raise self._recusa("ordem INCERTA pendente de reconciliacao: entradas bloqueadas")
        if not prioritaria and self._recuo_ate_ns > self.relogio.agora_ns():
            raise self._recusa("recuo ativo apos 10024 (muitas requisicoes)")

        info = self._info()
        tick = float(info.trade_tick_size)
        ticket, antes, pos = self._posicao()
        fill = preenchimento_do_simbolo(info)
        tk = self.t.symbol_info_tick(self._sym)
        if tk is None:
            raise self._recusa("sem cotacao")
        compra = cmd.lado == Lado.COMPRA
        tipo_ordem = C.ORDEM_BUY if compra else C.ORDEM_SELL
        comentario = f"rw:{cmd.intencao_id[-20:]}"
        qtd = cmd.quantidade

        if saida:
            if cmd.position_ticket is None:
                raise self._recusa("saida exige position_ticket")
            if pos is None or ticket != cmd.position_ticket:
                raise self._recusa("ticket inexistente: posicao ja encerrada (saida duplicada nao e enviada)")
            if (antes > 0) == compra:
                raise self._recusa("lado da saida igual ao da posicao: aumentaria/inverteria a posicao")
            qtd = min(qtd, int(abs(antes)))
            if any(i["ticket"] == ticket for i in self._incertas.values()):
                raise self._recusa("saida INCERTA pendente para este ticket: aguardar reconciliacao")
            tipos_oposto = (C.ORDEM_SELL, C.ORDEM_SELL_LIMIT) if antes > 0 else (C.ORDEM_BUY, C.ORDEM_BUY_LIMIT)
            if not corretivo and any(int(o.type) in tipos_oposto for o in self._pendentes()):
                raise self._recusa("saida em voo: ordem aberta de sentido oposto a posicao no simbolo (nao duplicar saida)")
            dono_atual = self._donos.get(ticket)
            if dono_atual is not None and dono_atual != dono and not forcar_dono:
                raise self._recusa(f"saida do ticket {ticket} pertence a '{dono_atual}' (dono unico)")
            self._donos = {t: d for t, d in self._donos.items() if t == ticket}
            self._donos[ticket] = dono
            req: dict[str, Any] = {
                "action": C.ACAO_DEAL, "symbol": self._sym, "volume": float(qtd * self.cfg.lote), "type": tipo_ordem,
                "price": float(tk.ask if compra else tk.bid), "deviation": self.cfg.desvio,
                "magic": cmd.magic, "comment": comentario, "type_filling": fill, "position": ticket,
            }
        else:
            if pos is not None:
                raise self._recusa("ja ha posicao aberta: sem piramidacao e sem inversao por entrada")
            pend = self._pendentes()
            total_pend = sum(float(o.volume_current) for o in pend) / self.cfg.lote
            if total_pend + qtd > self.cfg.contratos_max:
                raise self._recusa("posicao + pendentes excederiam o maximo de contratos")
            sl = self._alinhar(cmd.sl, tick) if cmd.sl else None
            tp = self._alinhar(cmd.tp, tick) if cmd.tp else None
            if cmd.tipo == "mercado":
                req = {"action": C.ACAO_DEAL, "symbol": self._sym, "volume": float(qtd * self.cfg.lote), "type": tipo_ordem,
                       "price": float(tk.ask if compra else tk.bid), "deviation": self.cfg.desvio,
                       "magic": cmd.magic, "comment": comentario, "type_filling": fill}
            elif cmd.tipo == "limitada":
                if cmd.preco is None:
                    raise self._recusa("ordem limitada sem preco")
                req = {"action": C.ACAO_PENDING, "symbol": self._sym, "volume": float(qtd * self.cfg.lote),
                       "type": C.ORDEM_BUY_LIMIT if compra else C.ORDEM_SELL_LIMIT,
                       "price": self._alinhar(cmd.preco, tick), "magic": cmd.magic, "comment": comentario,
                       "type_filling": C.PREENCHE_RETURN if not (info.filling_mode & C.SIMB_FILL_IOC) else fill,
                       "type_time": C.VALIDADE_DIA}
            else:
                raise self._recusa(f"tipo de ordem desconhecido: {cmd.tipo}")
            if sl:
                req["sl"] = sl
            if tp:
                req["tp"] = tp

        res, t0, t1 = self._transmitir(req, cmd.intencao_id, checar=True, prioritaria=prioritaria,
                                       meta={"tipo": cmd.tipo, "lado": cmd.lado.value, "magic": cmd.magic,
                                             "ticket": ticket if saida else None})
        ack = self._ack(res, t0, t1)
        self.estados[cmd.intencao_id] = estado_do_retcode(ack.retcode)
        if ack.retcode in (C.RET_EXECUTADA, C.RET_PARCIAL) and req["action"] == C.ACAO_DEAL:  # mesmo com deal==0
            self._conferir_apos_deal(cmd, antes, ack, corretivo)
        return ack

    def _transmitir(self, req: dict[str, Any], intencao_id: str, checar: bool, prioritaria: bool,
                    meta: dict[str, Any] | None = None) -> tuple[Any, int, int]:
        """order_check -> guarda -> intencao gravada -> order_send. UNICO ponto de order_send do projeto."""
        # saidas prioritarias (seguranca) ignoram o teto, mas continuam contadas
        if not prioritaria and not self.orcamento.disponivel():
            raise self._recusa(f"orcamento de mensagens esgotado ({self.orcamento.teto}/min)")
        if checar:
            chk = self.t.order_check(req)
            if chk is None:
                raise self._recusa("order_check sem resposta")
            rc = int(chk.retcode)
            if rc not in C.RETS_CHECK_OK:
                self._tratar_retcode(rc, intencao_id)
                raise _Recusa(AckOrdem(retcode=rc, comentario=f"order_check recusou: {getattr(chk, 'comment', '')}"))
        self.verificar_guarda()  # de novo, imediatamente antes do envio
        # intencao gravada ANTES do envio (idempotencia; se a gravacao falhar, nada e enviado)
        self._registrar("intencao_antes_envio", {"intencao_id": intencao_id, "requisicao": dict(req)})
        self._enviadas.add(intencao_id)
        self.estados[intencao_id] = EstadoOrdem.ENVIADA
        self.orcamento.registrar()
        t_ns = self.relogio.agora_ns()
        t0 = t_ns // 1_000_000
        self.envios += 1
        res = self.t.order_send(req)
        t1 = self._agora_ms()
        rc = None if res is None else int(res.retcode)
        if estado_do_retcode(rc) == EstadoOrdem.INCERTA:
            self.estados[intencao_id] = EstadoOrdem.INCERTA
            self._incertas[intencao_id] = {
                "t_ns": t_ns, "t_srv_ms": self.rs.utc_ns_para_servidor_ms(t_ns), "retcode": rc,
                "tipo": (meta or {}).get("tipo", req.get("action")), "lado": (meta or {}).get("lado"),
                "magic": req.get("magic", 0), "ticket": (meta or {}).get("ticket"), "acao": req["action"],
            }
            self._alerta("ordem_incerta", {"intencao_id": intencao_id, "retcode": rc})
        else:
            self._tratar_retcode(rc, intencao_id)
        return res, t0, t1

    def _tratar_retcode(self, rc: int | None, intencao_id: str) -> None:
        if rc == C.RET_MUITAS_REQUISICOES:  # 10024: recuo exponencial, sem laco
            self._recuos += 1
            espera = min(self.cfg.recuo_max_s, self.cfg.recuo_base_s * 2 ** (self._recuos - 1))
            self._recuo_ate_ns = self.relogio.agora_ns() + int(espera * 1e9)
            self._alerta("recuo_10024", {"intencao_id": intencao_id, "espera_s": espera})
        elif rc == C.RET_ALGO_DESLIGADO_CLIENTE:  # 10027
            self._alerta("algotrading_desligado", {"intencao_id": intencao_id})
        elif rc in (C.RET_EXECUTADA, C.RET_PARCIAL, C.RET_COLOCADA):
            self._recuos = 0

    def _ack(self, res: Any, t0: int, t1: int) -> AckOrdem:
        if res is None:
            return AckOrdem(retcode=None, t_setup_msc=t0, t_done_msc=t1,
                            comentario="INCERTA: order_send devolveu None; nao reenviar, reconciliar")
        rc = int(res.retcode)
        com = str(getattr(res, "comment", ""))
        if estado_do_retcode(rc) == EstadoOrdem.INCERTA:
            com = f"INCERTA: retcode {rc}; nao reenviar, reconciliar. {com}"
        return AckOrdem(retcode=rc, order=int(getattr(res, "order", 0) or 0), deal=int(getattr(res, "deal", 0) or 0),
                        t_setup_msc=t0, t_done_msc=t1, preco=float(getattr(res, "price", 0.0) or 0.0), comentario=com)

    # ------------------------------------------------------------------------------------------
    # conferencia pos-deal (netting)
    # ------------------------------------------------------------------------------------------
    def _conferir_apos_deal(self, cmd: ComandoOrdem, antes: float, ack: AckOrdem, corretivo: bool) -> None:
        try:
            ticket, depois, _ = self._posicao()
        except _Recusa:
            self._alerta("sinal_nao_conferido", {"intencao_id": cmd.intencao_id})
            self.travado = "posicao nao pode ser conferida apos deal"
            return
        violou = ""
        if cmd.tipo == "saida":
            if depois * antes < 0:
                violou = "inversao_netting"
            elif abs(depois) > abs(antes) + 1e-9:
                violou = "saida_aumentou_posicao"
        else:
            esperado = 1 if cmd.lado == Lado.COMPRA else -1
            if depois * esperado < 0 or abs(depois) > self.cfg.contratos_max + 1e-9:
                violou = "posicao_inesperada_apos_entrada"
        if not violou:
            return
        self.travado = f"{violou} (antes={antes}, depois={depois})"
        self._alerta(violou, {"intencao_id": cmd.intencao_id, "antes": antes, "depois": depois})
        if not corretivo and ticket is not None:
            lado = Lado.VENDA if depois > 0 else Lado.COMPRA
            corr = ComandoOrdem(intencao_id=f"corretiva-{cmd.intencao_id}", magic=self.cfg.magic_guardiao, lado=lado,
                                tipo="saida", quantidade=max(1, int(abs(depois))), position_ticket=ticket)
            try:
                self.verificar_guarda()
                self._enviar(corr, "guardiao", True, True, corretivo=True)
            except _Recusa as r:
                self._alerta("saida_corretiva_recusada", {"motivo": r.ack.comentario})

    # ------------------------------------------------------------------------------------------
    # reconciliacao de ordens INCERTAS
    # ------------------------------------------------------------------------------------------
    def reconciliar_incertas(self) -> dict[str, EstadoOrdem]:
        """Resolve ordens INCERTAS por evidencia (deal EXPERT com o mesmo magic / ordem pendente) ou por prazo.

        Nunca reenvia. Retorna {intencao_id: estado final} apenas das resolvidas nesta chamada.
        """
        if not self._incertas:
            return {}
        agora_ns = self.relogio.agora_ns()
        t_min = min(i["t_ns"] for i in self._incertas.values()) - 5_000_000_000
        deals = self.t.history_deals_get(self.rs.utc_ns_para_dt_servidor(t_min), self.rs.utc_ns_para_dt_servidor(agora_ns + 120_000_000_000))
        ordens = self.t.orders_get(symbol=self._sym)
        if deals is None or ordens is None:
            return {}
        usados: set[int] = set()
        resolvidas: dict[str, EstadoOrdem] = {}
        for iid, inc in sorted(self._incertas.items(), key=lambda kv: kv[1]["t_ns"]):
            if inc["acao"] != C.ACAO_DEAL and inc["acao"] != C.ACAO_PENDING:
                # SLTP/REMOVE incertos: a releitura de posicoes/ordens ja reflete o estado; so o prazo resolve
                if (agora_ns - inc["t_ns"]) / 1e9 >= self.cfg.espera_incerta_s:
                    resolvidas[iid] = EstadoOrdem.CANCELADA
                continue
            achou = next((d for d in deals if int(d.ticket) not in usados and int(d.magic) == inc["magic"]
                          and int(d.reason) == C.RAZAO_EXPERT and int(d.time_msc) >= inc["t_srv_ms"] - 1000), None)
            if achou is not None:
                usados.add(int(achou.ticket))
                resolvidas[iid] = EstadoOrdem.EXECUTADA
                continue
            pend = next((o for o in ordens if int(o.magic) == inc["magic"]
                         and int(o.time_setup_msc) >= inc["t_srv_ms"] - 1000), None)
            if pend is not None:
                resolvidas[iid] = EstadoOrdem.ACEITA
                continue
            if (agora_ns - inc["t_ns"]) / 1e9 >= self.cfg.espera_incerta_s:
                resolvidas[iid] = EstadoOrdem.REJEITADA  # sem evidencia no prazo: tratada como nao executada
                self._alerta("incerta_sem_evidencia", {"intencao_id": iid})
        for iid, est in resolvidas.items():
            self.estados[iid] = est
            del self._incertas[iid]
            self._registrar("incerta_resolvida", {"intencao_id": iid, "estado": est.value})
        return resolvidas

    def restaurar_enviadas(self, ids: set[str]) -> None:
        """Recarrega intencoes ja enviadas (lidas da auditoria) para nunca reenviar apos reinicio."""
        self._enviadas |= set(ids)

    def liberar_dono_saida(self, ticket: int) -> None:
        self._donos.pop(ticket, None)

    def destravar(self, motivo: str) -> None:
        if not motivo.strip():
            raise ValueError("destravar exige motivo (vai para a auditoria)")
        self._registrar("destravar", {"motivo": motivo, "estava": self.travado})
        self.travado = None


class ExecucaoReal:
    """Ponto de extensao para ordens REAIS: NAO implementado. Sempre levanta `ExecucaoRealNaoAutorizada`.

    A habilitacao exigiria etapa aprovada, configuracao assinada e autorizacao expressa (plano 7.2).
    """

    def enviar(self, cmd: ComandoOrdem) -> AckOrdem:
        raise ExecucaoRealNaoAutorizada("ordens reais nao estao implementadas nem autorizadas nesta versao")
