"""Gateway: processo fino e independente, com UMA thread tocando o MT5.

- Publica ticks, livro, estado e heartbeat (1 s) pelo barramento (`PublicadorZMQ` em producao,
  `BarramentoLocal` em testes). O PUB leva numero de sequencia; o assinante detecta lacunas.
- Atende comandos (ROUTER/DEALER ou canal local): ordem -> AckOrdem, heartbeat do nucleo, emergencia com
  confirmacao ponta a ponta (so confirma depois de reler positions/orders e ver tudo zerado), rearme, estado.
- Hospeda o guardiao (limites rigidos) e o executor demo (unico `order_send`).
- Guarda o log bruto da coleta em disco (sobrevive a quedas do nucleo).
- Troca de conta / violacao da guarda interrompe o gateway.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robowin.core.barramento import BarramentoLocal
from robowin.core.instrumentos import PerfilInstrumento, PerfilNaoConfirmado
from robowin.core.relogio import Relogio, RelogioReal
from robowin.core.tipos import AckOrdem, ComandoOrdem

from . import constantes as C
from .canais import exigir_bind_seguro, token_do_ambiente, token_valido
from .capacidades import capacidades_do_terminal, features_desligadas, normalizar_plataforma
from .coleta import ColetorLivro, ColetorTicks, LogBruto
from .consultas import posicoes_publicas, snapshot_terminal
from .conta import ler_estado_conta
from .contrato import ContratoRecusado, ResolucaoContrato, resolver_contrato
from .execucao_demo import (
    ConfigDemo,
    ExecucaoDemo,
    GuardaDemoViolada,
    estado_do_retcode,
    violacao_de_identidade,
)
from .guardiao import ConfigGuardiao, Guardiao
from .tempo_servidor import RelogioServidor, calibrar


def intencoes_enviadas_em(diretorio: Path) -> set[str]:
    """Intencoes ja gravadas como `intencao_antes_envio` nos arquivos de auditoria (idempotencia apos reinicio)."""
    ids: set[str] = set()
    if not Path(diretorio).is_dir():
        return ids
    for arq in sorted(Path(diretorio).glob("auditoria-*.jsonl")):
        with open(arq, encoding="utf-8") as f:
            for linha in f:
                try:
                    reg = json.loads(linha)
                except ValueError:
                    continue
                if isinstance(reg, dict) and reg.get("tipo") == "intencao_antes_envio":
                    iid = (reg.get("dados") or {}).get("intencao_id")
                    if iid:
                        ids.add(str(iid))
    return ids


@dataclass
class ConfigGateway:
    login: int
    servidor: str
    modo_config: str = "demo_mt5"
    simbolo: str | None = None  # None => contrato vigente do ATIVO do perfil
    perfil: PerfilInstrumento | None = None  # obrigatorio: define o ativo, o contrato e as especificacoes esperadas
    hoje: dt.date | None = None
    intervalo_ticks_s: float = 0.05
    intervalo_livro_s: float = 0.25
    intervalo_guardiao_s: float = 1.0
    intervalo_hb_s: float = 1.0
    intervalo_estado_s: float = 1.0
    dir_log: Path | None = None
    dir_auditoria: Path | None = None  # None => <dir_log>/auditoria; sem nenhum dos dois => so memoria (alerta)
    offset_fixo_s: int | None = None
    inicio_coleta_ns: int | None = None
    msgs_por_min: int = 20
    espera_incerta_s: float = 5.0
    guardiao: ConfigGuardiao = field(default_factory=ConfigGuardiao)
    tentativas_emergencia: int = 5
    espera_emergencia_s: float = 0.1
    plataforma: str = "mt5"  # "mt5" | "mt4": deve coincidir com o terminal (ConectorMT4 => "mt4")
    modo_mm: bool = False  # ponte do market making: uma limitada por lado, limitada pode reduzir a posicao oposta


class Gateway:
    def __init__(self, terminal: Any, cfg: ConfigGateway, relogio: Relogio | None = None, publicador: Any = None,
                 canal: Any = None, dormir: Callable[[float], None] = time.sleep) -> None:
        self._dormir = dormir
        self.t, self.cfg = terminal, cfg
        self.relogio = relogio or RelogioReal()
        self.pub = publicador if publicador is not None else BarramentoLocal()
        self.canal = canal
        self._token = token_do_ambiente()  # ROBOWIN_CMD_TOKEN: se definido, TODO comando precisa vir assinado (HMAC)
        self.resolucao: ResolucaoContrato | None = None
        self.rs = RelogioServidor()
        self.executor: ExecucaoDemo
        self.guardiao: Guardiao
        self.ticks: ColetorTicks
        self.livro: ColetorLivro
        self.log: LogBruto | None = None
        self.parado: str | None = None  # motivo da parada (violacao de guarda, comando)
        self._hb_seq = 0
        self._prox: dict[str, int] = {}
        self.erros_coleta = 0
        self.incidentes: list[tuple[str, str]] = []
        self.auditoria: Any = None
        self.erros_mensagem = 0
        self.erros_ciclo = 0
        self.caps = capacidades_do_terminal(terminal)  # do proprio terminal (MT4 declara; MT5/duble => MT5)
        self._lacunas_vistas = 0

    # --- inicializacao -------------------------------------------------------------------------
    def iniciar(self) -> None:
        if self.caps.plataforma != normalizar_plataforma(self.cfg.plataforma):
            raise GuardaDemoViolada(f"plataforma configurada '{self.cfg.plataforma}' difere do terminal '{self.caps.plataforma}'")
        if self.cfg.modo_mm and self.caps.conta == "hedging":
            raise GuardaDemoViolada("modo_mm nao e suportado em conta hedging (MT4): a limitada 'que reduz' abriria outra posicao")
        if not self.t.initialize():
            raise GuardaDemoViolada(f"initialize do MT5 falhou: {self.t.last_error()}")
        est = ler_estado_conta(self.t)
        if est is None:
            raise GuardaDemoViolada("account_info indisponivel")
        hoje = self.cfg.hoje or dt.datetime.fromtimestamp(self.relogio.agora_ns() / 1e9, dt.UTC).date()
        perfil = self.cfg.perfil
        if perfil is None:
            raise GuardaDemoViolada("perfil de instrumento nao informado: o ativo vem de config/instrumentos.toml")
        try:
            self.resolucao = resolver_contrato(self.t, hoje, perfil)
        except (ContratoRecusado, PerfilNaoConfirmado) as e:  # corretora divergente do perfil ou perfil a_confirmar
            raise GuardaDemoViolada(f"ativo {perfil.simbolo_base} recusado: {e}") from e
        simbolo = self.resolucao.simbolo_corretora
        if self.cfg.simbolo and self.cfg.simbolo != simbolo:
            raise GuardaDemoViolada(f"simbolo configurado {self.cfg.simbolo} difere do vigente {simbolo}")
        self.rs = calibrar(self.t, simbolo, self.relogio, amostras=3, intervalo_s=0.3, offset_fixo_s=self.cfg.offset_fixo_s,
                           dormir=self._dormir)
        dir_aud = self.cfg.dir_auditoria or (self.cfg.dir_log / "auditoria" if self.cfg.dir_log is not None else None)
        registrar = None
        ja_enviadas: set[str] = set()
        if dir_aud is not None:
            from robowin.auditoria.cadeia import RegistradorAuditoria
            self.auditoria = RegistradorAuditoria(dir_aud, self.relogio)
            registrar = self.auditoria.registrar
            ja_enviadas = intencoes_enviadas_em(dir_aud)
        else:
            self._alerta("auditoria_so_memoria", {"motivo": "sem diretorio de auditoria: idempotencia nao sobrevive a reinicio"})
        self.executor = ExecucaoDemo(
            self.t, ConfigDemo(login=self.cfg.login, servidor=self.cfg.servidor, simbolo=simbolo,
                               modo_config=self.cfg.modo_config, msgs_por_min=self.cfg.msgs_por_min,
                               espera_incerta_s=self.cfg.espera_incerta_s, lote=perfil.volume_unidade,
                               conta=self.caps.conta, modo_mm=self.cfg.modo_mm),
            self.relogio, alerta=self._alerta, registrar=registrar, relogio_srv=self.rs)
        self.executor.restaurar_enviadas(ja_enviadas)
        self.executor.verificar_guarda()  # falha cedo: conta real/login/servidor errados
        self.cfg.guardiao.perfil = perfil  # o guardiao valida o simbolo da corretora e o stop contra o perfil
        self.guardiao = Guardiao(self.executor, self.t, self.cfg.guardiao, self.relogio, self.rs, alerta=self._alerta,
                                 registrar=registrar)
        if not self.rs.confiavel:
            self.guardiao.bloquear_ate_resolver("offset", "offset do relogio do servidor nao calibrado com confianca")
        if self.cfg.dir_log is not None:
            self.log = LogBruto(self.cfg.dir_log, self.relogio)
        self.ticks = ColetorTicks(self.t, simbolo, self.relogio, self.rs, self.pub, self.log,
                                  inicio_ns=self.cfg.inicio_coleta_ns)
        self.livro = ColetorLivro(self.t, simbolo, self.relogio, self.pub, self.log)
        if perfil.aceita_book and self.caps.livro:
            self.livro.assinar()
        elif perfil.aceita_book:  # capacidade ausente => livro/MM/features de livro DESLIGADOS, com mensagem clara
            self._alerta("capacidade_ausente", {"motivos": self.caps.bloqueios(usa_livro=True, usa_mm=True),
                                                "features_desligadas": list(features_desligadas(self.caps))})
        self._publicar_estado()

    def _alerta(self, tipo: str, dados: dict[str, Any]) -> None:
        self.incidentes.append((tipo, str(dados)))
        self.pub.publicar("alerta", {"tipo": tipo, "dados": dados, "t_ns": self.relogio.agora_ns()})

    def _lacuna_sem_backfill_ativa(self) -> bool:
        return any(m.startswith("feed_lacuna") for m in self.guardiao.motivos_bloqueio())

    def _lacuna_sem_backfill(self) -> None:
        """Terminal sem historico de ticks (MT4): lacuna do feed NAO se recupera; so BLOQUEIA entradas (ate a virada
        do dia ou rearme manual). Perdas reais vem do EA (`lacunas_ticks`); falhas de leitura, do coletor."""
        n = self.ticks.lacunas_sem_backfill + int(getattr(self.t, "lacunas_ticks", 0))
        if n > self._lacunas_vistas:
            self._lacunas_vistas = n
            self.guardiao.bloquear_ate_resolver(
                "feed_lacuna", "lacuna no feed de ticks e a plataforma nao tem historico de ticks: sem backfill",
                persistente=False)
            self._alerta("lacuna_sem_backfill", {"lacunas": n, "plataforma": self.caps.plataforma})

    def _recalibrar(self) -> None:
        """Enquanto o offset nao for confiavel, tenta de novo (feed vivo); detecta mudanca de offset."""
        novo = calibrar(self.t, self.executor._sym, self.relogio, amostras=3, intervalo_s=0.3,
                        offset_fixo_s=self.cfg.offset_fixo_s, dormir=self._dormir)
        if novo.confiavel and (novo.offset_ms != self.rs.offset_ms or not self.rs.confiavel):
            if self.rs.confiavel:
                self._alerta("offset_servidor_mudou", {"de_s": self.rs.offset_s, "para_s": novo.offset_s})
            self.rs = novo
            self.guardiao.atualizar_relogio_servidor(novo)
            self.ticks.rs = novo
            self.guardiao.desbloquear("offset")

    # --- comandos --------------------------------------------------------------------------------
    def _ordem(self, msg: dict[str, Any]) -> dict[str, Any]:
        cmd = ComandoOrdem.model_validate(msg["cmd"])
        ok, motivo = self.guardiao.avaliar_entrada(cmd)
        if not ok:
            ack = AckOrdem(retcode=C.RET_LOCAL_RECUSADO, comentario=f"bloqueado pelo guardiao: {motivo}")
        else:
            ack = self.executor.enviar(cmd, dono=str(msg.get("dono", "nucleo")))
        est = estado_do_retcode(ack.retcode)
        return {"tipo": "ack", "id": msg.get("id"), "ack": ack.model_dump(mode="json"), "estado": est.value,
                "intencao_id": cmd.intencao_id}

    def _ordem_pendente(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Cancela ou modifica (preco/SL) uma ordem pendente pelo ticket; conta na franquia de mensagens."""
        dono = str(msg.get("dono", "nucleo"))
        if msg.get("tipo") == "cancelar_ordem":
            ack = self.executor.cancelar_ordem(int(msg["ticket"]), dono=dono)
        else:
            ack = self.executor.modificar_ordem(int(msg["ticket"]), float(msg["preco"]), msg.get("sl"), dono=dono)
        return {"tipo": "ack", "id": msg.get("id"), "ack": ack.model_dump(mode="json"),
                "estado": estado_do_retcode(ack.retcode).value}

    def _emergencia(self, msg: dict[str, Any]) -> dict[str, Any]:
        origem = f"comando ({msg.get('origem', 'desconhecida')})"
        self.guardiao.acionar_emergencia(origem)
        for _ in range(self.cfg.tentativas_emergencia):
            conf = self.guardiao.confirmacao_emergencia()
            if conf["zerado"]:
                break
            time.sleep(self.cfg.espera_emergencia_s)
            self.guardiao.zerar_tudo(origem)
        conf = self.guardiao.confirmacao_emergencia()
        return {"tipo": "ack_emergencia", "id": msg.get("id"), **conf, "confirmado": bool(conf["zerado"] and conf["travado"])}

    def tratar(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        tipo = msg.get("tipo")
        if self._token and not token_valido(msg, self._token):  # ordem, emergencia, rearmar e parar passam pelo mesmo token
            self._alerta("comando_sem_token", {"tipo": str(tipo)})
            return {"tipo": "erro", "id": msg.get("id"), "erro": "token invalido ou ausente"}
        if tipo == "heartbeat":
            self.guardiao.heartbeat()
            return None
        if tipo == "ordem":
            return self._ordem(msg)
        if tipo == "emergencia":
            return self._emergencia(msg)
        if tipo == "snapshot":  # consulta SOMENTE LEITURA (positions/orders/deals) para a reconciliacao do nucleo
            return {"tipo": "snapshot", "id": msg.get("id"), **snapshot_terminal(
                self.t, self.rs, self.executor.cfg.lote, int(msg.get("desde_ns", 0)), self.relogio.agora_ns() + 86_400_000_000_000)}
        if tipo in ("cancelar_ordem", "modificar_ordem"):
            return self._ordem_pendente(msg)
        if tipo == "estado":
            return {"tipo": "estado", "id": msg.get("id"), **self.estado()}
        if tipo == "rearmar":
            try:
                self.guardiao.rearmar(str(msg.get("motivo", "")))
                return {"tipo": "rearmado", "id": msg.get("id"), "ok": True}
            except (ValueError, RuntimeError) as e:
                return {"tipo": "rearmado", "id": msg.get("id"), "ok": False, "erro": str(e)}
        if tipo == "parar":
            self.parado = "comando parar"
            return {"tipo": "parando", "id": msg.get("id")}
        return {"tipo": "erro", "id": msg.get("id"), "erro": f"comando desconhecido: {tipo}"}

    # --- estado / publicacao -----------------------------------------------------------------------
    def estado(self) -> dict[str, Any]:
        conta = ler_estado_conta(self.t)
        ps = self.t.positions_get()
        os_ = self.t.orders_get()
        posicoes = None if ps is None else posicoes_publicas(ps, self.executor.cfg.lote)
        pos = posicoes[0] if posicoes else None  # compat.: a primeira posicao
        tickets = sorted(int(p.ticket) for p in ps) if ps else []
        return {
            "t_ns": self.relogio.agora_ns(),
            "conta": conta.model_dump() if conta else None,
            "duble": bool(getattr(self.t, "eh_duble", False)),
            "simbolo": self.resolucao.simbolo_corretora if self.resolucao else None,
            "offset_s": self.rs.offset_s, "offset_confiavel": self.rs.confiavel,
            "feed_parado": self.ticks.feed_parado,
            "lacuna_pendente": self.ticks.lacuna_pendente or self._lacuna_sem_backfill_ativa(),
            "plataforma": self.caps.plataforma, "tickets_abertos": tickets,
            "ticks_total": self.ticks.total, "livro_total": self.livro.total,
            "livro_profundidade": {"declarada": self.livro.profundidade_declarada,
                                   "observada": self.livro.profundidade_observada},
            "guardiao": self.guardiao.estado(), "posicao": pos, "posicoes": posicoes,
            "ordens": None if os_ is None else len(os_), "lote": self.executor.cfg.lote,
            "modo_mm": self.cfg.modo_mm,
            "capacidades": {**self.caps.como_dict(), "livro": bool(self.caps.livro) and bool(self.livro.profundidade_declarada)},
            "incertas": sorted(self.executor.incertas), "parado": self.parado,
        }

    def _publicar_estado(self) -> None:
        self.pub.publicar("estado", self.estado())

    def _vencido(self, nome: str, intervalo_s: float) -> bool:
        agora = self.relogio.monotonico_ns()
        if agora >= self._prox.get(nome, 0):
            self._prox[nome] = agora + int(intervalo_s * 1e9)
            return True
        return False

    # --- laco -------------------------------------------------------------------------------------
    def passo(self, timeout_ms: int = 0) -> None:
        """Uma iteracao do laco (unica thread que toca o MT5)."""
        try:
            if self.canal is not None:
                for _ in range(50):
                    r = self.canal.receber(timeout_ms if _ == 0 else 0)
                    if r is None:
                        break
                    ident, msg = r
                    mid = msg.get("id") if isinstance(msg, dict) else None
                    try:
                        if not isinstance(msg, dict):
                            raise TypeError(f"mensagem nao e dict: {type(msg).__name__}")
                        resp = self.tratar(msg)
                    except GuardaDemoViolada as e:
                        resp = {"tipo": "ack", "id": mid, "estado": "REJEITADA",
                                "ack": AckOrdem(retcode=C.RET_LOCAL_RECUSADO, comentario=f"guarda demo violada: {e}").model_dump(mode="json")}
                        if violacao_de_identidade(e):
                            self.parar(f"guarda demo violada: {e}")
                    except Exception as e:  # noqa: BLE001 - mensagem malformada/erro de tratamento nao derruba o gateway
                        self.erros_mensagem += 1
                        self._alerta("erro_mensagem", {"erro": repr(e), "id": mid})
                        resp = {"tipo": "erro", "id": mid, "erro": f"falha ao tratar mensagem: {e!r}"}
                    if resp is not None:
                        self.canal.responder(ident, resp)
            elif timeout_ms:
                time.sleep(timeout_ms / 1000.0)
            if self.parado:
                return
            if self._vencido("ticks", self.cfg.intervalo_ticks_s):
                try:
                    self.ticks.sondar()
                    self._lacuna_sem_backfill()
                except Exception as e:  # noqa: BLE001 - coleta nunca derruba o guardiao
                    self.erros_coleta += 1
                    self.incidentes.append(("coleta", repr(e)))
            if self._vencido("livro", self.cfg.intervalo_livro_s):
                try:
                    self.livro.sondar()
                except Exception as e:  # noqa: BLE001
                    self.erros_coleta += 1
                    self.incidentes.append(("livro", repr(e)))
            if self._vencido("guardiao", self.cfg.intervalo_guardiao_s):
                try:
                    self.guardiao.ciclo()
                except GuardaDemoViolada:
                    raise
                except Exception as e:  # noqa: BLE001 - falha no ciclo do guardiao nao pode parar o laco
                    self.erros_ciclo += 1
                    self._alerta("erro_ciclo_guardiao", {"erro": repr(e)})
            if not self.rs.confiavel and self._vencido("recalibrar", 30.0):
                try:
                    self._recalibrar()
                except GuardaDemoViolada:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.erros_ciclo += 1
                    self._alerta("erro_recalibrar", {"erro": repr(e)})
            if self._vencido("hb", self.cfg.intervalo_hb_s):
                self._hb_seq += 1
                # dict sem a chave `seq`: Heartbeat.seq colidiria com o numero de sequencia do barramento
                self.pub.publicar("heartbeat", {"origem": "gateway", "hb": self._hb_seq, "t_ns": self.relogio.agora_ns()})
            if self._vencido("estado", self.cfg.intervalo_estado_s):
                self._publicar_estado()
        except GuardaDemoViolada as e:
            if violacao_de_identidade(e):
                self.parar(f"guarda demo violada: {e}")
            else:
                self._alerta("guarda_demo_transitoria", {"motivo": str(e)})

    def parar(self, motivo: str = "parada solicitada") -> None:
        if not self.parado:
            self.parado = motivo
            self._alerta("gateway_parado", {"motivo": motivo})

    def executar(self, parar: threading.Event | None = None, duracao_s: float | None = None,
                 timeout_ms: int = 20) -> None:
        """Roda o laco ate `parar`, `duracao_s` ou violacao de guarda; no fim encerra com seguranca."""
        fim = None if duracao_s is None else time.monotonic() + duracao_s
        try:
            while not self.parado:
                if parar is not None and parar.is_set():
                    break
                if fim is not None and time.monotonic() >= fim:
                    break
                self.passo(timeout_ms)
        finally:
            self.encerrar()

    def encerrar(self) -> None:
        try:
            self._publicar_estado()
        except Exception:  # noqa: BLE001
            pass
        if self.log is not None:
            self.log.fechar()
        try:
            self.livro.liberar()
        except Exception:  # noqa: BLE001
            pass
        if self.canal is not None:
            self.canal.fechar()
        close = getattr(self.pub, "fechar", None)
        if close:
            close()


def bind_exposto(endereco: str) -> bool:
    """True se o endereco escuta em todas as interfaces (0.0.0.0, *, ::) em vez de 127.0.0.1."""
    host = endereco.split("://", 1)[-1].rsplit(":", 1)[0].strip("[]")
    return host in ("0.0.0.0", "*", "::", "")


def rodar_gateway_zmq(fabrica_terminal: Callable[[], Any], cfg: ConfigGateway, end_pub: str, end_cmd: str,
                      duracao_s: float | None = None, relogio: Relogio | None = None) -> None:
    """Ponto de entrada de PROCESSO: cria terminal, PUB e ROUTER e roda o laco. Picklavel (multiprocessing)."""
    from robowin.core.barramento import PublicadorZMQ

    from .canais import CanalComandosZMQ
    exigir_bind_seguro(end_cmd, token_do_ambiente())  # fora do loopback so com token (levanta BindInseguro)
    terminal = fabrica_terminal()
    gw = Gateway(terminal, cfg, relogio, PublicadorZMQ(end_pub), CanalComandosZMQ(end_cmd))
    for nome, end in (("pub", end_pub), ("cmd", end_cmd)):
        if bind_exposto(end):
            logging.getLogger(__name__).warning("gateway %s escutando em interface exposta (%s): padrao e 127.0.0.1", nome, end)
            gw._alerta("bind_exposto", {"canal": nome, "endereco": end})
    gw.iniciar()
    gw.executar(duracao_s=duracao_s)
