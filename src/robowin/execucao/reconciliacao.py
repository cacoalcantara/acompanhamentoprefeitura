"""Reconciliacao: estado interno x terminal (positions_get, orders_get, history_deals_get).

Somente leitura: nunca envia ordens. Divergencia nao explicada => bloqueio de entradas (e kill switch
automatico por divergencia, se configurado). Deals sao classificados por `deal.reason`.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from ..core.protocolos import RegistroAuditoria, TerminalMT5
from ..core.tipos import Base, Lado, Posicao
from ..risco.kill_switch import KillSwitch
from .oms import OMS

DEAL_REASON = {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL", 5: "TP", 6: "SO",
               7: "ROLLOVER", 8: "VMARGIN", 9: "SPLIT"}
DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1
DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1


class ClasseDeal(StrEnum):
    ROBO = "robo"  # EXPERT
    SERVIDOR = "servidor_esperado"  # SL / TP / SO / rolagem / vmargin / split
    MANUAL = "intervencao_manual"  # CLIENT / MOBILE / WEB
    DESCONHECIDO = "desconhecido"


def classificar_deal(reason: int) -> ClasseDeal:
    nome = DEAL_REASON.get(reason, "")
    if nome == "EXPERT":
        return ClasseDeal.ROBO
    if nome in ("SL", "TP", "SO", "ROLLOVER", "VMARGIN", "SPLIT"):
        return ClasseDeal.SERVIDOR
    if nome in ("CLIENT", "MOBILE", "WEB"):
        return ClasseDeal.MANUAL
    return ClasseDeal.DESCONHECIDO


class Divergencia(Base):
    tipo: str
    detalhe: str = ""
    braco: str = ""
    critica: bool = True


class ResultadoReconciliacao(Base):
    ok: bool
    divergencias: tuple[Divergencia, ...] = ()
    deals_robo: int = 0
    deals_servidor: int = 0
    deals_manuais: int = 0
    incertas_pendentes: int = 0
    incertas_resolvidas: int = 0
    bloqueio: bool = False
    kill_acionado: bool = False


def _g(o: Any, nome: str, padrao: Any = 0) -> Any:
    return getattr(o, nome, padrao)


class Reconciliador:
    def __init__(
        self, terminal: TerminalMT5, oms: OMS, simbolo: str, posicao_interna: Callable[[str], Posicao],
        kill_switch: KillSwitch | None = None, auditoria: RegistroAuditoria | None = None,
        confirmacoes_ausencia: int = 2, espera_incerta_s: float = 10.0, desde: dt.datetime | None = None,
        acionar_kill_em_divergencia: bool = True,
    ) -> None:
        self.terminal, self.oms, self.simbolo = terminal, oms, simbolo
        self.posicao_interna = posicao_interna
        self.kill, self.auditoria = kill_switch, auditoria
        self.confirmacoes_ausencia = confirmacoes_ausencia
        self.espera_incerta_ns = int(espera_incerta_s * 1e9)
        self.desde = desde or dt.datetime(2000, 1, 1, tzinfo=dt.UTC)
        self.acionar_kill = acionar_kill_em_divergencia
        self._deals_vistos: set[int] = set()
        self._ausencias: dict[str, int] = {}
        self.manual_pendente = False
        self.ultimo: ResultadoReconciliacao | None = None

    @property
    def bloqueado(self) -> bool:
        return self.manual_pendente or (self.ultimo is not None and self.ultimo.bloqueio)

    def reconhecer_intervencao_manual(self, responsavel: str, motivo: str, t_ns: int) -> None:
        """Desbloqueio humano apos intervencao manual detectada (fica na auditoria)."""
        if not responsavel.strip() or len(motivo.strip()) < 5:
            raise ValueError("reconhecimento exige responsavel e motivo")
        if self.auditoria is not None:
            self.auditoria.registrar("reconciliacao_manual_reconhecida",
                                     {"responsavel": responsavel, "motivo": motivo, "t_ns": t_ns})
        self.manual_pendente = False

    # ------------------------------------------------------------------ nucleo ------------------------
    def reconciliar(self, t_ns: int) -> ResultadoReconciliacao:
        posicoes = list(self.terminal.positions_get(symbol=self.simbolo) or ())
        ordens = list(self.terminal.orders_get(symbol=self.simbolo) or ())
        agora = dt.datetime.fromtimestamp(t_ns / 1e9, tz=dt.UTC)
        deals = list(self.terminal.history_deals_get(self.desde, agora + dt.timedelta(days=1)) or ())
        deals = [d for d in deals if _g(d, "symbol", self.simbolo) == self.simbolo]
        div: list[Divergencia] = []
        incertos_antes = {x.braco for x in self.oms.incertas()}
        magics = dict(self.oms.magic_por_braco)
        braco_de = {v: k for k, v in magics.items()}

        # 1) posicoes por braco (liquido assinado) e posicoes de origem desconhecida
        liquido_term: dict[int, int] = {}
        tickets_por_magic: dict[int, list[int]] = {}
        for p in posicoes:
            mg = int(_g(p, "magic"))
            tickets_por_magic.setdefault(mg, []).append(int(_g(p, "ticket")))
            sinal = 1 if int(_g(p, "type")) == DEAL_TYPE_BUY else -1
            liquido_term[mg] = liquido_term.get(mg, 0) + sinal * int(_g(p, "volume"))
        # hedging: compra+venda do mesmo magic somam liquido 0 mas sao DUAS posicoes (por ticket); o robo opera no maximo
        # uma por simbolo/magic, entao mais de uma posicao e divergencia (em netting nunca ocorre)
        for mg, tks in sorted(tickets_por_magic.items()):
            if len(tks) > 1:
                div.append(Divergencia(tipo="posicoes_multiplas", braco=braco_de.get(mg, ""),
                                       detalhe=f"magic={mg} {len(tks)} posicoes (tickets {sorted(tks)}); liquido={liquido_term[mg]}"))
        for mg, q in liquido_term.items():
            if mg not in braco_de and q != 0:
                div.append(Divergencia(tipo="posicao_externa", detalhe=f"magic={mg} liquido={q}"))
        for braco, mg in magics.items():
            pi = self.posicao_interna(braco)
            interna = 0 if pi.lado is None else (pi.quantidade if pi.lado == Lado.COMPRA else -pi.quantidade)
            term = liquido_term.get(mg, 0)
            if interna != term:
                div.append(Divergencia(tipo="posicao_divergente", braco=braco,
                                       detalhe=f"interna={interna} terminal={term}"))

        # 2) ordens pendentes
        tickets_term = {int(_g(o, "ticket")) for o in ordens}
        deals_por_ordem: dict[int, list[Any]] = {}
        for d in deals:
            deals_por_ordem.setdefault(int(_g(d, "order")), []).append(d)
        for o in ordens:
            if int(_g(o, "magic")) in braco_de and self.oms.por_ticket(int(_g(o, "ticket"))) is None:
                if not any(x.estado.value == "INCERTA" and x.braco == braco_de[int(_g(o, "magic"))]
                           for x in self.oms.abertas()):
                    div.append(Divergencia(tipo="ordem_desconhecida_no_terminal",
                                           detalhe=f"ticket={_g(o, 'ticket')}", braco=braco_de[int(_g(o, 'magic'))]))
        for x in self.oms.abertas():
            if x.estado.value in ("ACEITA", "PARCIAL") and x.ticket and x.ticket not in tickets_term:
                if x.ticket not in deals_por_ordem:
                    div.append(Divergencia(tipo="ordem_ausente_no_terminal", braco=x.braco,
                                           detalhe=f"ticket={x.ticket} estado={x.estado.value}"))

        # 3) deals novos classificados por reason
        n_robo = n_srv = n_man = 0
        for d in sorted(deals, key=lambda z: int(_g(z, "time_msc"))):
            tk = int(_g(d, "ticket"))
            if tk in self._deals_vistos:
                continue
            self._deals_vistos.add(tk)
            cls = classificar_deal(int(_g(d, "reason", 3)))
            if cls == ClasseDeal.ROBO:
                n_robo += 1
                if int(_g(d, "magic")) not in braco_de:
                    div.append(Divergencia(tipo="deal_expert_magic_desconhecido", detalhe=f"deal={tk}"))
            elif cls == ClasseDeal.SERVIDOR:
                n_srv += 1
            elif cls == ClasseDeal.MANUAL:
                n_man += 1
                self.manual_pendente = True
                div.append(Divergencia(tipo="intervencao_manual",
                                       detalhe=f"deal={tk} reason={DEAL_REASON.get(int(_g(d, 'reason')), '?')}"))
            else:
                div.append(Divergencia(tipo="deal_reason_desconhecido", detalhe=f"deal={tk}", critica=False))

        # 4) ordens INCERTAS: procurar no terminal; nunca reenviar
        resolvidas, pendentes = self._resolver_incertas(t_ns, ordens, deals)

        criticas = [x for x in div if x.critica]
        incertos = {x.braco for x in self.oms.incertas()} | incertos_antes
        # divergencia de posicao explicada por ordem INCERTA do mesmo braco bloqueia, mas nao aciona o kill
        diverge_estado = [x for x in criticas if x.tipo != "intervencao_manual"
                          and not (x.tipo == "posicao_divergente" and x.braco in incertos)]
        kill = False
        if diverge_estado and self.kill is not None and self.acionar_kill:
            kill = self.kill.divergencia_reconciliacao("; ".join(f"{x.tipo}:{x.detalhe}" for x in diverge_estado), t_ns)
        res = ResultadoReconciliacao(
            ok=not div and pendentes == 0, divergencias=tuple(div), deals_robo=n_robo, deals_servidor=n_srv,
            deals_manuais=n_man, incertas_pendentes=pendentes, incertas_resolvidas=resolvidas,
            bloqueio=bool(criticas) or pendentes > 0 or self.manual_pendente, kill_acionado=kill)
        if self.auditoria is not None and (div or resolvidas or pendentes):
            self.auditoria.registrar("reconciliacao", {**res.model_dump(mode="json"), "t_ns": t_ns})
        self.ultimo = res
        return res

    def _resolver_incertas(self, t_ns: int, ordens: list[Any], deals: list[Any]) -> tuple[int, int]:
        resolvidas = pendentes = 0
        for x in list(self.oms.incertas()):
            mg = self.oms.magic(x.braco)
            t_env_ms = x.t_envio_ns // 1_000_000 - 1000
            cand_o = [o for o in ordens if int(_g(o, "magic")) == mg and int(_g(o, "volume_current", _g(o, "volume")))
                      == x.cmd.quantidade and int(_g(o, "time_setup_msc")) >= t_env_ms]
            cand_d = [d for d in deals if int(_g(d, "magic")) == mg and int(_g(d, "volume")) == x.cmd.quantidade
                      and int(_g(d, "time_msc")) >= t_env_ms and int(_g(d, "entry")) in (DEAL_ENTRY_IN, DEAL_ENTRY_OUT)]
            if cand_d:
                d = cand_d[0]
                self.oms.resolver_incerta(x.id, "executada", ticket=int(_g(d, "order")), quantidade=int(_g(d, "volume")),
                                          preco=float(_g(d, "price")))
                resolvidas += 1
            elif cand_o:
                self.oms.resolver_incerta(x.id, "aceita", ticket=int(_g(cand_o[0], "ticket")))
                resolvidas += 1
            else:
                self._ausencias[x.id] = self._ausencias.get(x.id, 0) + 1
                if (self._ausencias[x.id] >= self.confirmacoes_ausencia
                        and t_ns - x.t_envio_ns >= self.espera_incerta_ns):
                    self.oms.resolver_incerta(x.id, "inexistente")
                    resolvidas += 1
                else:
                    pendentes += 1
        return resolvidas, pendentes
