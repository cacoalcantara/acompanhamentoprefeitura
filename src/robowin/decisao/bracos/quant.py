"""Braco A - quant: meta-politica sobre ModeloPreditivo + StatArb (sem LLM, sem indicadores tecnicos).

Regra: entrar quando E[R$ liquido] > theta e P(stop) < limite, no lado de maior E; o StatArb (quando ha
sinal direcional validado) NAO pode contradizer o lado. Le tudo do DOSSIE (saidas dos modelos com status),
nunca de indicadores. Honestidade de status: so opera "limpo" com o modelo ``validado_oos``; com o modelo
apenas ``em_validacao`` o braco opera como EXPLORATORIO e o rotulo vai em todo registro; ``nao_treinado`` e
``reprovado`` nao operam.
"""
from __future__ import annotations

from typing import Any

from robowin.core.tipos import Acao, Condicao, PlanoCondicional

from ..plano_condicional import converter_plano
from .base import NS, BracoBase, ContextoDecisao, Geometria, Modo, ResultadoDecisao

STATUS_LIMPO = "validado_oos"
STATUS_EXPLORATORIO = "em_validacao"
ROTULO_EXPLORATORIO = "exploratorio"


class BracoQuant(BracoBase):
    familia = "quant"

    def __init__(self, id: str = "quant", modo: Modo = "imediato", *, modelo: str, theta_brl: float = 0.0,
                 p_stop_max: float = 0.5, geometria: Geometria | None = None, permitir_exploratorio: bool = True,
                 usar_statarb: bool = True, pullback_pontos: float = 10.0, expira_plano_s: float = 180.0,
                 validade_s: float = 60.0, desvio_max_pts: float = 20.0) -> None:
        super().__init__(id, modo, geometria)
        self.nome_modelo = modelo
        self.theta_brl, self.p_stop_max = theta_brl, p_stop_max
        self.permitir_exploratorio, self.usar_statarb = permitir_exploratorio, usar_statarb
        self.pullback_pontos, self.expira_plano_s = pullback_pontos, expira_plano_s
        self.validade_s, self.desvio_max_pts = validade_s, desvio_max_pts

    def manifesto(self) -> dict[str, Any]:
        m = super().manifesto()
        m.update(modelo=self.nome_modelo, theta_brl=self.theta_brl, p_stop_max=self.p_stop_max,
                 permitir_exploratorio=self.permitir_exploratorio, usar_statarb=self.usar_statarb)
        return m

    def _plano(self, ctx: ContextoDecisao, compra: bool) -> PlanoCondicional:
        pb = self.pullback_pontos
        spread = Condicao(tipo="spread_max_pontos", valor=2 * ctx.tick_pontos)
        if compra:  # espera um recuo para comprar mais barato; abandona se o preco fugir para cima
            gat = (Condicao(tipo="preco_toca", valor=-pb), spread)
            inv = (Condicao(tipo="preco_rompe_acima", valor=2 * pb),)
        else:
            gat = (Condicao(tipo="preco_toca", valor=pb), spread)
            inv = (Condicao(tipo="preco_rompe_abaixo", valor=-2 * pb),)
        rel = PlanoCondicional(gatilhos=gat, invalidacoes=inv, expira_ns=int(self.expira_plano_s * NS))
        return converter_plano(rel, ctx.t0_ns)

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        info = (ctx.dossie.get("modelos") or {}).get(self.nome_modelo) or {}
        status = str(info.get("status", "nao_treinado"))
        saida: dict[str, Any] = info.get("saida") or {}
        base: dict[str, Any] = {"origem": "quant", "modelo": self.nome_modelo, "status_modelo": status,
                                "theta_brl": self.theta_brl, "p_stop_max": self.p_stop_max}
        kw: dict[str, Any] = {"validade_s": self.validade_s, "desvio_max_pts": self.desvio_max_pts}
        if (ctx.posicao.quantidade or 0) > 0:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=("posicao_aberta",),
                                   proposta=base, **kw)
        rotulos: tuple[str, ...] = ()
        if status == STATUS_EXPLORATORIO and self.permitir_exploratorio:
            rotulos = (ROTULO_EXPLORATORIO, f"modelo:{status}")
        elif status != STATUS_LIMPO:
            return self._bloqueada(ctx, (f"modelo_status_inadequado:{status}",), proposta=base, **kw)
        base["rotulo"] = ROTULO_EXPLORATORIO if rotulos else "validado"
        ec, ev = saida.get("e_brl_compra"), saida.get("e_brl_venda")
        if ec is None and ev is None:
            return self._bloqueada(ctx, ("modelo_sem_saida",), proposta=base, rotulos=rotulos, **kw)
        cands: list[tuple[float, bool, float | None]] = []  # (E, compra?, P(stop))
        for e, compra, chave in ((ec, True, "p_stop_compra"), (ev, False, "p_stop_venda")):
            ps = saida.get(chave)
            if e is not None and e > self.theta_brl and (ps is None or ps < self.p_stop_max):
                cands.append((float(e), compra, None if ps is None else float(ps)))
        base.update(e_brl_compra=ec, e_brl_venda=ev, p_stop_compra=saida.get("p_stop_compra"),
                    p_stop_venda=saida.get("p_stop_venda"))
        if not cands:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=("sem_edge",), proposta=base,
                                   rotulos=rotulos, **kw)
        e, compra, ps = max(cands, key=lambda c: c[0])
        d = float((ctx.dossie.get("statarb") or {}).get("direcional_win", 0.0) or 0.0)
        base.update(lado="compra" if compra else "venda", e_brl=e, p_stop=ps, statarb_direcional=d)
        if self.usar_statarb and d != 0.0 and (d > 0) != compra:
            return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar", motivos=("statarb_contradiz",),
                                   proposta=base, rotulos=rotulos, **kw)
        acao = Acao.COMPRAR if compra else Acao.VENDER
        g = self.geometria
        plano = self._plano(ctx, compra) if self.modo == "condicional" else None
        validade_ns = plano.expira_ns if plano is not None else ctx.t0_ns + int(self.validade_s * NS)
        it = self._intencao(ctx, acao, stop=g.stop_pontos, alvo=g.alvo_pontos, tempo_max_s=g.tempo_max_s,
                            validade_ns=validade_ns, plano=plano,
                            modo_intencao="condicional" if plano is not None else "quant")
        if plano is not None:
            kw["plano"] = plano
        return self._resultado(ctx, acao, "plano" if plano is not None else "entrada", intencao=it,
                               proposta=base, rotulos=rotulos, **kw)
