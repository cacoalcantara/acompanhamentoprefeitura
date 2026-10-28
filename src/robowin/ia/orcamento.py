"""Orcamento de IA: reserva o pior caso ANTES de cada chamada, confirma o custo real depois e libera o resto.

Teto mensal e ritmo por pregao (a verba restante e distribuida pelos pregoes que faltam, para nao
faltar verba a tarde). Esgotado => bloqueia NOVAS chamadas (saidas e protecao nunca dependem de IA).
"""
from __future__ import annotations

import itertools
import math
import threading
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from robowin.core.instrumentos import exigir_mesmo_ativo
from robowin.core.tipos import ChamadaLLM


class OrcamentoEsgotado(Exception):
    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


@dataclass(frozen=True)
class Reserva:
    id: int
    valor_usd: float


class OrcamentoIA:
    def __init__(self, teto_mensal_usd: float, *, pregoes_no_mes: int = 21, gasto_mes_usd: float = 0.0,
                 fator_ritmo: float = 1.0, ativo: str = ""):
        if teto_mensal_usd <= 0:
            raise ValueError("teto mensal deve ser positivo")
        self.teto = float(teto_mensal_usd)
        self.gasto_mes = float(gasto_mes_usd)
        self.gasto_hoje = 0.0
        self.fator_ritmo = fator_ritmo
        self.ativo = ativo  # o orcamento (gasto e ritmo) pertence a UM ativo: nunca e reutilizado em outro
        self.cota_dia = self.teto / max(pregoes_no_mes, 1) * fator_ritmo
        self.dia: str | None = None
        self._reservas: dict[int, float] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def vincular_ativo(self, ativo: str) -> None:
        """Vincula o orcamento ao ativo da sessao; recusa (AtivoDivergente) se ja pertence a outro."""
        if self.ativo:
            exigir_mesmo_ativo(ativo, self.ativo, "orcamento de IA")
        self.ativo = ativo

    # --- ciclo do pregao ---
    def iniciar_pregao(self, dia: str, pregoes_restantes: int) -> None:
        """Redefine a cota do dia: (teto - gasto do mes) / pregoes que faltam (incluindo hoje)."""
        with self._lock:
            self.dia = dia
            self.gasto_hoje = 0.0
            restante = max(self.teto - self.gasto_mes, 0.0)
            self.cota_dia = restante / max(pregoes_restantes, 1) * self.fator_ritmo

    @property
    def reservado(self) -> float:
        return sum(self._reservas.values())

    @property
    def disponivel_mes(self) -> float:
        return self.teto - self.gasto_mes - self.reservado

    @property
    def esgotado(self) -> bool:
        return self.disponivel_mes <= 0

    # --- operacoes ---
    def reservar(self, pior_caso_usd: float) -> Reserva:
        with self._lock:
            if pior_caso_usd < 0:
                raise ValueError("pior caso negativo")
            if self.gasto_mes + self.reservado + pior_caso_usd > self.teto:
                raise OrcamentoEsgotado("teto_mensal")
            if self.gasto_hoje + self.reservado + pior_caso_usd > self.cota_dia:
                raise OrcamentoEsgotado("ritmo_pregao")
            r = Reserva(next(self._ids), pior_caso_usd)
            self._reservas[r.id] = pior_caso_usd
            return r

    def confirmar(self, reserva: Reserva, custo_real_usd: float) -> None:
        with self._lock:
            if self._reservas.pop(reserva.id, None) is None:
                raise KeyError("reserva inexistente ou ja resolvida")
            self.gasto_mes += custo_real_usd
            self.gasto_hoje += custo_real_usd

    def liberar(self, reserva: Reserva) -> None:
        with self._lock:
            self._reservas.pop(reserva.id, None)

    def estado(self) -> dict[str, Any]:
        return {"ativo": self.ativo, "teto": self.teto, "gasto_mes": self.gasto_mes, "gasto_hoje": self.gasto_hoje,
                "reservado": self.reservado, "cota_dia": self.cota_dia, "esgotado": self.esgotado}


def estimar_tokens(texto: str) -> int:
    """Estimativa conservadora (~2,5 caracteres por token)."""
    return math.ceil(len(texto) / 2.5)


class ProvedorComOrcamento:
    """Envolve um ProvedorLLM: reserva -> chama -> confirma/libera. Orcamento esgotado => resposta invalida."""

    def __init__(self, interno: Any, orcamento: OrcamentoIA, catalogo: Any):
        self.interno, self.orcamento, self.catalogo = interno, orcamento, catalogo
        self.nome = interno.nome
        self.bloqueios = 0

    async def gerar_estruturado(
        self, *, modelo: str, sistema: str, mensagem: str, schema: type[BaseModel],
        prazo_s: float, effort: str | None = None, max_tokens: int = 4096,
    ) -> tuple[BaseModel | None, ChamadaLLM]:
        ref = f"{self.nome}:{modelo}"
        tok_ent = estimar_tokens(sistema + mensagem)
        pior = self.catalogo.pior_caso(ref, tok_ent, max_tokens)
        try:
            reserva = self.orcamento.reservar(pior)
        except OrcamentoEsgotado as e:
            self.bloqueios += 1
            return None, ChamadaLLM(papel="", modelo_pedido=ref, erro=f"orcamento_esgotado:{e.motivo}")
        try:
            obj, ch = await self.interno.gerar_estruturado(
                modelo=modelo, sistema=sistema, mensagem=mensagem, schema=schema,
                prazo_s=prazo_s, effort=effort, max_tokens=max_tokens)
        except BaseException:
            # cancelamento/erro inesperado: cobra o pior caso (conservador) e propaga
            self.orcamento.confirmar(reserva, pior)
            raise
        custo = ch.custo_usd
        if custo == 0.0 and ch.erro == "timeout":
            # a requisicao pode ter sido cobrada: assume ao menos a entrada
            custo = self.catalogo.custo_tokens(ref, tok_ent, 0, 0)
        if custo == 0.0 and ch.erro and ch.tokens_entrada == 0:
            self.orcamento.liberar(reserva)
        else:
            self.orcamento.confirmar(reserva, custo)
        return obj, ch
