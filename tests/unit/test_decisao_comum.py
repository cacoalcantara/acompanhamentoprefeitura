"""Utilitarios compartilhados pelos testes de decisao (sem testes proprios alem do de sanidade)."""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any

from robowin.core.calendario_b3 import dt_utc_ns
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    ConfigRisco,
    Instrumento,
    PlanoCondicional,
    Tick,
)
from robowin.dados.banco import BancoSQLite
from robowin.decisao.bracos.base import BracoBase, ContextoDecisao, ResultadoDecisao
from robowin.decisao.gatilhos import ConfigGatilhos
from robowin.decisao.orquestrador import ConfigOrquestrador, Orquestrador
from robowin.decisao.registro import Registro
from robowin.ia.dossie import hash_dossie, montar_dossie

T0 = dt_utc_ns(dt.datetime(2026, 11, 10, 10, 0))  # terca, dentro da janela de entradas
MS = 1_000_000
NS = 1_000_000_000
INS = Instrumento(simbolo="WINZ26")


def tk(dt_ms: int, bid: float, ask: float | None = None, last: float | None = None, vol: float = 1.0) -> Tick:
    t = T0 + dt_ms * MS
    ask = bid + 5.0 if ask is None else ask
    return Tick(t_servidor_ms=t // MS, t_utc_ns=t, t_recebido_ns=t, bid=bid, ask=ask,
                last=last if last is not None else bid, volume=vol, flags=8)


def serie(precos_bid: Sequence[float], passo_ms: int = 1000, inicio_ms: int = 0) -> list[Tick]:
    return [tk(inicio_ms + i * passo_ms, p) for i, p in enumerate(precos_bid)]


def dossie_basico(posicao: dict[str, Any] | None = None, t0: int = T0, extra_modelos: dict | None = None,
                  statarb: dict[str, float] | None = None) -> dict[str, Any]:
    """Dossie anonimizado real (montar_dossie) com ~15 min de precos sinteticos."""
    ticks = [(t0 - (900 - i) * NS, 130_000.0 + (i % 40) * 5.0 - (i // 100) * 5.0) for i in range(901)]
    return montar_dossie(
        t0_ns=t0, ticks=ticks, spread_pontos=5.0, features={"ret_5s": 1.0}, features_permitidas={"ret_5s"},
        modelos=extra_modelos or {}, statarb=statarb, regime="normal", posicao=posicao, limites={"contratos_max": 1},
        minutos_desde_abertura=60, minutos_para_zeragem=400)


def ctx_basico(dossie: dict[str, Any] | None = None, posicao: Any = None, t0: int = T0, ciclo: str = "c1",
               preco: float = 130_000.0) -> ContextoDecisao:
    from robowin.core.tipos import Posicao
    d = dossie if dossie is not None else dossie_basico()
    return ContextoDecisao(t0_ns=t0, dossie=d, hash_dossie=hash_dossie(d), preco_ref=preco, bid=preco - 2.5,
                           ask=preco + 2.5, posicao=posicao or Posicao(simbolo="WINZ26"), ciclo_id=ciclo)


def plano_rel(toca: float = -10.0, expira_s: int = 120, invalida_acima: float | None = 50.0) -> PlanoCondicional:
    from robowin.core.tipos import Condicao
    inv = (Condicao(tipo="preco_rompe_acima", valor=invalida_acima),) if invalida_acima is not None else ()
    return PlanoCondicional(gatilhos=(Condicao(tipo="preco_toca", valor=toca),), invalidacoes=inv,
                            expira_ns=expira_s * NS)


# --------------------------------------------------------------------------------------------------
# braco roteirizado + montagem do orquestrador
# --------------------------------------------------------------------------------------------------
Passo = Callable[["BracoRoteirizado", ContextoDecisao], ResultadoDecisao]


class BracoRoteirizado(BracoBase):
    """Braco de teste: cada ciclo executa o proximo passo do roteiro (o ultimo se repete)."""
    familia = "teste"

    def __init__(self, id: str, passos: list[Passo], modo: str = "imediato", ia: bool = False) -> None:
        super().__init__(id, modo)  # type: ignore[arg-type]
        self.passos = passos
        self.i = 0
        self.ia = ia
        self.contextos: list[ContextoDecisao] = []

    async def adecidir(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        self.contextos.append(ctx)
        fn = self.passos[min(self.i, len(self.passos) - 1)]
        self.i += 1
        return fn(self, ctx)

    # fabricas de resultado ---------------------------------------------------------------------
    def entrada(self, ctx: ContextoDecisao, acao: Acao = Acao.COMPRAR, *, stop: float = 30.0, alvo: float | None = 30.0,
                tempo: int = 60, plano_abs: PlanoCondicional | None = None, latencia_ns: int = 0,
                quantidade: int = 1, validade_s: float = 60.0, desvio: float = 20.0) -> ResultadoDecisao:
        it = self._intencao(ctx, acao, stop=stop, alvo=alvo, tempo_max_s=tempo,
                            validade_ns=(plano_abs.expira_ns if plano_abs else ctx.t0_ns + int(validade_s * NS)),
                            plano=plano_abs)
        if quantidade != 1:
            it = it.model_copy(update={"quantidade": quantidade})
        return self._resultado(ctx, acao, "plano" if plano_abs else "entrada", intencao=it, latencia_ns=latencia_ns,
                               validade_s=validade_s, desvio_max_pts=desvio, plano=plano_abs)

    def nada(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        return self._resultado(ctx, Acao.NAO_OPERAR, "nao_operar")

    def encerrar(self, ctx: ContextoDecisao) -> ResultadoDecisao:
        it = self._intencao(ctx, Acao.ENCERRAR, stop=None, alvo=None, tempo_max_s=None, validade_ns=ctx.t0_ns + 60 * NS)
        return self._resultado(ctx, Acao.ENCERRAR, "saida", intencao=it)

    def bloqueada(self, ctx: ContextoDecisao, *motivos: str) -> ResultadoDecisao:
        return self._bloqueada(ctx, tuple(motivos) or ("ia:erro_http",))


def montar_orq(bracos: list[BracoBase], *, risco: ConfigRisco | None = None, eventos: Sequence[Any] = (),
               latencia_exec_ns: int = 0, bloqueio: Any = None, passo_s: float = 1.0,
               gatilhos: ConfigGatilhos | None = None, orcamento: Any = None,
               **kw: Any) -> tuple[Orquestrador, BancoSQLite, RelogioSimulado]:
    rel = RelogioSimulado(T0)
    banco = BancoSQLite(":memory:")
    reg = Registro(banco, None, "t")
    cfg = ConfigOrquestrador(
        instrumento=INS, risco=risco or ConfigRisco(), sessao_id="t", passo_s=passo_s, checagem_s=1.0,
        exigir_aquecimento=False, latencia_exec_ns=latencia_exec_ns,
        gatilhos=gatilhos or ConfigGatilhos(periodo_s=1.0, intervalo_min_s=1.0, e_brl_min=1e18, z_statarb_min=1e18,
                                            evento_posicao=False, max_chamadas_dia=None), **kw)
    orq = Orquestrador(cfg, relogio=rel, registro=reg, bracos=bracos, eventos=list(eventos), bloqueio=bloqueio,
                       orcamento=orcamento)
    return orq, banco, rel


async def alimentar(orq: Orquestrador, ticks: Sequence[Tick]) -> None:
    for t in ticks:
        await orq.on_tick(t)


def test_utilitarios_de_decisao_sanidade():
    d = dossie_basico()
    assert "movimento" in d and d["posicao"]["quantidade"] == 0
    assert tk(0, 130000.0).t_utc_ns == T0
