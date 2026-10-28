"""Ponte do market making com a conta DEMO (atras da flag ``[mm].demo_mm``; SOMENTE demo, nenhuma ordem real).

As mensagens do ``CotadorMM`` (nova / modificacao / cancelamento) viram comandos ao gateway (unico dono de
``order_send``):

* nova  -> ``ComandoOrdem`` LIMITADA com SL obrigatorio, via ``Roteador(modo="gateway_demo")`` (mesmo veto de
  risco, OMS e ack sincrono; INCERTA em ``None``/10012/10031 sem reenvio);
* modificacao -> ``modificar_ordem`` (uma unica mensagem, preco + SL); cancelamento -> ``cancelar_ordem``.

Orcamento de mensagens e tempo minimo no livro (2 s) ja vivem no ``CotadorMM`` + ``risco/antimanipulacao`` (unico
contador de mensagens: o ``Roteador`` NAO recebe o monitor para nao contar duas vezes). A ponte repete as duas
travas como defesa em profundidade (janela deslizante de 60 s proprio e tempo desde a ultima colocacao/alteracao)
e o gateway ainda tem a sua franquia. Uma mensagem recusada por qualquer camada deixa o cotador e a demo
alinhados via ``CotadorMM.descartar`` (nunca "supoe" uma cotacao que nao existe).

Regras duras
* ESPELHAMENTO EXCLUSIVO: adquire a ``TravaEspelho`` (um braco do nucleo OU o MM, nunca os dois);
* exige a capacidade ``livro`` do terminal (``mm.exigencias``) e o gateway em ``modo_mm`` (uma limitada por lado,
  a limitada pode REDUZIR a posicao oposta; exposicao maxima 1 contrato);
* kill switch/EMERGENCIA, perda do heartbeat do gateway, dado velho ou divergencia => cancela as cotacoes e/ou
  manda EMERGENCIA ao gateway (que cancela, zera e trava).
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from ..core import calendario_b3
from ..core.config import ErroConfig
from ..core.instrumentos import PerfilInstrumento
from ..core.relogio import Relogio
from ..core.tipos import Acao, ConfigRisco, EstadoOrdem, Execucao, Instrumento, Intencao, Posicao
from ..execucao.oms import OMS
from ..execucao.politicas import PoliticaPassiva
from ..execucao.ponte_gateway import (
    CanalComandos,
    EmissorGateway,
    EspelhamentoNaoExclusivo,
    TerminalViaGateway,
    TravaEspelho,
    ack_de_resposta,
)
from ..execucao.roteador import Roteador
from ..mt5 import constantes as C
from ..mt5.contrato import listar_contratos
from ..risco.kill_switch import EventoKill, KillSwitch
from ..risco.motor import EventoAgenda, MotorRiscoPadrao
from .cotacao import CotadorMM, Mensagem
from .exigencias import CapacidadeAusente, exigir_capacidade_livro

NS = 1_000_000_000
BRACO_BID, BRACO_ASK, BRACO_GUARDIAO = "mm_bid", "mm_ask", "guardiao"
DONO = "mm"


class MMDemoRecusado(ErroConfig):
    """A ponte do MM com a demo nao pode ser ligada."""


def validar_config_mm(cfg: dict[str, Any]) -> None:
    """Regras de CONFIGURACAO: so simulacao + conta demo; EXCLUSIVO com ``[geral].espelhar``."""
    g = cfg.get("geral", {})
    if str(g.get("conta", "")) != "demo_mt5":
        raise MMDemoRecusado(f"[geral].conta = '{g.get('conta', '')}': a ponte do MM so existe com 'demo_mt5'")
    if str(g.get("modo", "simulacao")) != "simulacao":
        raise MMDemoRecusado(f"[geral].modo = '{g.get('modo')}': a ponte do MM so existe em 'simulacao' (demo)")
    if str(g.get("espelhar", "") or "").strip():
        raise MMDemoRecusado(f"[mm].demo_mm = true e [geral].espelhar = '{g.get('espelhar')}': espelhamento EXCLUSIVO "
                             "(um unico braco/estrategia por vez na conta demo)")


@dataclass
class ConfigPonteMM:
    stop_pontos: float  # distancia do SL das cotacoes (obrigatorio; em pontos de preco)
    magic_bid: int = 3001
    magic_ask: int = 3002
    magic_guardiao: int = 990001
    timeout_s: float = 2.0
    estado_max_idade_s: float = 5
    espera_incerta_s: float = 5
    validade_s: float = 30.0


@dataclass
class _Quote:
    ordem_id: str  # id do cotador
    preco: float
    t_ns: int  # ultima colocacao/alteracao (tempo minimo no livro conta daqui)
    ticket: int | None = None  # None => INCERTA (ack perdido), resolvida na sincronizacao
    oms_id: str = ""
    t_envio_ns: int = 0
    verificar_preco: bool = False  # alteracao sem resposta: confere o preco real no proximo snapshot


@dataclass
class _Orfa:
    ticket: int
    visto_ns: int


class PonteMMDemo:
    def __init__(
        self, cotador: CotadorMM, instrumento: Instrumento, perfil: PerfilInstrumento, canal: CanalComandos,
        relogio: Relogio, risco: ConfigRisco, cfg: ConfigPonteMM, *, eventos: tuple[EventoAgenda, ...] = (),
        arquivo_emergencia: Any = None, trava: TravaEspelho | None = None, registro: Any = None,
    ) -> None:
        if cfg.stop_pontos <= 0:
            raise MMDemoRecusado("stop_pontos > 0 e obrigatorio: toda cotacao leva SL")
        self.cotador, self.ins, self.perfil, self.canal, self.rel = cotador, instrumento, perfil, canal, relogio
        self.cfg, self.trava, self.registro = cfg, trava, registro
        self.kill = KillSwitch(arquivo_flag=arquivo_emergencia)
        self.kill.registrar_efeito(self._efeito_kill)
        # cada cotacao nova conta como "operacao" no veto de cadencia: o teto do MM e o orcamento de mensagens da sessao
        risco = risco.model_copy(update={"operacoes_dia_max": max(risco.operacoes_dia_max, cotador.cfg.mensagens_sessao_max)})
        self.motor = MotorRiscoPadrao(risco, instrumento, kill_switch=self.kill, eventos=eventos,
                                      grade=lambda d: calendario_b3.sessao_em(d, perfil))
        magics = {BRACO_BID: cfg.magic_bid, BRACO_ASK: cfg.magic_ask, BRACO_GUARDIAO: cfg.magic_guardiao}
        self.oms = OMS(relogio, magics, banco=registro.banco if registro is not None else None, simulada=False)
        if registro is not None:
            self.oms.ao_transicao.append(lambda o: registro.ordem(o, simulada=False))
        self.emissor = EmissorGateway(canal, dono=DONO, timeout_s=cfg.timeout_s)
        self.roteador = Roteador("gateway_demo", instrumento, relogio, self.oms, self.motor, emissor_gateway=self.emissor,
                                 politica=PoliticaPassiva(0.0), bloqueio_externo=self._bloqueio,
                                 exigir_aprovacao=True)
        self.terminal = TerminalViaGateway(canal, desde_ns=relogio.agora_ns() - 60 * NS, timeout_s=cfg.timeout_s)
        self.simbolo_gw = ""
        self.ativo = False
        self.estado_gw: dict[str, Any] = {}
        self.t_estado_gw = 0.0
        self.vivas: dict[str, _Quote] = {}
        self.estoque = 0
        self.orfas: dict[int, _Orfa] = {}
        self.fills: list[tuple[str, int, float]] = []  # (lado, t_ns, preco)
        self.eventos: list[tuple[str, str]] = []
        self.historico: list[dict[str, Any]] = []  # mensagens efetivamente enviadas ao gateway (auditoria dos testes)
        self._janela: deque[int] = deque()
        self._falhas = 0
        self._deals_vistos: set[int] = set()
        self.pausado_por = ""
        self._emergencia_enviada = False

    # ============================================================================ inicio / fim ============
    def iniciar(self) -> dict[str, Any]:
        if self.trava is not None:
            try:
                self.trava.adquirir("mm")
            except EspelhamentoNaoExclusivo as e:
                raise MMDemoRecusado(str(e)) from e
        try:
            est = self.canal.enviar({"tipo": "estado"}, timeout_s=max(self.cfg.timeout_s, 3.0))
            if est is None or est.get("tipo") != "estado":
                raise MMDemoRecusado("gateway nao respondeu ao estado: nada e cotado sem gateway")
            conta = est.get("conta") or {}
            if conta.get("trade_mode") != "demo":
                raise MMDemoRecusado(f"conta {conta.get('trade_mode')} (login {conta.get('login')}): MM so em conta DEMO")
            try:
                exigir_capacidade_livro(est)
            except CapacidadeAusente as e:
                raise MMDemoRecusado(str(e)) from e
            if not est.get("modo_mm"):
                raise MMDemoRecusado("gateway sem modo_mm: ele recusaria a segunda cotacao/limitada que reduz posicao")
            if est.get("parado") or (est.get("guardiao") or {}).get("travado_emergencia"):
                raise MMDemoRecusado("gateway parado ou travado por emergencia")
            sim = str(est.get("simbolo") or "")
            if not listar_contratos([sim], self.perfil):
                raise MMDemoRecusado(f"simbolo do gateway '{sim}' nao e do ativo {self.perfil.simbolo_base}")
            if not self.terminal.atualizar():
                raise MMDemoRecusado("gateway nao entregou o snapshot")
            if self.terminal.positions_get() or self.terminal.orders_get():
                raise MMDemoRecusado("a conta demo tem posicao ou ordem aberta: zere antes de ligar o MM")
            self.simbolo_gw = sim
            self.estado_gw, self.t_estado_gw, self.ativo = est, time.monotonic(), True
        except Exception:
            if self.trava is not None:
                self.trava.liberar()
            raise
        self._evento("mm_demo_ligado", f"simbolo={sim} conta={conta.get('login')}")
        return est

    def encerrar(self, t_ns: int) -> None:
        """Cancela as cotacoes (por seguranca, mesmo antes do tempo minimo) e libera a trava de exclusividade."""
        if self.ativo:
            self.cancelar_cotacoes("encerramento", t_ns, forcar=True)
        self.ativo = False
        if self.trava is not None:
            self.trava.liberar()

    def _evento(self, tipo: str, detalhe: str, t_ns: int | None = None, critico: bool = False) -> None:
        self.eventos.append((tipo, detalhe))
        if self.registro is not None:
            self.registro.evento_risco(t_ns=self.rel.agora_ns() if t_ns is None else t_ns, braco="mm", tipo=tipo,
                                       detalhe=detalhe[:400], critico=critico)

    # ============================================================================ estado do gateway =======
    def ao_estado_gateway(self, d: dict[str, Any]) -> None:
        self.estado_gw, self.t_estado_gw = d, time.monotonic()

    def _bloqueio(self) -> str:
        if not self.ativo:
            return "mm_desligado"
        if self.pausado_por:
            return f"mm_pausado:{self.pausado_por}"
        if time.monotonic() - self.t_estado_gw > self.cfg.estado_max_idade_s:
            return "gateway_sem_estado"
        if self.estado_gw.get("parado"):
            return "gateway_parado"
        if (self.estado_gw.get("guardiao") or {}).get("travado_emergencia"):
            return "gateway_travado"
        if self._falhas >= 3:
            return "gateway_mudo"
        return ""

    # ============================================================================ orcamento (defesa em profundidade) =
    def _limite_msgs(self) -> int:
        return min(self.cotador.cfg.mensagens_min_max, self.cotador.monitor.limites.mensagens_min_max)

    def _cabe(self, t_ns: int) -> bool:
        while self._janela and self._janela[0] <= t_ns - 60 * NS:
            self._janela.popleft()
        return len(self._janela) < self._limite_msgs()

    def _tempo_minimo_ok(self, q: _Quote, t_ns: int) -> bool:
        return t_ns - q.t_ns >= int(self.cotador.monitor.limites.tempo_min_livro_s * NS)

    # ============================================================================ aplicacao das mensagens ==
    def aplicar(self, t_ns: int, mensagens: list[Mensagem] | None = None) -> list[str]:
        """Aplica as mensagens do cotador no gateway. Devolve o resultado de cada uma (observabilidade)."""
        out: list[str] = []
        for m in list(self.cotador.ultimas_mensagens if mensagens is None else mensagens):
            if m.tipo == "nova":
                out.append(self._nova(m, t_ns))
            elif m.tipo == "modificacao":
                out.append(self._modificar(m, t_ns))
            else:
                out.append(self._cancelar(m, t_ns))
        return out

    def _recusa(self, m: Mensagem, motivo: str, t_ns: int, descartar: bool = True) -> str:
        self._evento("mm_mensagem_recusada", f"{m.tipo} {m.lado} {m.preco}: {motivo}", t_ns)
        if descartar:
            self.cotador.descartar(m.lado)  # o cotador nao pode supor uma cotacao que nao existe
            self.vivas.pop(m.lado, None)
        return f"recusada:{motivo}"

    def _nova(self, m: Mensagem, t_ns: int) -> str:
        assert m.preco is not None
        if (b := self._bloqueio()):
            return self._recusa(m, b, t_ns)
        if not self._cabe(t_ns):
            return self._recusa(m, "orcamento_da_ponte", t_ns)
        compra = m.lado == "bid"
        it = Intencao(id=f"mm-{m.ordem_id}-{t_ns}", braco=BRACO_BID if compra else BRACO_ASK,
                      acao=Acao.COMPRAR if compra else Acao.VENDER, quantidade=1, stop_pontos=self.cfg.stop_pontos,
                      validade_ns=t_ns + int(self.cfg.validade_s * NS), preco_referencia=m.preco, t0_ns=t_ns)
        # a posicao passada ao veto e SEMPRE zerada: o sentido (abrir x reduzir) e imposto pelo executor em modo MM
        r = self.roteador.executar_intencao(it, Posicao(simbolo=self.simbolo_gw), t_ns, {"spread_ticks": 99.0})
        if not r.decisao.aprovada:
            return self._recusa(m, "vetada:" + ";".join(r.decisao.motivos), t_ns)
        env = r.envio
        if env is None or not env.enviado:
            return self._recusa(m, r.motivo or "nao_enviada", t_ns)
        self._janela.append(t_ns)
        self.historico.append({"tipo": "nova", "lado": m.lado, "preco": m.preco, "t_ns": t_ns})
        estado = env.ordem.estado
        if estado == EstadoOrdem.ACEITA and env.ack is not None and env.ack.order:
            self.vivas[m.lado] = _Quote(m.ordem_id, m.preco, t_ns, ticket=env.ack.order, oms_id=env.ordem.id, t_envio_ns=t_ns)
            return "ok"
        if estado == EstadoOrdem.INCERTA:
            self.vivas[m.lado] = _Quote(m.ordem_id, m.preco, t_ns, ticket=None, oms_id=env.ordem.id, t_envio_ns=t_ns)
            self._evento("mm_nova_incerta", f"{m.lado} {m.preco}: sem ack; nunca reenvia, a sincronizacao resolve",
                         t_ns, critico=True)
            return "incerta"
        if estado in (EstadoOrdem.EXECUTADA, EstadoOrdem.PARCIAL):  # limitada que ja cruzou (raro): vira fill
            self._registrar_fill(m.lado, t_ns, m.preco, env.ordem.id)
            return "executada"
        return self._recusa(m, f"{estado.value}:{env.ack.comentario if env.ack else ''}", t_ns)

    def _sl(self, lado: str, preco: float) -> float:
        return preco - self.cfg.stop_pontos if lado == "bid" else preco + self.cfg.stop_pontos

    def _modificar(self, m: Mensagem, t_ns: int) -> str:
        assert m.preco is not None
        q = self.vivas.get(m.lado)
        if q is None or q.ticket is None:
            return self._recusa(m, "cotacao_inexistente_na_demo", t_ns)
        if not self._tempo_minimo_ok(q, t_ns):
            return self._recusa(m, "tempo_minimo_no_livro", t_ns, descartar=False)
        if not self._cabe(t_ns):
            return self._recusa(m, "orcamento_da_ponte", t_ns, descartar=False)
        if (b := self._bloqueio()):
            return self._recusa(m, b, t_ns, descartar=False)
        self._janela.append(t_ns)
        self.historico.append({"tipo": "modificacao", "lado": m.lado, "preco": m.preco, "t_ns": t_ns, "ticket": q.ticket})
        resp = self.canal.enviar({"tipo": "modificar_ordem", "ticket": q.ticket, "preco": m.preco,
                                  "sl": self._sl(m.lado, m.preco), "dono": DONO}, timeout_s=self.cfg.timeout_s)
        ack = ack_de_resposta(resp)
        if ack is None:  # resultado desconhecido: nao reenvia; o preco real sai do proximo snapshot
            q.verificar_preco, q.t_ns = True, t_ns
            self._evento("mm_modificacao_incerta", f"{m.lado} {m.preco}", t_ns, critico=True)
            return "incerta"
        if ack.retcode == C.RET_EXECUTADA:
            q.preco, q.t_ns = m.preco, t_ns
            return "ok"
        self._evento("mm_modificacao_recusada", f"{m.lado} {m.preco}: retcode={ack.retcode} {ack.comentario}", t_ns)
        # a demo manteve o preco antigo: realinha o cotador ao que existe de fato
        v = self.cotador.vivas.get(m.lado)
        if v is not None:
            v.preco = q.preco
        return f"recusada:{ack.retcode}"

    def _cancelar(self, m: Mensagem, t_ns: int, forcar: bool = False) -> str:
        q = self.vivas.get(m.lado)
        if q is None or q.ticket is None:
            self.vivas.pop(m.lado, None)
            return "sem_cotacao_na_demo"
        if not forcar:
            if not self._tempo_minimo_ok(q, t_ns):
                return self._recusa(m, "tempo_minimo_no_livro", t_ns, descartar=False)
            if not self._cabe(t_ns):
                return self._recusa(m, "orcamento_da_ponte", t_ns, descartar=False)
        self._janela.append(t_ns)
        self.historico.append({"tipo": "cancelamento", "lado": m.lado, "t_ns": t_ns, "ticket": q.ticket})
        resp = self.canal.enviar({"tipo": "cancelar_ordem", "ticket": q.ticket, "dono": DONO}, timeout_s=self.cfg.timeout_s)
        ack = ack_de_resposta(resp)
        if ack is not None and ack.retcode == C.RET_EXECUTADA:
            self.oms.ao_cancelada(q.ticket)
            self.vivas.pop(m.lado, None)
            return "ok"
        # sem resposta/recusada: a ordem pode continuar viva; a sincronizacao confere (nunca reenvia as cegas)
        self._evento("mm_cancelamento_nao_confirmado", f"{m.lado} ticket={q.ticket}: "
                     f"{'sem resposta' if ack is None else ack.comentario}", t_ns, critico=True)
        return "incerta" if ack is None else f"recusada:{ack.retcode}"

    def cancelar_cotacoes(self, motivo: str, t_ns: int, forcar: bool = False) -> None:
        """Cancela as cotacoes vivas (respeita o tempo minimo no livro, exceto quando ``forcar`` por seguranca)."""
        if not self.vivas:
            return
        for lado in list(self.vivas):
            r = self._cancelar(Mensagem("cancelamento", lado, self.vivas[lado].ordem_id, None, t_ns), t_ns, forcar=forcar)
            v = self.cotador.vivas.get(lado)
            if r == "ok" and v is not None:  # cancelamento iniciado pela ponte (nao pelo cotador): o cotador esquece
                self.cotador.vivas.pop(lado, None)
                self.cotador.monitor.registrar_cancelamento(v.ordem_id, t_ns)
        self._evento("mm_cotacoes_canceladas", motivo, t_ns)

    # ============================================================================ sincronizacao / fills ====
    def _registrar_fill(self, lado: str, t_ns: int, preco: float, oms_id: str = "") -> None:
        self.cotador.registrar_execucao(lado, t_ns)
        self.fills.append((lado, t_ns, preco))
        q = self.vivas.pop(lado, None)
        oid = oms_id or (q.oms_id if q else "")
        if oid:
            o = self.oms.ordem(oid)
            if o.ticket:
                self.oms.ao_execucao(o.ticket, Execucao(ordem_id=oid, preco=preco, quantidade=1, t_ns=t_ns, simulada=False))
        self._evento("mm_fill", f"{lado} @ {preco}", t_ns)

    def sincronizar(self, t_ns: int) -> bool:
        """Snapshot da demo: detecta fills, cotacoes perdidas, ordens orfas, intervencao manual e o estoque real."""
        if not self.terminal.atualizar():
            self._falhas += 1
            return False
        self._falhas = 0
        sym = self.simbolo_gw
        ordens = {int(o.ticket): o for o in self.terminal.orders_get(symbol=sym) or ()}
        deals = list(self.terminal.history_deals_get() or ())
        for d in deals:  # intervencao manual: pausa o MM (alerta + bloqueio)
            if int(d.ticket) in self._deals_vistos:
                continue
            self._deals_vistos.add(int(d.ticket))
            if int(d.reason) in C.RAZOES_MANUAIS:
                self.pausado_por = "intervencao_manual"
                self._evento("mm_intervencao_manual", f"deal {d.ticket} reason={C.NOMES_RAZAO.get(int(d.reason))}", t_ns, True)
        for lado, q in list(self.vivas.items()):
            if q.ticket is None:
                self._resolver_incerta(lado, q, ordens, deals, t_ns)
            elif q.ticket not in ordens:
                fill = next((x for x in deals if int(x.order) == q.ticket and int(x.entry) in (C.ENTRADA_IN, C.ENTRADA_OUT)), None)
                if fill is not None:
                    self._registrar_fill(lado, t_ns, float(fill.price))
                else:
                    self._evento("mm_cotacao_sumiu", f"{lado} ticket={q.ticket} sem deal (cancelada/expirada fora da ponte)", t_ns, True)
                    self.cotador.descartar(lado)
                    self.vivas.pop(lado, None)
            elif q.verificar_preco:
                real = float(ordens[q.ticket].price_open)
                q.preco, q.verificar_preco = real, False
                v = self.cotador.vivas.get(lado)
                if v is not None:
                    v.preco = real
        self._limpar_orfas(ordens, t_ns)
        sinal = {C.POS_COMPRA: 1, C.POS_VENDA: -1}
        self.estoque = int(round(sum(sinal.get(int(p.type), 0) * float(p.volume)
                                     for p in self.terminal.positions_get(symbol=sym) or ())))
        return True

    def _resolver_incerta(self, lado: str, q: _Quote, ordens: dict[int, Any], deals: list[Any], t_ns: int) -> None:
        magic = self.cfg.magic_bid if lado == "bid" else self.cfg.magic_ask
        ms_envio = q.t_envio_ns // 1_000_000 - 1000
        o = next((x for x in ordens.values() if int(x.magic) == magic and int(x.time_setup_msc) >= ms_envio), None)
        if o is not None:
            q.ticket = int(o.ticket)
            self.oms.resolver_incerta(q.oms_id, "aceita", ticket=q.ticket)
            self._evento("mm_incerta_resolvida", f"{lado}: ordem encontrada ticket={q.ticket}", t_ns)
            return
        d = next((x for x in deals if int(x.magic) == magic and int(x.time_msc) >= ms_envio and int(x.reason) == C.RAZAO_EXPERT), None)
        if d is not None:
            self.oms.resolver_incerta(q.oms_id, "executada", ticket=int(d.order), quantidade=1, preco=float(d.price))
            self._registrar_fill(lado, t_ns, float(d.price), q.oms_id)
            return
        if (t_ns - q.t_envio_ns) / NS >= self.cfg.espera_incerta_s:
            self.oms.resolver_incerta(q.oms_id, "inexistente")
            self._evento("mm_incerta_sem_evidencia", f"{lado}: tratada como nao colocada", t_ns, True)
            self.cotador.descartar(lado)
            self.vivas.pop(lado, None)

    def _limpar_orfas(self, ordens: dict[int, Any], t_ns: int) -> None:
        """Ordem pendente com magic do MM que a ponte nao acompanha (ack perdido): cancela apos o tempo minimo."""
        meus = {self.cfg.magic_bid, self.cfg.magic_ask}
        acompanhadas = {q.ticket for q in self.vivas.values() if q.ticket is not None}
        pendentes_incertas = any(q.ticket is None for q in self.vivas.values())
        for tk, o in ordens.items():
            if int(o.magic) not in meus or tk in acompanhadas or pendentes_incertas:
                continue
            ft = self.orfas.setdefault(tk, _Orfa(tk, t_ns))
            if t_ns - ft.visto_ns >= int(self.cotador.monitor.limites.tempo_min_livro_s * NS):
                self._evento("mm_ordem_orfa_cancelada", f"ticket={tk}", t_ns, True)
                self._janela.append(t_ns)
                self.canal.enviar({"tipo": "cancelar_ordem", "ticket": tk, "dono": DONO}, timeout_s=self.cfg.timeout_s)
                self.orfas.pop(tk, None)

    # ============================================================================ kill / emergencia ========
    def verificar_kill(self, t_ns: int) -> bool:
        return self.kill.verificar_flag(t_ns)

    def _efeito_kill(self, ev: EventoKill) -> None:
        self.pausado_por = "kill_switch"
        self.emergencia(f"{ev.origem}: {ev.motivo}", ev.t_ns)

    def emergencia(self, motivo: str, t_ns: int) -> dict[str, Any] | None:
        resp = self.canal.enviar({"tipo": "emergencia", "origem": f"mm: {motivo}"[:120]}, timeout_s=max(self.cfg.timeout_s, 5))
        self._emergencia_enviada = resp is not None
        self._evento("mm_emergencia", f"{motivo}: {'sem confirmacao' if resp is None else resp.get('confirmado')}", t_ns, True)
        self.vivas.clear()
        return resp

    def resumo(self) -> dict[str, Any]:
        c = self.cotador.contagem()
        return {"ativo": self.ativo, "pausado_por": self.pausado_por, "estoque": self.estoque, "fills": len(self.fills),
                "mensagens_cotador": c, "enviadas_ao_gateway": len(self.historico), "vivas": sorted(self.vivas)}

