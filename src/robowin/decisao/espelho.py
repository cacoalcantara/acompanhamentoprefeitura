"""Espelhamento de UM braco na conta DEMO do MT5 (SOMENTE simulacao/demo; nenhuma ordem real).

O braco escolhido continua operando no simulador interno (fonte da verdade do experimento). O espelho copia
as operacoes dele para a conta demo, SEMPRE pelo gateway (unico dono de ``order_send``):

* entrada: quando a posicao simulada ABRE, o espelho (1) exige a conta demo ZERADA (sem posicao e sem ordens),
  (2) passa a intencao pelo MESMO veto de risco (``MotorRiscoPadrao`` com a mesma configuracao e o MESMO kill
  switch do braco), pelo OMS e pelo ``Roteador(modo="gateway_demo")`` e (3) envia ordem a mercado com SL obrigatorio
  (e alvo) nos mesmos niveis do simulador. O ack e SINCRONO; ``None``/10012/10031 => INCERTA, sem reenvio;
* saida: UM dono por posicao. Saidas por tempo/ENCERRAR/zeragem/kill (``Orquestrador._sair``) e o fechamento da
  posicao simulada (stop/alvo) acionam a MESMA saida do espelho, com ``position=<ticket>``; a reivindicacao e por
  ticket (a segunda tentativa e recusada). O SL do servidor protege a demo mesmo se o nucleo cair;
* reconciliacao periodica: um snapshot atomico (positions/orders/deals do gateway) alimenta o ``Reconciliador``
  (deals classificados por ``reason``: SL/TP/SO = esperado; EXPERT = robo; CLIENT/MOBILE/WEB = intervencao
  manual). Divergencia => bloqueio de entradas (e kill switch do braco, como no resto do projeto); intervencao
  manual => alerta + bloqueio ate o reconhecimento humano.

Limites conhecidos: o envio e o snapshot sao requisicoes sincronas ao gateway (``timeout_s``) feitas no laco do
nucleo; ordens limitadas/passivas do simulador NAO sao espelhadas (o espelho agride a mercado); o fill da demo
e o do simulador diferem (a demo nao tem fila nem profundidade reais): a demo mede encanamento, nao lucro.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from robowin.core import calendario_b3
from robowin.core.config import ErroConfig
from robowin.core.tipos import Acao, AckOrdem, EstadoOrdem, Intencao, Lado, Posicao
from robowin.execucao.oms import OMS, TERMINAIS
from robowin.execucao.ponte_gateway import (
    CanalComandos,
    EmissorGateway,
    EspelhamentoNaoExclusivo,
    TerminalViaGateway,
    TravaEspelho,
)
from robowin.execucao.reconciliacao import Reconciliador, ResultadoReconciliacao
from robowin.execucao.roteador import Roteador
from robowin.mt5 import constantes as C
from robowin.mt5.contrato import listar_contratos
from robowin.risco.motor import MotorRiscoPadrao

NS = 1_000_000_000
ORIGEM = "espelho"


class EspelhoRecusado(ErroConfig):
    """O espelhamento nao pode ser ligado (conta nao demo, gateway mudo, conta nao zerada, outro espelho ativo...)."""


def validar_config_espelhamento(cfg: dict[str, Any], espelhar: str, *, canal_ok: bool) -> None:
    """Regras de CONFIGURACAO do espelhamento (falha segura): so simulacao + conta demo, com canal de comandos e
    EXCLUSIVO (ou espelha um braco ou o market making da demo, nunca os dois)."""
    g = cfg.get("geral", {})
    if str(g.get("conta", "")) != "demo_mt5":
        raise EspelhoRecusado(f"[geral].conta = '{g.get('conta', '')}': o espelhamento so existe com 'demo_mt5'")
    if str(g.get("modo", "simulacao")) != "simulacao":
        raise EspelhoRecusado(f"[geral].modo = '{g.get('modo')}': o espelhamento so existe em 'simulacao' (demo)")
    if not canal_ok:
        raise EspelhoRecusado("espelhar exige o canal de comandos do gateway (--cmd) e o heartbeat ligado")
    if (cfg.get("mm") or {}).get("demo_mm"):
        raise EspelhoRecusado(f"[geral].espelhar = '{espelhar}' e [mm].demo_mm = true: espelhamento EXCLUSIVO "
                              "(um unico braco/estrategia por vez na conta demo)")


@dataclass
class ConfigEspelho:
    braco: str  # id do braco espelhado ([geral].espelhar)
    magic: int = 2001  # magic das ordens do espelho (distinto dos magics do simulador e do guardiao)
    magic_guardiao: int = 990001  # magic das saidas do guardiao (zeragem/emergencia): legitimas, nao sao divergencia
    timeout_s: float = 2.0  # espera do ack sincrono / do snapshot
    reconciliar_a_cada_s: float = 5
    estado_max_idade_s: float = 5  # estado do gateway mais velho que isto bloqueia entradas
    espera_incerta_s: float = 5
    falhas_gateway_max: int = 3  # snapshots seguidos sem resposta => bloqueia entradas
    validade_entrada_s: float = 30.0


@dataclass
class ResultadoEspelho:
    acao: str  # entrada | saida
    enviado: bool
    motivo: str = ""
    estado: str = ""
    ack: AckOrdem | None = None


class MonitorGateway:
    """Grava o estado/heartbeat do gateway em ``incidentes``/``eventos_risco`` e CONFIRMA o kill switch ponta a ponta.

    * ``incidentes(origem='gateway_estado')``: gateway online e mudancas de estado (o painel usa isto para saber que
      HA um gateway reportando; com ele a confirmacao do kill switch passa a exigir a posicao zerada);
    * ``eventos_risco(tipo='gateway_estado')``: resumo (posicoes, ordens, travado, bloqueios) a cada mudanca ou 60 s;
    * ``incidentes(origem='gateway', detalhe='emergencia_confirmada...')``: o gateway esta TRAVADO e reportou
      posicao zero e nenhuma ordem pendente. Reemitido quando o arquivo EMERGENCIA e mais novo que a ultima
      confirmacao ou a cada ``confirmacao_a_cada_s`` (o painel procura uma confirmacao POSTERIOR a flag).
    """

    def __init__(self, registro: Any, *, arquivo_flag: Any = None, intervalo_registro_s: float = 60.0,
                 confirmacao_a_cada_s: float = 30.0) -> None:
        self.registro, self.arquivo_flag = registro, arquivo_flag
        self.intervalo_registro_ns = int(intervalo_registro_s * NS)
        self.confirmacao_ns = int(confirmacao_a_cada_s * NS)
        self._online = False
        self._chave: Any = None
        self._t_registro = 0
        self._t_conf = 0
        self._conf_agora = False
        self._t_conf_wall = 0
        self._mudo = False
        self.ultimo: dict[str, Any] = {}
        self.confirmacoes = 0

    @staticmethod
    def resumo(d: dict[str, Any]) -> dict[str, Any]:
        g = d.get("guardiao") or {}
        posicoes = d.get("posicoes")
        npos = None if posicoes is None else len(posicoes)
        nord = d.get("ordens")
        bloqueios = sorted({str(b).split(":", 1)[0] for b in g.get("bloqueios", [])})
        return {"posicoes": npos, "ordens": nord, "travado": bool(g.get("travado_emergencia")), "bloqueios": bloqueios,
                "heartbeat_nucleo_idade_s": g.get("heartbeat_idade_s"), "parado": d.get("parado"),
                "incertas": len(d.get("incertas") or []), "duble": bool(d.get("duble"))}

    def _mtime_flag(self) -> int:
        try:
            return int(self.arquivo_flag.stat().st_mtime_ns) if self.arquivo_flag is not None else 0
        except OSError:
            return 0

    def ao_estado(self, d: dict[str, Any], t_ns: int) -> None:
        r = self.resumo(d)
        self.ultimo = r
        if self._mudo:
            self._mudo = False
            self.registro.incidente(t_ns=t_ns, origem="gateway_estado", severidade="info",
                                    detalhe="gateway voltou a publicar estado/heartbeat")
        if not self._online:
            self._online = True
            c = d.get("conta") or {}
            self.registro.incidente(
                t_ns=t_ns, origem="gateway_estado", severidade="info",
                detalhe=f"gateway online: conta {c.get('login')}@{c.get('servidor')} ({c.get('trade_mode')}); "
                        f"posicoes={r['posicoes']} ordens={r['ordens']} travado={r['travado']}")
        chave = (r["posicoes"], r["ordens"], r["travado"], tuple(r["bloqueios"]), r["incertas"], bool(r["parado"]))
        if chave != self._chave or t_ns - self._t_registro >= self.intervalo_registro_ns:
            self._chave, self._t_registro = chave, t_ns
            self.registro.evento_risco(t_ns=t_ns, braco="gateway", tipo="gateway_estado",
                                       detalhe=json.dumps(r, sort_keys=True), critico=False)
        zerado = r["posicoes"] == 0 and r["ordens"] == 0
        if r["travado"] and zerado:
            novo = (not self._conf_agora or self._mtime_flag() > self._t_conf_wall
                    or t_ns - self._t_conf >= self.confirmacao_ns)
            if novo:
                self._t_conf, self._t_conf_wall = t_ns, time.time_ns()
                self.confirmacoes += 1
                self.registro.incidente(
                    t_ns=t_ns, origem="gateway", severidade="critico",
                    detalhe="emergencia_confirmada: gateway travado, posicao zerada e sem ordens pendentes "
                            "(posicoes=0 ordens=0)")
                self.registro.evento_risco(t_ns=t_ns, braco="gateway", tipo="gateway_emergencia_confirmada",
                                           detalhe="posicao zero confirmada pelo gateway", critico=True)
            self._conf_agora = True
        else:
            self._conf_agora = False

    def silencio(self, t_ns: int, mudo: bool) -> None:
        """O gateway deixou de enviar heartbeat/estado (ou voltou): um incidente por transicao."""
        if mudo and not self._mudo:
            self._mudo = True
            self.registro.incidente(t_ns=t_ns, origem="gateway_estado", severidade="alerta",
                                    detalhe="sem heartbeat do gateway: entradas bloqueadas (o guardiao zera sozinho)")


class EspelhoDemo:
    """Liga o ``Roteador`` em modo ``gateway_demo`` ao gateway (ZMQ) e espelha UM braco (ver docstring do modulo)."""

    def __init__(self, orq: Any, cfg: ConfigEspelho, canal: CanalComandos, *, trava: TravaEspelho | None = None) -> None:
        conta = orq.contas.get(cfg.braco)
        if conta is None:
            raise EspelhoRecusado(f"[geral].espelhar = '{cfg.braco}': braco inexistente (ha: {sorted(orq.contas)})")
        self.orq, self.cfg, self.canal, self.trava = orq, cfg, canal, trava
        self.registro = orq.registro
        self.rel = orq.rel
        self.braco_demo = f"demo:{cfg.braco}"
        ins = orq.cfg.instrumento
        self.motor = MotorRiscoPadrao(
            orq.cfg.risco, ins, kill_switch=conta.kill, auditoria=orq.registro.auditoria, eventos=orq.eventos,
            grade=lambda d: calendario_b3.sessao_em(d, orq.perfil))
        self.oms = OMS(self.rel, {self.braco_demo: cfg.magic, "guardiao": cfg.magic_guardiao}, banco=orq.registro.banco,
                       auditoria=orq.registro.auditoria, simulada=False)
        self.oms.ao_transicao.append(lambda o: self.registro.ordem(o, simulada=False))
        self.emissor = EmissorGateway(canal, timeout_s=cfg.timeout_s)
        self.terminal = TerminalViaGateway(canal, desde_ns=self.rel.agora_ns() - 60 * NS, timeout_s=cfg.timeout_s)
        self.roteador = Roteador("gateway_demo", ins, self.rel, self.oms, self.motor, emissor_gateway=self.emissor,
                                 bloqueio_externo=self._bloqueio, posicao_fn=self._posicao_gateway)
        self.reconciliador = Reconciliador(
            self.terminal, self.oms, "", self._crenca_de, kill_switch=conta.kill, auditoria=orq.registro.auditoria,
            espera_incerta_s=cfg.espera_incerta_s)
        self.simbolo_gw = ""
        self.crenca = Posicao(simbolo=ins.simbolo)  # o que o espelho acredita ter na demo
        self.estado_gw: dict[str, Any] = {}
        self.t_estado_gw = 0.0
        self.ativo = False
        self.desligado_por = ""
        self.eventos: list[tuple[str, str]] = []  # (tipo, detalhe) - observabilidade para testes/CLI
        self._saidas: dict[int, tuple[str, float, str]] = {}  # ticket -> (motivo, instante, estado) da saida reivindicada
        self._prox_reconc = 0.0
        self._falhas = 0
        self._mudo = False
        self._kill_confirmado = False
        self._prox_kill = 0.0
        self.ultimo_reconc: ResultadoReconciliacao | None = None
        self.deals_vistos = {"robo": 0, "servidor": 0, "manual": 0}  # deals classificados por `reason` (acumulado)
        self.entradas = self.saidas_enviadas = 0
        orq.espelho = self

    def _crenca_de(self, braco: str) -> Posicao:
        return self.crenca if braco == self.braco_demo else Posicao(simbolo=self.crenca.simbolo)

    # ================================================================================== inicio / fim =======
    def iniciar(self) -> dict[str, Any]:
        """Handshake: recusa se a conta nao for DEMO, se o ativo divergir, se ja houver posicao/ordem ou outro espelho."""
        if self.trava is not None:
            try:
                self.trava.adquirir(f"nucleo:{self.cfg.braco}")
            except EspelhamentoNaoExclusivo as e:
                raise EspelhoRecusado(str(e)) from e
        try:
            est = self.canal.enviar({"tipo": "estado"}, timeout_s=max(self.cfg.timeout_s, 3.0))
            if est is None or est.get("tipo") != "estado":
                raise EspelhoRecusado("gateway nao respondeu ao estado: nada e espelhado sem gateway")
            conta = est.get("conta") or {}
            if conta.get("trade_mode") != "demo":
                raise EspelhoRecusado(f"conta {conta.get('trade_mode')} (login {conta.get('login')}): so espelha em conta DEMO")
            if est.get("parado"):
                raise EspelhoRecusado(f"gateway parado: {est.get('parado')}")
            if (est.get("guardiao") or {}).get("travado_emergencia"):
                raise EspelhoRecusado("gateway TRAVADO por emergencia: rearme manualmente antes de espelhar")
            sim = str(est.get("simbolo") or "")
            if not listar_contratos([sim], self.orq.perfil):
                raise EspelhoRecusado(f"simbolo do gateway '{sim}' nao e do ativo {self.orq.ativo}")
            if not self.terminal.atualizar():
                raise EspelhoRecusado("gateway nao entregou o snapshot (positions/orders/deals)")
            if self.terminal.positions_get() or self.terminal.orders_get():
                raise EspelhoRecusado("a conta demo tem posicao ou ordem aberta: zere antes de ligar o espelhamento")
            self.simbolo_gw = sim
            self.reconciliador.simbolo = sim
            self.crenca = Posicao(simbolo=sim)
            self.estado_gw, self.t_estado_gw, self.ativo = est, time.monotonic(), True
        except Exception:
            if self.trava is not None:
                self.trava.liberar()
            raise
        self._evento("espelho_ligado", f"braco={self.cfg.braco} simbolo={sim} conta={conta.get('login')}", critico=False)
        return est

    def encerrar(self, t_ns: int) -> None:
        """Fim da sessao do nucleo: zera a demo e libera a trava de exclusividade."""
        if self.ativo and self.crenca.quantidade > 0:
            self.saida("encerramento", t_ns, forcado=True)
        self.ativo = False
        if self.trava is not None:
            self.trava.liberar()

    def desligar(self, motivo: str, t_ns: int | None = None) -> None:
        """Falha segura: o espelho deixa de ABRIR posicoes; o que esta aberto segue protegido pelo SL e pelo guardiao."""
        if not self.desligado_por:
            self.desligado_por = motivo
            t = self.rel.agora_ns() if t_ns is None else t_ns
            self.registro.incidente(t_ns=t, origem=ORIGEM, severidade="alerta",
                                    detalhe=f"espelho desligado para novas entradas: {motivo}")
            self.eventos.append(("espelho_desligado", motivo))

    def _evento(self, tipo: str, detalhe: str, *, critico: bool = False, t_ns: int | None = None) -> None:
        t = self.rel.agora_ns() if t_ns is None else t_ns
        self.eventos.append((tipo, detalhe))
        self.registro.evento_risco(t_ns=t, braco=self.cfg.braco, tipo=tipo, detalhe=detalhe[:400], critico=critico)

    # ================================================================================== estado do gateway ==
    def ao_estado_gateway(self, d: dict[str, Any]) -> None:
        self.estado_gw, self.t_estado_gw = d, time.monotonic()

    def _bloqueio(self) -> str:
        """Bloqueio EXTERNO de entradas (usado pelo Roteador): '' se livre."""
        if not self.ativo:
            return "espelho_desligado"
        if self.desligado_por:
            return f"espelho_desligado:{self.desligado_por}"
        if time.monotonic() - self.t_estado_gw > self.cfg.estado_max_idade_s:
            return "gateway_sem_estado"
        g = self.estado_gw.get("guardiao") or {}
        if self.estado_gw.get("parado"):
            return "gateway_parado"
        if g.get("travado_emergencia"):
            return "gateway_travado"
        if self.estado_gw.get("incertas"):
            return "gateway_ordem_incerta"
        if self._mudo:
            return "gateway_mudo"
        if self.reconciliador.bloqueado:
            return "reconciliacao_manual" if self.reconciliador.manual_pendente else "reconciliacao_divergente"
        if self.crenca.quantidade > 0:
            return "espelho_posicao_aberta"
        return ""

    def _posicao_gateway(self, _braco: str | None = None) -> Posicao:
        """Posicao REAL da demo (snapshot fresco). Sem resposta, cai na crenca do espelho."""
        if not self.terminal.atualizar():
            return self.crenca
        ps = self.terminal.positions_get(symbol=self.simbolo_gw)
        if not ps:
            return Posicao(simbolo=self.simbolo_gw)
        p = ps[0]
        return Posicao(simbolo=self.simbolo_gw, lado=Lado.COMPRA if int(p.type) == C.POS_COMPRA else Lado.VENDA,
                       quantidade=max(int(round(p.volume)), 1), preco_medio=float(p.price_open), ticket=int(p.ticket),
                       stop=float(p.sl) or None, alvo=float(p.tp) or None)

    # ================================================================================== entrada ============
    def espelhar_entrada(self, t_ns: int, pos_sim: Posicao, intencao: Intencao, stop_pontos: float | None,
                         alvo_pontos: float | None, tempo_max_s: int | None) -> ResultadoEspelho:
        """Chamado quando a posicao SIMULADA do braco abre. Nunca levanta: qualquer recusa vira evento."""
        if pos_sim.lado is None or pos_sim.quantidade <= 0:
            return ResultadoEspelho("entrada", False, "sem_posicao_simulada")
        if (b := self._bloqueio()):
            return self._recusar("entrada", b, t_ns)
        if not self.terminal.atualizar():
            return self._recusar("entrada", "gateway_sem_snapshot", t_ns)
        if self.terminal.positions_get() or self.terminal.orders_get():
            return self._recusar("entrada", "conta_demo_nao_zerada", t_ns)
        it = Intencao(
            id=f"esp-{intencao.id}", braco=self.braco_demo, acao=Acao.COMPRAR if pos_sim.lado == Lado.COMPRA else Acao.VENDER,
            quantidade=min(pos_sim.quantidade, self.orq.cfg.risco.contratos_max), stop_pontos=stop_pontos,
            alvo_pontos=alvo_pontos, tempo_max_s=tempo_max_s, validade_ns=t_ns + int(self.cfg.validade_entrada_s * NS),
            modo=intencao.modo, preco_referencia=pos_sim.preco_medio, t0_ns=t_ns)
        r = self.roteador.executar_intencao(it, Posicao(simbolo=self.simbolo_gw), t_ns, {})
        if not r.decisao.aprovada:
            return self._recusar("entrada", "vetada:" + ";".join(r.decisao.motivos), t_ns)
        env = r.envio
        if env is None or not env.enviado:
            return self._recusar("entrada", r.motivo or "nao_enviada", t_ns)
        self.entradas += 1
        return self._apos_envio("entrada", env.ordem.estado, env.ack, pos_sim.lado, pos_sim.quantidade, t_ns)

    def _recusar(self, acao: str, motivo: str, t_ns: int) -> ResultadoEspelho:
        self._evento(f"espelho_{acao}_recusada", motivo, t_ns=t_ns)
        return ResultadoEspelho(acao, False, motivo)

    def _apos_envio(self, acao: str, estado: EstadoOrdem, ack: AckOrdem | None, lado: Lado | None, qtd: int,
                    t_ns: int) -> ResultadoEspelho:
        com = ack.comentario if ack is not None else "sem ack"
        if estado == EstadoOrdem.INCERTA:
            self._evento(f"espelho_{acao}_incerta", f"{com} (nunca reenvia; a reconciliacao resolve)", critico=True, t_ns=t_ns)
            self.registro.incidente(t_ns=t_ns, origem=ORIGEM, severidade="alerta",
                                    detalhe=f"ordem {acao} INCERTA no gateway; entradas bloqueadas ate reconciliar")
        elif estado in (EstadoOrdem.EXECUTADA, EstadoOrdem.PARCIAL):
            self._evento(f"espelho_{acao}_executada", f"retcode={ack.retcode if ack else None} preco={ack.preco if ack else 0}",
                         t_ns=t_ns)
            if acao == "entrada" and lado is not None:
                self.crenca = Posicao(simbolo=self.simbolo_gw, lado=lado, quantidade=qtd,
                                      preco_medio=ack.preco if ack else 0.0, aberta_ns=t_ns)
                self._descobrir_ticket()
            elif acao == "saida":
                self.crenca = Posicao(simbolo=self.simbolo_gw)
        else:
            self._evento(f"espelho_{acao}_recusada", f"{estado.value}: {com}", t_ns=t_ns)
        return ResultadoEspelho(acao, estado not in TERMINAIS or estado == EstadoOrdem.EXECUTADA, estado=estado.value, ack=ack)

    def _descobrir_ticket(self) -> None:
        if self.crenca.quantidade > 0 and self.crenca.ticket is None and self.terminal.atualizar():
            p = next((x for x in self.terminal.positions_get(symbol=self.simbolo_gw) or ()
                      if int(x.magic) == self.cfg.magic), None)
            if p is not None:
                self.crenca = self.crenca.model_copy(update={"ticket": int(p.ticket), "stop": float(p.sl) or None,
                                                             "alvo": float(p.tp) or None})

    # ================================================================================== saida ==============
    def saida(self, motivo: str, t_ns: int, *, forcado: bool = False) -> ResultadoEspelho:
        """Fecha a posicao da demo com ``position=<ticket>``. UM dono: a mesma posicao nao recebe duas saidas."""
        if not self.ativo and not forcado:
            return ResultadoEspelho("saida", False, "espelho_desligado")
        pos = self._posicao_gateway()
        if pos.quantidade <= 0 or pos.ticket is None:
            if self.crenca.quantidade > 0:  # o SL/TP do servidor (ou outro dono) ja fechou: nada a enviar
                self._evento("espelho_posicao_encerrada", f"demo ja zerada antes da saida '{motivo}' (SL/TP do servidor)",
                             t_ns=t_ns)
            self.crenca = Posicao(simbolo=self.simbolo_gw or self.crenca.simbolo)
            return ResultadoEspelho("saida", False, "sem_posicao_na_demo")
        ant = self._saidas.get(pos.ticket)
        if ant is not None and (ant[2] != EstadoOrdem.REJEITADA.value or time.monotonic() - ant[1] < 1.0):
            self._evento("espelho_saida_duplicada_evitada", f"ticket={pos.ticket} dono={ant[0]}", t_ns=t_ns)
            return ResultadoEspelho("saida", False, f"saida_ja_reivindicada:{ant[0]}")
        env = self.roteador.zerar_braco(self.braco_demo, t_ns, motivo)
        if env is None or not env.enviado:
            self._evento("espelho_saida_recusada", env.motivo if env else "nada_a_enviar", t_ns=t_ns)
            return ResultadoEspelho("saida", False, env.motivo if env else "nada_a_enviar")
        self._saidas[pos.ticket] = (motivo, time.monotonic(), env.ordem.estado.value)
        self.saidas_enviadas += 1
        return self._apos_envio("saida", env.ordem.estado, env.ack, None, 0, t_ns)

    def sim_fechou(self, motivo_sim: str, t_ns: int) -> ResultadoEspelho | None:
        """A posicao simulada fechou (stop/alvo/qualquer motivo): garante a demo fechada."""
        if not self.ativo or self.crenca.quantidade <= 0:
            return None
        return self.saida(f"sim_{motivo_sim}", t_ns)

    # ================================================================================== kill / emergencia ==
    def ao_kill(self, motivo: str, t_ns: int) -> dict[str, Any] | None:
        """Kill switch do braco espelhado: comando de EMERGENCIA ao gateway (cancela, zera e TRAVA; rearme manual)."""
        if self._kill_confirmado or time.monotonic() < self._prox_kill:
            return None
        self._prox_kill = time.monotonic() + 30.0  # gateway mudo: nao trava o laco do nucleo a cada tick
        resp = self.emergencia(motivo, t_ns)
        self._kill_confirmado = resp is not None
        return resp

    def emergencia(self, motivo: str, t_ns: int) -> dict[str, Any] | None:
        resp = self.canal.enviar({"tipo": "emergencia", "origem": f"nucleo: {motivo}"[:120]},
                                 timeout_s=max(self.cfg.timeout_s, 5))
        if resp is None:
            self._evento("espelho_emergencia_sem_confirmacao", motivo, critico=True, t_ns=t_ns)
            self.registro.incidente(t_ns=t_ns, origem=ORIGEM, severidade="critico",
                                    detalhe="emergencia: gateway nao confirmou no prazo (o guardiao e a flag seguem valendo)")
            return None
        self._evento("espelho_emergencia", f"{motivo}: zerado={resp.get('zerado')} travado={resp.get('travado')} "
                     f"posicoes={resp.get('posicoes')} ordens={resp.get('ordens')}", critico=True, t_ns=t_ns)
        if resp.get("zerado"):
            self.crenca = Posicao(simbolo=self.simbolo_gw or self.crenca.simbolo)
        return dict(resp)

    # ================================================================================== reconciliacao ======
    def passo(self, t_ns: int) -> None:
        """Chamado a cada volta do laco do nucleo; reconcilia no maximo a cada ``reconciliar_a_cada_s``."""
        if not self.ativo:
            return
        agora = time.monotonic()
        if agora >= self._prox_reconc:
            self._prox_reconc = agora + self.cfg.reconciliar_a_cada_s
            self.reconciliar(t_ns)

    def _sincronizar_crenca(self) -> None:
        """Fim de posicao explicado por deal de saida (SL/TP/SO do servidor, robo ou manual) zera a crenca."""
        if self.crenca.quantidade <= 0:
            return
        if self.crenca.ticket is None:
            self._descobrir_ticket()
        tk = self.crenca.ticket
        if tk is None:
            return
        vivos = {int(p.ticket) for p in self.terminal.positions_get(symbol=self.simbolo_gw) or ()}
        if tk in vivos:
            return
        saidas = [d for d in self.terminal.history_deals_get() or ()
                  if int(d.position_id) == tk and int(d.entry) in (C.ENTRADA_OUT, C.ENTRADA_INOUT)]
        if saidas:
            d = saidas[-1]
            nome = C.NOMES_RAZAO.get(int(d.reason), str(d.reason))
            self._evento("espelho_posicao_encerrada", f"ticket={tk} reason={nome} lucro={d.profit:+.2f}")
            self.crenca = Posicao(simbolo=self.simbolo_gw)

    def _adotar_posicao_do_terminal(self) -> None:
        """Entrada que ficou INCERTA e foi resolvida como executada: a posicao real passa a ser a crenca."""
        if self.crenca.quantidade > 0:
            return
        p = next((x for x in self.terminal.positions_get(symbol=self.simbolo_gw) or () if int(x.magic) == self.cfg.magic), None)
        if p is not None:
            self.crenca = Posicao(
                simbolo=self.simbolo_gw, lado=Lado.COMPRA if int(p.type) == C.POS_COMPRA else Lado.VENDA,
                quantidade=max(int(round(p.volume)), 1), preco_medio=float(p.price_open), ticket=int(p.ticket),
                stop=float(p.sl) or None, alvo=float(p.tp) or None)
            self._evento("espelho_posicao_adotada", f"ticket={p.ticket} (entrada INCERTA resolvida pela reconciliacao)")

    def reconciliar(self, t_ns: int) -> ResultadoReconciliacao | None:
        if not self.terminal.atualizar():
            self._falhas += 1
            if self._falhas >= self.cfg.falhas_gateway_max and not self._mudo:
                self._mudo = True
                self.registro.incidente(t_ns=t_ns, origem=ORIGEM, severidade="alerta",
                                        detalhe=f"gateway sem resposta a {self._falhas} snapshots: entradas bloqueadas")
            return None
        self._falhas, self._mudo = 0, False
        self._sincronizar_crenca()
        res = self.reconciliador.reconciliar(t_ns)
        self.ultimo_reconc = res
        for k, n in (("robo", res.deals_robo), ("servidor", res.deals_servidor), ("manual", res.deals_manuais)):
            self.deals_vistos[k] += n
        for dv in res.divergencias:
            self._evento("espelho_divergencia" if dv.tipo != "intervencao_manual" else "espelho_intervencao_manual",
                         f"{dv.tipo}: {dv.detalhe}", critico=dv.critica, t_ns=t_ns)
            self.registro.incidente(
                t_ns=t_ns, origem=ORIGEM, severidade="critico" if dv.critica else "alerta",
                detalhe=f"reconciliacao do espelho: {dv.tipo} {dv.detalhe}"[:300])
        if res.incertas_resolvidas:
            self._adotar_posicao_do_terminal()
        return res

    def reconhecer_intervencao_manual(self, responsavel: str, motivo: str, t_ns: int) -> None:
        self.reconciliador.reconhecer_intervencao_manual(responsavel, motivo, t_ns)
        self._evento("espelho_intervencao_reconhecida", f"{responsavel}: {motivo}", t_ns=t_ns)

    # ================================================================================== resumo =============
    def resumo(self) -> dict[str, Any]:
        return {"braco": self.cfg.braco, "ativo": self.ativo, "desligado_por": self.desligado_por,
                "entradas": self.entradas, "saidas": self.saidas_enviadas, "ordens_sem_ack": self.emissor.sem_resposta,
                "crenca": self.crenca.quantidade, "bloqueio": self._bloqueio()}

