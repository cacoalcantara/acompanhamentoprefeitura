"""OMS: maquina de estados de ordens, idempotencia e tratamento de retcodes (sem loop de reenvio).

NOVA -> ENVIADA -> INCERTA -> ACEITA -> PARCIAL -> EXECUTADA/CANCELADA/REJEITADA/EXPIRADA.

Garantias:
* a ordem (intencao) e gravada em estado NOVA ANTES de qualquer chamada ao executor;
* idempotencia por (intencao_id, tipo) e por janela de duplicadas; `magic` por braco; mapa proprio
  ordem<->ticket (nunca depende de `comment`);
* retcode None/10012/10031 => INCERTA: bloqueia o braco, a reconciliacao resolve; NUNCA reenvia;
* 10008 = colocada (ACEITA), nao executada; 10009 = executada; 10010 = parcial;
* 10024 => recuo exponencial (sem loop); 10027 => alerta de algotrading desligado e bloqueio.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..core.ids import novo_id
from ..core.protocolos import Banco, Executor, RegistroAuditoria
from ..core.relogio import Relogio
from ..core.tipos import AckOrdem, ComandoOrdem, EstadoOrdem, Execucao, Intencao

RET_REQUOTE, RET_REJECT, RET_DONE, RET_PLACED, RET_DONE_PARTIAL = 10004, 10006, 10009, 10008, 10010
RET_TIMEOUT, RET_TOO_MANY_REQUESTS, RET_CLIENT_DISABLES_AT, RET_NO_CONNECTION = 10012, 10024, 10027, 10031
RETCODES_INCERTOS = frozenset({RET_TIMEOUT, RET_NO_CONNECTION})

E = EstadoOrdem
TRANSICOES: dict[EstadoOrdem, frozenset[EstadoOrdem]] = {
    E.NOVA: frozenset({E.ENVIADA, E.REJEITADA, E.CANCELADA}),
    E.ENVIADA: frozenset({E.INCERTA, E.ACEITA, E.PARCIAL, E.EXECUTADA, E.REJEITADA, E.CANCELADA, E.EXPIRADA}),
    E.INCERTA: frozenset({E.ACEITA, E.PARCIAL, E.EXECUTADA, E.REJEITADA, E.CANCELADA, E.EXPIRADA}),
    E.ACEITA: frozenset({E.PARCIAL, E.EXECUTADA, E.CANCELADA, E.REJEITADA, E.EXPIRADA}),
    E.PARCIAL: frozenset({E.PARCIAL, E.EXECUTADA, E.CANCELADA, E.EXPIRADA}),
    E.EXECUTADA: frozenset(), E.CANCELADA: frozenset(), E.REJEITADA: frozenset(), E.EXPIRADA: frozenset(),
}
TERMINAIS = frozenset({E.EXECUTADA, E.CANCELADA, E.REJEITADA, E.EXPIRADA})
ABERTOS = frozenset({E.NOVA, E.ENVIADA, E.INCERTA, E.ACEITA, E.PARCIAL})


class ErroTransicao(Exception):
    pass


@dataclass
class OrdemOMS:
    id: str
    intencao_id: str
    braco: str
    cmd: ComandoOrdem
    t_criacao_ns: int
    estado: EstadoOrdem = EstadoOrdem.NOVA
    t_envio_ns: int = 0
    ticket: int = 0
    retcode: int | None = None
    qtd_exec: int = 0
    valor_exec: float = 0.0
    motivo: str = ""
    historico: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def eh_entrada(self) -> bool:
        return self.cmd.tipo != "saida"

    @property
    def restante(self) -> int:
        return max(self.cmd.quantidade - self.qtd_exec, 0)

    @property
    def preco_medio_exec(self) -> float:
        return self.valor_exec / self.qtd_exec if self.qtd_exec else 0.0


@dataclass
class ResultadoEnvio:
    ordem: OrdemOMS
    ack: AckOrdem | None
    enviado: bool
    motivo: str = ""


class OMS:
    def __init__(
        self, relogio: Relogio, magic_por_braco: dict[str, int], banco: Banco | None = None,
        auditoria: RegistroAuditoria | None = None, janela_duplicada_s: float = 2.0,
        recuo_inicial_s: float = 1.0, recuo_max_s: float = 30.0, simulada: bool = True,
    ) -> None:
        self.relogio = relogio
        self.magic_por_braco = dict(magic_por_braco)
        self._braco_por_magic = {v: k for k, v in magic_por_braco.items()}
        self.banco = banco
        self.auditoria = auditoria
        self.janela_duplicada_ns = int(janela_duplicada_s * 1e9)
        self.recuo_inicial_s = recuo_inicial_s
        self.recuo_max_s = recuo_max_s
        self.simulada = simulada
        self._ordens: dict[str, OrdemOMS] = {}
        self._por_ticket: dict[int, str] = {}
        self._por_chave: dict[tuple[str, str], str] = {}
        self._recentes: dict[tuple[Any, ...], int] = {}
        self._intencoes: dict[str, Intencao] = {}
        self.bloqueado_ate_ns = 0
        self._recuo_atual_s = 0.0
        self.algotrading_desligado = False
        self.alertas: list[str] = []
        self.ao_transicao: list[Callable[[OrdemOMS], None]] = []

    # ------------------------------------------------------------------ consultas --------------------
    def ordem(self, ordem_id: str) -> OrdemOMS:
        return self._ordens[ordem_id]

    def por_ticket(self, ticket: int) -> OrdemOMS | None:
        oid = self._por_ticket.get(ticket)
        return self._ordens.get(oid) if oid else None

    def por_intencao(self, intencao_id: str) -> list[OrdemOMS]:
        return [o for o in self._ordens.values() if o.intencao_id == intencao_id]

    def abertas(self, braco: str | None = None) -> list[OrdemOMS]:
        return [o for o in self._ordens.values() if o.estado in ABERTOS and (braco is None or o.braco == braco)]

    def incertas(self, braco: str | None = None) -> list[OrdemOMS]:
        return [o for o in self.abertas(braco) if o.estado == E.INCERTA]

    def pendentes_entrada(self, braco: str) -> int:
        """Contratos de entrada ainda nao resolvidos (INCERTA conta inteira): alimenta o limite de exposicao."""
        return sum(o.restante for o in self.abertas(braco) if o.eh_entrada)

    def magic(self, braco: str) -> int:
        return self.magic_por_braco[braco]

    def bloqueado(self, braco: str, t_ns: int | None = None) -> str:
        """Motivo de bloqueio de novos envios para o braco ('' se livre)."""
        t = self.relogio.agora_ns() if t_ns is None else t_ns
        if self.algotrading_desligado:
            return "algotrading_desligado"
        if t < self.bloqueado_ate_ns:
            return "recuo_ativo"
        if self.incertas(braco):
            return "ordem_incerta_pendente"
        return ""

    # ------------------------------------------------------------------ persistencia/auditoria -------
    def _linha(self, o: OrdemOMS) -> dict[str, Any]:
        return {"id": o.id, "t_ns": o.t_criacao_ns, "braco": o.braco, "intencao_id": o.intencao_id,
                "lado": o.cmd.lado.value, "tipo": o.cmd.tipo, "quantidade": o.cmd.quantidade,
                "preco": o.cmd.preco, "estado": o.estado.value, "ticket": o.ticket, "retcode": o.retcode,
                "simulada": int(self.simulada)}

    def _audit(self, tipo: str, o: OrdemOMS, extra: str = "") -> None:
        if self.auditoria is not None:
            self.auditoria.registrar(tipo, {**self._linha(o), "motivo": extra or o.motivo})

    def _transitar(self, o: OrdemOMS, novo: EstadoOrdem, motivo: str = "") -> None:
        if novo not in TRANSICOES[o.estado]:
            raise ErroTransicao(f"{o.id}: {o.estado.value} -> {novo.value} invalido")
        o.estado = novo
        o.motivo = motivo or o.motivo
        o.historico.append((self.relogio.agora_ns(), novo.value, motivo))
        self._audit("oms_estado", o, motivo)
        for cb in self.ao_transicao:
            cb(o)

    # ------------------------------------------------------------------ envio --------------------------
    def registrar_intencao(self, intencao: Intencao) -> None:
        """Grava a intencao (memoria + auditoria) antes de qualquer ordem derivada dela."""
        self._intencoes[intencao.id] = intencao
        if self.auditoria is not None:
            self.auditoria.registrar("oms_intencao", intencao.model_dump(mode="json"))

    def enviar(self, cmd: ComandoOrdem, executor: Executor) -> ResultadoEnvio:
        agora = self.relogio.agora_ns()
        braco = self._braco_por_magic.get(cmd.magic)
        if braco is None:
            raise KeyError(f"magic {cmd.magic} sem braco")
        chave = (cmd.intencao_id, cmd.tipo)
        if chave in self._por_chave:  # idempotencia: mesma intencao nunca gera 2 ordens
            return ResultadoEnvio(self._ordens[self._por_chave[chave]], None, False, "idempotente")
        o = OrdemOMS(id=novo_id("ord"), intencao_id=cmd.intencao_id, braco=braco, cmd=cmd, t_criacao_ns=agora)
        self._ordens[o.id] = o
        self._por_chave[chave] = o.id
        o.historico.append((agora, E.NOVA.value, ""))
        if self.banco is not None:
            self.banco.inserir("ordens", self._linha(o))  # gravada ANTES do envio
        self._audit("oms_estado", o, "nova")

        motivo = self._motivo_bloqueio(o, agora)
        if motivo:
            self._transitar(o, E.REJEITADA, motivo)
            return ResultadoEnvio(o, None, False, motivo)
        self._recentes[self._chave_dup(o)] = agora

        self._transitar(o, E.ENVIADA)
        o.t_envio_ns = agora
        try:
            ack = executor.enviar(cmd)
        except Exception as e:  # nao sabemos se chegou ao terminal
            self._transitar(o, E.INCERTA, f"excecao_no_envio:{type(e).__name__}")
            return ResultadoEnvio(o, None, True, "incerta")
        self._processar_ack(o, ack)
        return ResultadoEnvio(o, ack, True, o.motivo)

    @staticmethod
    def _chave_dup(o: OrdemOMS) -> tuple[Any, ...]:
        c = o.cmd
        return (o.braco, c.lado.value, c.tipo, c.quantidade, c.preco)

    def _motivo_bloqueio(self, o: OrdemOMS, agora: int) -> str:
        if self.algotrading_desligado:
            return "algotrading_desligado"
        if agora < self.bloqueado_ate_ns:
            return "recuo_ativo"
        if o.eh_entrada:
            if self.incertas(o.braco):
                return "ordem_incerta_pendente"
        else:
            if any(x.id != o.id and not x.eh_entrada for x in self.abertas(o.braco)):
                return "saida_ja_em_andamento"
        ult = self._recentes.get(self._chave_dup(o))
        if ult is not None and agora - ult < self.janela_duplicada_ns:
            return "ordem_duplicada_na_janela"
        return ""

    def _processar_ack(self, o: OrdemOMS, ack: AckOrdem | None) -> None:
        rc = None if ack is None else ack.retcode
        o.retcode = rc
        if ack is not None and ack.order:
            o.ticket = ack.order
            self._por_ticket[ack.order] = o.id
        if rc is None or rc in RETCODES_INCERTOS:
            self._transitar(o, E.INCERTA, f"retcode={rc}")
            return
        if rc != RET_TOO_MANY_REQUESTS:
            self._recuo_atual_s = 0.0
        if rc == RET_PLACED:  # 10008: colocada, NAO executada
            self._transitar(o, E.ACEITA, "colocada")
        elif rc == RET_DONE:
            o.qtd_exec, o.valor_exec = o.cmd.quantidade, (ack.preco if ack else 0.0) * o.cmd.quantidade
            self._transitar(o, E.EXECUTADA, "executada")
        elif rc == RET_DONE_PARTIAL:
            self._transitar(o, E.PARCIAL, "parcial")
        elif rc == RET_TOO_MANY_REQUESTS:
            self._recuo_atual_s = (min(self._recuo_atual_s * 2, self.recuo_max_s) if self._recuo_atual_s
                                   else self.recuo_inicial_s)
            self.bloqueado_ate_ns = self.relogio.agora_ns() + int(self._recuo_atual_s * 1e9)
            self._transitar(o, E.REJEITADA, f"10024_recuo_{self._recuo_atual_s:.0f}s")
        elif rc == RET_CLIENT_DISABLES_AT:
            self.algotrading_desligado = True
            self.alertas.append("algotrading desligado no terminal (10027)")
            self._transitar(o, E.REJEITADA, "10027_algotrading_desligado")
        else:
            self._transitar(o, E.REJEITADA, f"retcode={rc}")

    # ------------------------------------------------------------------ eventos do terminal/simulador --
    def ao_execucao(self, ticket: int, ex: Execucao) -> OrdemOMS | None:
        o = self.por_ticket(ticket)
        if o is None or o.estado in TERMINAIS:
            return o
        o.qtd_exec += ex.quantidade
        o.valor_exec += ex.preco * ex.quantidade
        if o.qtd_exec >= o.cmd.quantidade:
            self._transitar(o, E.EXECUTADA, "executada")
        else:
            self._transitar(o, E.PARCIAL, f"{o.qtd_exec}/{o.cmd.quantidade}")
        return o

    def ao_cancelada(self, ticket: int) -> OrdemOMS | None:
        return self._terminal(ticket, E.CANCELADA, "cancelada")

    def ao_expirada(self, ticket: int) -> OrdemOMS | None:
        return self._terminal(ticket, E.EXPIRADA, "expirada")

    def ao_rejeitada(self, ticket: int, motivo: str = "rejeitada") -> OrdemOMS | None:
        return self._terminal(ticket, E.REJEITADA, motivo)

    def _terminal(self, ticket: int, novo: EstadoOrdem, motivo: str) -> OrdemOMS | None:
        o = self.por_ticket(ticket)
        if o is not None and o.estado not in TERMINAIS:
            self._transitar(o, novo, motivo)
        return o

    def cancelar(self, ordem_id: str, cancelador: Callable[[int], AckOrdem]) -> bool:
        """Pede cancelamento (uma tentativa; sem reenvio). So ordens com ticket e abertas."""
        o = self._ordens[ordem_id]
        if o.estado not in (E.ACEITA, E.PARCIAL) or not o.ticket:
            return False
        ack = cancelador(o.ticket)
        if ack.retcode in (RET_DONE, RET_PLACED):
            self._transitar(o, E.CANCELADA, "cancelada")
            return True
        return False

    # ------------------------------------------------------------------ resolucao de INCERTA -----------
    def resolver_incerta(
        self, ordem_id: str, achado: Literal["aceita", "executada", "parcial", "inexistente"],
        ticket: int = 0, quantidade: int = 0, preco: float = 0.0,
    ) -> OrdemOMS:
        """Chamado pela reconciliacao apos olhar o terminal. NUNCA reenvia a ordem."""
        o = self._ordens[ordem_id]
        if o.estado != E.INCERTA:
            return o
        if ticket:
            o.ticket = ticket
            self._por_ticket[ticket] = o.id
        if achado == "aceita":
            self._transitar(o, E.ACEITA, "resolvida_na_reconciliacao")
        elif achado in ("executada", "parcial"):
            q = quantidade or o.cmd.quantidade
            o.qtd_exec, o.valor_exec = q, preco * q
            self._transitar(o, E.EXECUTADA if q >= o.cmd.quantidade else E.PARCIAL, "resolvida_na_reconciliacao")
        else:
            self._transitar(o, E.CANCELADA, "inexistente_no_terminal")
        return o

    def limpar_algotrading(self) -> None:
        self.algotrading_desligado = False
