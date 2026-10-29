"""Bracos SOMBRA do planejador (docs/16 WP6 e 4.1): custo de API zero, nunca decidem sozinhos, nunca espelhados.

Uma sombra replica, no MESMO instante, cada entrada executada do braco de referencia (P), com a mesma acao,
quantidade, stop e alvo JA ANCORADOS, tempo, preco de referencia e validade. Muda so a gestao:

* ``sem_gestao``: nenhuma regra nem ajuste (mede o valor da gestao da IA: H3);
* ``trailing_fixo``: gestao deterministica PRE-REGISTRADA (``REGRA_TRAILING_FIXO``): breakeven quando o MFE atinge
  1x o stop e trailing ativado com 1x o stop, a 1x o stop de distancia (a IA bate uma regra simples?).

Cada sombra executa pelo seu proprio roteador e motor de risco (mesmo ``ConfigRisco``; o veto da sombra fica
registrado). ``adecidir`` devolve sempre ``nao_operar``: a sombra nao chama IA nem gera entrada propria.
"""
from __future__ import annotations

from typing import Any, Literal

from robowin.core.tipos import Acao, Intencao, PlanoGestaoCompilado, RegraCompilada

from .base import BracoBase, ContextoDecisao, Geometria, ResultadoDecisao

PoliticaSombra = Literal["sem_gestao", "trailing_fixo"]
POLITICAS_SOMBRA: tuple[PoliticaSombra, ...] = ("sem_gestao", "trailing_fixo")
# regra pre-registrada da sombra trailing (multiplos do stop ancorado da entrada); mudar gera NOVA variante
REGRA_TRAILING_FIXO = {"breakeven_mfe_x_stop": 1.0, "trailing_ativacao_x_stop": 1.0, "trailing_distancia_x_stop": 1.0}
MOTIVO_SOMBRA = "sombra_nao_decide"


class BracoSombra(BracoBase):
    """Sombra passiva de um braco de referencia (``referencia``)."""

    ia = False
    familia = "sombra"
    passivo = True

    def __init__(self, id: str, referencia: str, politica: PoliticaSombra = "sem_gestao", *,
                 intervalo_ajuste_s: float = 2.0) -> None:
        if politica not in POLITICAS_SOMBRA:
            raise ValueError(f"politica de sombra invalida: {politica!r} (use {POLITICAS_SOMBRA})")
        super().__init__(id, "condicional", Geometria(0.0, 0.0, 0))
        self.referencia = referencia
        self.politica: PoliticaSombra = politica
        self.intervalo_ajuste_s = float(intervalo_ajuste_s)

    def manifesto(self) -> dict[str, Any]:
        m = super().manifesto()
        m.update(referencia=self.referencia, politica=self.politica, passivo=True, sem_geometria_fixa=True)
        if self.politica == "trailing_fixo":
            m["regra"] = dict(REGRA_TRAILING_FIXO)
        return m

    def gestao_para(self, it_ref: Intencao) -> PlanoGestaoCompilado | None:
        """Gestao da sombra para a entrada ``it_ref`` (None em ``sem_gestao`` ou sem stop)."""
        stop = float(it_ref.stop_pontos or 0.0)
        if self.politica == "sem_gestao" or stop <= 0:
            return None
        g = REGRA_TRAILING_FIXO
        regras = (
            RegraCompilada(tipo="breakeven", mfe_pontos=round(g["breakeven_mfe_x_stop"] * stop, 10), folga_pontos=0.0),
            RegraCompilada(tipo="trailing", mfe_pontos=round(g["trailing_ativacao_x_stop"] * stop, 10),
                           distancia_pontos=round(g["trailing_distancia_x_stop"] * stop, 10)),
        )
        ref = it_ref.gestao
        passo = ref.passo_min_pontos if ref is not None else 0.0  # 0 => o passo efetivo e o tick (decisao/gestao.py)
        intervalo = ref.intervalo_ajuste_s if ref is not None else self.intervalo_ajuste_s
        return PlanoGestaoCompilado(regras=regras, passo_min_pontos=passo, intervalo_ajuste_s=intervalo)

    def derivar(self, it_ref: Intencao, novo_id: str) -> Intencao:
        """Intencao da sombra a partir da entrada JA AJUSTADA da referencia (mesmo t). Nunca copia ajustes da IA,
        regras de gestao da IA, pedidos de reavaliacao nem ``p_alvo``; a ancoragem ja foi aplicada (``ancora_t0``
        fica None para nao ancorar de novo)."""
        if it_ref.acao not in (Acao.COMPRAR, Acao.VENDER):
            raise ValueError(f"a sombra so replica entradas (acao={it_ref.acao})")
        return Intencao(
            id=novo_id, braco=self.id, acao=it_ref.acao, quantidade=it_ref.quantidade, stop_pontos=it_ref.stop_pontos,
            alvo_pontos=it_ref.alvo_pontos, tempo_max_s=it_ref.tempo_max_s, validade_ns=it_ref.validade_ns,
            modo=it_ref.modo, plano=None, preco_referencia=it_ref.preco_referencia, t0_ns=it_ref.t0_ns,
            gestao=self.gestao_para(it_ref))

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=(MOTIVO_SOMBRA,),
                               proposta={"origem": "sombra", "referencia": self.referencia, "politica": self.politica})
