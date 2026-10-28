"""Plano condicional (plano 5.2): UM plano armado por braco.

Ciclo de vida: armar -> (disparado | expirado | invalidado | cancelado). Cancelam o plano: mudanca de
posicao, kill switch e janela de evento (calendario). No disparo o RISCO E REFEITO com o preco real de
execucao e o stop e recalculado a partir dele (o plano guarda apenas DISTANCIAS em pontos). A idade do
plano no disparo e registrada.

As duracoes que o LLM entrega (``expira_ns``/``ate_ns`` relativos) sao convertidas em instantes
absolutos por ``ia.validadores.plano_para_absoluto`` antes de armar (ver ``converter_plano``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from robowin.core.tipos import Intencao, Lado, PlanoCondicional, Posicao
from robowin.ia.validadores import plano_para_absoluto

from .dsl_plano import (
    EstadoPlano,
    descrever,
    gatilhos_satisfeitos,
    invalidacao_ativa,
    validar_dsl_absoluto,
)

NS = 1_000_000_000
TipoEventoPlano = Literal["armado", "substituido", "disparado", "cancelado", "expirado", "invalidado"]


def converter_plano(plano_relativo: PlanoCondicional, t0_ns: int) -> PlanoCondicional:
    """Duracoes em ns -> instantes absolutos (t0 + duracao), via ia.validadores.plano_para_absoluto."""
    return plano_para_absoluto(plano_relativo, t0_ns)


@dataclass(frozen=True)
class AssinaturaPosicao:
    """Identifica o estado da posicao do braco; qualquer diferenca cancela o plano."""
    quantidade: int = 0
    lado: str | None = None
    ticket: int | None = None

    @staticmethod
    def de(pos: Posicao) -> AssinaturaPosicao:
        lado = pos.lado.value if isinstance(pos.lado, Lado) else None
        return AssinaturaPosicao(pos.quantidade, lado, pos.ticket)


@dataclass
class PlanoArmado:
    braco: str
    plano: PlanoCondicional  # ABSOLUTO
    base: Intencao  # acao, stop/alvo/tempo (distancias), quantidade
    ref: float  # preco de referencia em t0
    t0_ns: int
    t_armado_ns: int
    assinatura: AssinaturaPosicao
    proposta_id: str | None = None
    revisao_id: str | None = None
    decisao_id: str = ""
    hash_dossie: str = ""


@dataclass(frozen=True)
class EventoPlano:
    tipo: TipoEventoPlano
    braco: str
    t_ns: int
    motivo: str
    idade_ns: int
    plano: PlanoArmado

    def como_dict(self) -> dict[str, object]:
        return {"tipo": self.tipo, "braco": self.braco, "t_ns": self.t_ns, "motivo": self.motivo,
                "idade_ns": self.idade_ns, "idade_s": round(self.idade_ns / NS, 3), "ref": self.plano.ref,
                "acao": self.plano.base.acao.value, "proposta_id": self.plano.proposta_id,
                "gatilhos": [descrever(c) for c in self.plano.plano.gatilhos],
                "invalidacoes": [descrever(c) for c in self.plano.plano.invalidacoes],
                "expira_ns": self.plano.plano.expira_ns}


@dataclass
class GerenciadorPlano:
    braco: str
    tick_pontos: float
    validade_disparo_s: float = 30.0  # a intencao gerada no disparo vale este tempo para o motor de risco
    armado: PlanoArmado | None = None
    historico: list[EventoPlano] = field(default_factory=list)

    def _registrar(self, tipo: TipoEventoPlano, p: PlanoArmado, t_ns: int, motivo: str = "") -> EventoPlano:
        ev = EventoPlano(tipo, self.braco, t_ns, motivo, max(0, t_ns - p.t_armado_ns), p)
        self.historico.append(ev)
        return ev

    # ------------------------------------------------------------------------------------------
    def armar(self, base: Intencao, plano_abs: PlanoCondicional, *, ref: float, t0_ns: int, t_ns: int,
              assinatura: AssinaturaPosicao, decisao_id: str = "", hash_dossie: str = "",
              ) -> list[EventoPlano]:
        """Arma o plano (substituindo o anterior, se houver). Recusa plano fora da DSL/expirado."""
        erros = validar_dsl_absoluto(plano_abs, t0_ns)
        if plano_abs.expira_ns <= t_ns:
            erros.append("plano ja expirado ao armar")
        if erros:
            raise ValueError("plano invalido: " + "; ".join(erros))
        out: list[EventoPlano] = []
        if self.armado is not None:
            out.append(self._registrar("substituido", self.armado, t_ns, "novo_plano"))
        novo = PlanoArmado(
            braco=self.braco, plano=plano_abs, base=base, ref=ref, t0_ns=t0_ns, t_armado_ns=t_ns,
            assinatura=assinatura, proposta_id=base.proposta_id, revisao_id=base.revisao_id,
            decisao_id=decisao_id, hash_dossie=hash_dossie)
        self.armado = novo
        out.append(self._registrar("armado", novo, t_ns))
        return out

    def cancelar(self, motivo: str, t_ns: int) -> EventoPlano | None:
        if self.armado is None:
            return None
        p, self.armado = self.armado, None
        return self._registrar("cancelado", p, t_ns, motivo)

    def avaliar(self, est: EstadoPlano, *, assinatura: AssinaturaPosicao, kill_ativo: bool = False,
                janela_evento: bool = False) -> EventoPlano | None:
        """Avalia o plano armado neste instante. Retorna o evento terminal (ou None se segue armado)."""
        p = self.armado
        if p is None:
            return None
        t = est.t_ns
        if kill_ativo:
            return self.cancelar("kill_switch", t)
        if janela_evento:
            return self.cancelar("janela_de_evento", t)
        if assinatura != p.assinatura:
            return self.cancelar("mudanca_de_posicao", t)
        if t > p.plano.expira_ns:
            self.armado = None
            return self._registrar("expirado", p, t, "expirou")
        inv = invalidacao_ativa(p.plano, ref=p.ref, est=est, t_armado_ns=p.t_armado_ns, tick_pontos=self.tick_pontos)
        if inv is not None:
            self.armado = None
            return self._registrar("invalidado", p, t, descrever(inv))
        if gatilhos_satisfeitos(p.plano, ref=p.ref, est=est, t_armado_ns=p.t_armado_ns, tick_pontos=self.tick_pontos):
            self.armado = None
            return self._registrar("disparado", p, t, "gatilhos_satisfeitos")
        return None

    def intencao_disparo(self, ev: EventoPlano, *, preco_real: float, t_ns: int, novo_id: str) -> Intencao:
        """Intencao de entrada no disparo: preco de referencia = preco REAL de execucao (o roteador recalcula
        o stop e o alvo a partir dele, mantendo as distancias do plano) e validade curta para o risco."""
        b = ev.plano.base
        return b.model_copy(update={
            "id": novo_id, "preco_referencia": preco_real, "validade_ns": t_ns + int(self.validade_disparo_s * NS),
            "plano": None, "modo": "condicional"})


def niveis_recalculados(acao_compra: bool, preco_real: float, stop_pontos: float,
                        alvo_pontos: float | None) -> tuple[float, float | None]:
    """Stop e alvo ABSOLUTOS a partir do preco real de execucao (distancias do plano preservadas)."""
    if acao_compra:
        return preco_real - stop_pontos, (preco_real + alvo_pontos) if alvo_pontos else None
    return preco_real + stop_pontos, (preco_real - alvo_pontos) if alvo_pontos else None
