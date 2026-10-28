"""``robowin nucleo``: o mesmo orquestrador alimentado AO VIVO pelo gateway via ZeroMQ.

O nucleo NUNCA envia ordem real: os bracos operam no simulador interno com os ticks/livros vivos. Opcionalmente
(``[geral].espelhar = "<id do braco>"``, desligado por padrao) UM braco e espelhado na conta DEMO por
``decisao.espelho.EspelhoDemo`` (``Roteador(modo="gateway_demo")`` -> comandos ao gateway, unico dono de
``order_send``). O ``MonitorGateway`` grava o estado/heartbeat do gateway em ``incidentes``/``eventos_risco`` e
confirma o kill switch ponta a ponta (gateway travado + posicao zerada) para o painel.

Falha segura: lacuna de sequencia do PUB/SUB, heartbeat do gateway ausente ou dado velho bloqueiam novas
entradas; saidas e protecao continuam. O nucleo envia heartbeat ao gateway (sem ele o gateway bloqueia
ordens novas e depois zera a posicao demo).

Maquina do Tempo ao vivo (``[decisao].odds_nucleo``, ligada por padrao SO como evidencia ao revisor;
``veto_por_odds`` continua false): ``piscina_odds_nucleo`` monta a piscina de sessoes a partir do armazem de ticks
(Parquet) com SOMENTE datas estritamente anteriores ao pregao corrente e simbolos do mesmo ativo/perfil. Mistura de
sintetico e real (ou piscina sintetica com o nucleo em dados reais) e RECUSADA. Sem dados suficientes
(``odds_nucleo_min_sessoes``) o avaliador nao e ligado (comportamento identico ao anterior) e o motivo vai para
``incidentes`` (informativo). Se o gateway se revelar DUBLE (dados sinteticos), o avaliador e desligado: nunca
mistura piscina real com feed sintetico.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from robowin.avaliacao import maquina_do_tempo as mt
from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import LivroSnapshot, NivelBook, Tick

from .orquestrador import Orquestrador
from .registro import ORIGEM_SINTETICO

if TYPE_CHECKING:
    from .espelho import EspelhoDemo, MonitorGateway

NS = 1_000_000_000
CAMPOS_TICK = ("t_servidor_ms", "t_utc_ns", "t_recebido_ns", "bid", "ask", "last", "volume", "flags")
TOPICOS = ["tick", "livro", "heartbeat", "estado", "alerta"]


def tick_de_dict(d: dict[str, Any]) -> Tick:
    return Tick(**{k: d[k] for k in CAMPOS_TICK if k in d})


def livro_de_dict(d: dict[str, Any]) -> LivroSnapshot:
    return LivroSnapshot(t_utc_ns=int(d["t_utc_ns"]), niveis=tuple(NivelBook(**n) for n in d["niveis"]))


# ============================================================================ Maquina do Tempo ao vivo ======
ORIGEM_ODDS = "maquina_do_tempo"


@dataclass(frozen=True)
class ConfigOddsNucleo:
    ativo: bool = True  # liga a evidencia ao revisor no nucleo (o veto segue em [decisao].veto_por_odds = false)
    min_sessoes: int = mt.MIN_SESSOES  # abaixo disto o avaliador NAO e ligado (nunca menos que mt.MIN_SESSOES)
    sessoes: int = 60  # sessoes anteriores mais recentes lidas do armazem
    dados: str = ""  # armazem de ticks (Parquet); vazio = <[geral].dados>/ticks

    @property
    def min_efetivo(self) -> int:
        return max(int(self.min_sessoes), mt.MIN_SESSOES)


def config_odds_nucleo(cfg: Mapping[str, Any] | None) -> ConfigOddsNucleo:
    """Le ``[decisao]`` (``odds_nucleo``, ``odds_nucleo_min_sessoes``, ``odds_nucleo_sessoes``, ``odds_nucleo_dados``)."""
    sec = dict((cfg or {}).get("decisao", {}) or {})
    b = ConfigOddsNucleo()
    return ConfigOddsNucleo(ativo=bool(sec.get("odds_nucleo", b.ativo)),
                            min_sessoes=int(sec.get("odds_nucleo_min_sessoes", b.min_sessoes)),
                            sessoes=int(sec.get("odds_nucleo_sessoes", b.sessoes)),
                            dados=str(sec.get("odds_nucleo_dados", b.dados) or ""))


@dataclass(frozen=True)
class PiscinaNucleo:
    """Resultado da montagem: ``piscina`` None => o nucleo roda SEM avaliador (motivo registrado)."""
    piscina: mt.PiscinaSessoes | None
    motivo: str
    severidade: str = "info"  # info (sem base) | alerta (recusa por contaminacao/mistura)
    hoje: dt.date | None = None
    raiz: str = ""

    @property
    def ligada(self) -> bool:
        return self.piscina is not None

    def detalhe(self) -> str:
        if self.piscina is None:
            return f"Maquina do Tempo NAO ligada no nucleo: {self.motivo}"[:300]
        datas = self.piscina.datas()
        return (f"Maquina do Tempo ligada no nucleo (evidencia ao revisor; SIMULACAO, nao e previsao): "
                f"{len(datas)} sessoes completas anteriores a {self.hoje}, todas < pregao corrente; origem "
                f"{'SINTETICO' if self.piscina.sintetico else 'real'}")[:300]

    def registrar(self, registro: Any, t_ns: int) -> None:
        registro.incidente(t_ns=t_ns, origem=ORIGEM_ODDS, severidade="info" if self.ligada else self.severidade,
                           detalhe=self.detalhe())


def piscina_odds_nucleo(cfg: Mapping[str, Any] | None, perfil: PerfilInstrumento, hoje: dt.date, *,
                        raiz: str | Path | None = None, config_path: str | Path | None = None,
                        dados_sinteticos: bool = False) -> PiscinaNucleo:
    """Piscina CAUSAL do nucleo: so datas < ``hoje`` (pregao corrente), so simbolos do ativo do perfil.

    ``dados_sinteticos``: o nucleo roda sobre dados sinteticos? (ao vivo: False). A origem da piscina tem de
    coincidir: piscina sintetica com nucleo real (ou vice-versa) e recusada, assim como armazem com mistura."""
    from robowin.avaliacao.cli_maquina import piscina_de_armazem
    from robowin.core.config import dir_dados

    from .odds import config_odds

    cn = config_odds_nucleo(cfg)
    if not cn.ativo:
        return PiscinaNucleo(None, "desligada por [decisao].odds_nucleo = false", hoje=hoje)
    base = Path(raiz) if raiz is not None else (Path(cn.dados) if cn.dados else dir_dados(config_path) / "ticks")
    if not base.is_dir():
        return PiscinaNucleo(None, f"sem dados: armazem de ticks {base} nao encontrado", hoje=hoje, raiz=str(base))
    try:
        piscina = piscina_de_armazem(base, perfil, sessoes=cn.sessoes, intervalo_s=config_odds(cfg).intervalo_s,
                                     antes_de=hoje)
    except mt.ErroSessoesInsuficientes as e:
        return PiscinaNucleo(None, f"sem dados: {e}", hoje=hoje, raiz=str(base))
    except mt.ErroMaquinaDoTempo as e:  # contaminacao (mistura sintetico/real): recusa, como `maquina-do-tempo`
        return PiscinaNucleo(None, f"RECUSADO: {e}", "alerta", hoje, str(base))
    except Exception as e:  # leitura falhou: a evidencia e opcional, o nucleo segue sem ela
        return PiscinaNucleo(None, f"falha ao ler o armazem: {type(e).__name__}: {e}", "aviso", hoje, str(base))
    if bool(piscina.sintetico) != bool(dados_sinteticos):
        return PiscinaNucleo(None, "RECUSADO: mistura de dados sinteticos e reais (piscina "
                             f"{'SINTETICA' if piscina.sintetico else 'real'} com nucleo em dados "
                             f"{'SINTETICOS' if dados_sinteticos else 'reais'})", "alerta", hoje, str(base))
    futuras = [s.data for s in piscina.sessoes if s.data >= hoje]
    if futuras:  # defensivo: nunca deveria acontecer (antes_de filtra na leitura)
        return PiscinaNucleo(None, f"RECUSADO: sessao(oes) nao anteriores ao pregao corrente: {futuras[:3]}",
                             "alerta", hoje, str(base))
    n = len(piscina.sessoes)
    if n < cn.min_efetivo:
        return PiscinaNucleo(None, f"sessoes insuficientes: {n} completas anteriores a {hoje} "
                             f"(minimo {cn.min_efetivo}; descartadas {len(piscina.descartadas)})", hoje=hoje,
                             raiz=str(base))
    return PiscinaNucleo(piscina, "", hoje=hoje, raiz=str(base))


class FonteEventos(Protocol):
    @property
    def lacunas(self) -> int: ...

    def receber(self) -> tuple[str, dict[str, Any]] | None: ...
    def fechar(self) -> None: ...


class FonteZMQ:
    """SUB do gateway (nao bloqueante); conta lacunas de sequencia."""

    def __init__(self, endereco: str, topicos: list[str] | None = None) -> None:
        from robowin.core.barramento import AssinanteZMQ
        self._s = AssinanteZMQ(endereco, topicos or TOPICOS)

    @property
    def lacunas(self) -> int:
        return int(self._s.lacunas)

    def receber(self) -> tuple[str, dict[str, Any]] | None:
        return self._s.receber(timeout_ms=0)

    def fechar(self) -> None:
        self._s.fechar()


class FonteLista:
    """Fonte em memoria (testes): entrega mensagens ja prontas; ``lacunas`` pode ser alterado pelo teste."""

    def __init__(self, mensagens: Iterable[tuple[str, dict[str, Any]]]) -> None:
        self._it = iter(mensagens)
        self.lacunas: int = 0

    def receber(self) -> tuple[str, dict[str, Any]] | None:
        return next(self._it, None)

    def fechar(self) -> None:
        return None


class Nucleo:
    def __init__(self, orq: Orquestrador, fonte: FonteEventos, *, heartbeat: Callable[[], None] | None = None,
                 intervalo_hb_s: float = 1.0, hb_gateway_max_s: float = 15.0, cooperar_s: float = 0.005,
                 espelho: EspelhoDemo | None = None, monitor: MonitorGateway | None = None,
                 conta_esperada: Any = None) -> None:
        self.orq, self.fonte, self.heartbeat = orq, fonte, heartbeat
        # conta (risco.usuarios.ChaveConta) cujos limites foram aprovados: enquanto o gateway nao CONFIRMAR que a conta
        # conectada e esta (plataforma, servidor, login; e demo), novas entradas ficam bloqueadas
        self.conta_esperada = conta_esperada
        self.conta_divergente: str | None = "conta do gateway ainda nao confirmada" if conta_esperada is not None else None
        self._avisou_conta: str | None = None
        self._prox_marca_conta = 0
        self.espelho, self.monitor = espelho, monitor
        self.intervalo_hb_s, self.hb_gateway_max_s, self.cooperar_s = intervalo_hb_s, hb_gateway_max_s, cooperar_s
        self._lacunas_vistas = 0
        self._ult_hb_gw = time.monotonic()
        self._prox_hb = 0.0
        self.mensagens = 0
        self.estado_gateway: dict[str, Any] = {}
        self._marcou_duble = False
        self._avisou_sem_livro = False
        self.parar = asyncio.Event()

    async def tratar(self, topico: str, d: dict[str, Any]) -> None:
        o = self.orq
        self.mensagens += 1
        if topico == "tick":
            if d.get("backfill"):  # historico recuperado: nao e dado "ao vivo" - segura entradas por precaucao
                o.bloqueio.marcar("backfill", o._agora(), "tick de backfill")
                return
            await o.on_tick(tick_de_dict(d))
        elif topico == "livro":
            o.on_livro(livro_de_dict(d))
        elif topico == "heartbeat":
            self._ult_hb_gw = time.monotonic()
        elif topico == "estado":
            self.estado_gateway = d
            if self.conta_esperada is not None:
                self._conferir_conta(d)
            if self.monitor is not None:
                self.monitor.ao_estado(d, o._agora())
            if self.espelho is not None:
                self.espelho.ao_estado_gateway(d)
            if d.get("duble") and not self._marcou_duble:  # dados do DUBLE sao sinteticos: acende a faixa no painel
                self._marcou_duble = True
                o.registro.incidente(t_ns=o._agora(), origem=ORIGEM_SINTETICO, severidade="info",
                                     detalhe="gateway em modo DUBLE: dados SINTETICOS (nao e mercado)")
                av = o.avaliador_odds
                if av is not None and not (av.piscina is not None and av.piscina.sintetico):
                    o.avaliador_odds = None  # nunca mistura piscina real com feed sintetico
                    o.registro.incidente(t_ns=o._agora(), origem=ORIGEM_ODDS, severidade="info",
                                         detalhe="Maquina do Tempo desligada: gateway DUBLE (sintetico) com piscina real")
            caps = d.get("capacidades") or {}
            if caps.get("livro") is False and not self._avisou_sem_livro:  # plataforma sem livro (MT4)
                self._avisou_sem_livro = True
                o.registro.incidente(t_ns=o._agora(), origem="gateway", severidade="info",
                                     detalhe=f"plataforma {caps.get('plataforma')} sem livro: features de livro e MM desligados")
            if d.get("feed_parado") or d.get("lacuna_pendente"):
                o.bloqueio.marcar("feed_gateway", o._agora(), "gateway reporta feed parado/lacuna")
        elif topico == "alerta":
            o.registro.incidente(t_ns=o._agora(), origem="gateway", severidade="alerta",
                                 detalhe=str(d.get("tipo", ""))[:200])

    def _conferir_conta(self, d: dict[str, Any]) -> None:
        """Conta conectada (estado do gateway) x conta aprovada: divergencia bloqueia entradas com motivo claro."""
        conta = d.get("conta") or {}
        if not conta:
            motivo = "gateway nao informou a conta conectada"
        else:
            motivo = self.conta_esperada.confere(d.get("plataforma"), conta.get("servidor"), conta.get("login"))
            if not motivo and str(conta.get("trade_mode", "")).lower() != "demo":
                motivo = f"conta conectada nao e DEMO (trade_mode {conta.get('trade_mode')}): conta real exige autorizacao_real"
        self.conta_divergente = (f"conta conectada difere da conta aprovada {self.conta_esperada.texto}: {motivo}"
                                 if motivo else None)
        if self.conta_divergente and self.conta_divergente != self._avisou_conta:
            self._avisou_conta = self.conta_divergente
            self._prox_marca_conta = 0  # bloqueia ja no proximo ciclo
            self.orq.registro.incidente(t_ns=self.orq._agora(), origem="nucleo", severidade="critico",
                                        detalhe=f"{self.conta_divergente}; novas entradas BLOQUEADAS"[:300])

    def _verificar_integridade(self) -> None:
        o = self.orq
        agora = o._agora()
        if self.conta_divergente and agora >= self._prox_marca_conta:  # re-marca antes de a retencao expirar
            o.bloqueio.marcar("conta_divergente", agora, self.conta_divergente)
            self._prox_marca_conta = agora + max(1, int(getattr(o.bloqueio, "retencao_ns", 0)) // 2)
        if self.fonte.lacunas > self._lacunas_vistas:
            n = self.fonte.lacunas - self._lacunas_vistas
            self._lacunas_vistas = self.fonte.lacunas
            o.bloqueio.lacuna_sequencia(agora, f"{n} lacuna(s) de sequencia no PUB/SUB")
            o.registro.incidente(t_ns=agora, origem="nucleo", severidade="alerta",
                                 detalhe=f"lacuna de sequencia ({n}): entradas bloqueadas")
        mudo = time.monotonic() - self._ult_hb_gw > self.hb_gateway_max_s
        if mudo:
            o.bloqueio.marcar("heartbeat_gateway", agora, "sem heartbeat do gateway")
        if self.monitor is not None:
            self.monitor.silencio(agora, mudo)

    async def executar(self, duracao_s: float | None = None) -> None:
        ini = time.monotonic()
        while not self.parar.is_set():
            n = 0
            while n < 1000 and (m := self.fonte.receber()) is not None:
                await self.tratar(*m)
                n += 1
            self._verificar_integridade()
            await self.orq.on_tempo()
            if self.espelho is not None:
                self.espelho.passo(self.orq._agora())
            agora = time.monotonic()
            if self.heartbeat is not None and agora >= self._prox_hb:
                self._prox_hb = agora + self.intervalo_hb_s
                self.heartbeat()
            if duracao_s is not None and agora - ini >= duracao_s:
                break
            if n == 0:
                await asyncio.sleep(self.cooperar_s)
