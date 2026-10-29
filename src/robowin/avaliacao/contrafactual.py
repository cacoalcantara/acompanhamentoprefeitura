"""Contrafactuais das decisoes do planejador (docs/16 secao 3, WP9). SO DIAGNOSTICO: nunca metrica primaria e
nunca altera limites de risco (nada aqui le ``ConfigRisco``).

* ``replay_operacao``: refaz UMA operacao num ``SimuladorExecucao`` NOVO, com ``RelogioSimulado``, as mesmas regras
  de preenchimento (mercado na ponta contraria, stop pelo ultimo negocio com slippage Pareto, alvo como limitada) e a
  mesma latencia. Sem gestao, a saida e por stop, alvo ou tempo (``tempo_max_s`` desde a abertura). Com gestao
  (``PlanoGestaoCompilado``), o caminho de acoes vem de ``decisao.gestao.simular_caminho`` e e aplicado no
  simulador conforme os ticks chegam (apertos de stop, parciais, encerramentos por regra, prazo estendido).
* ``contrafactuais_da_sessao``: para cada previsao do planejador (``previsoes_ia``):

  - ``plano_sem_gestao`` (sempre): o plano original sem gestao, a partir da entrega da decisao; o seu desfecho e o
    ``y_alvo`` gravado em ``previsoes_ia`` (``fonte_y = replay_sem_gestao``);
  - entradas EXECUTADAS: ``sem_gestao`` e ``trailing_fixo`` (regra deterministica pre-registrada, ``TRAILING_FIXO``);
  - entradas NAO executadas (faixa/ancoragem, revisor, veto do motor, plano expirado): ``plano_ia`` (o plano da IA,
    com a gestao declarada, a partir da entrega).

  Todas as politicas de uma decisao usam a MESMA semente ``sha256(decisao_id)`` (numeros aleatorios comuns).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.relogio import RelogioSimulado
from robowin.core.tipos import (
    Acao,
    ComandoOrdem,
    Intencao,
    Lado,
    LivroSnapshot,
    PlanoGestaoCompilado,
    RegraCompilada,
    Tick,
    Trade,
)
from robowin.dados import decisao_ia
from robowin.execucao.custos import Custos
from robowin.execucao.preenchimento import ParamsSlippage, cotacao_valida
from robowin.execucao.simulador import RET_DONE, RET_PLACED, SimuladorExecucao

NS = 1_000_000_000
BRACO_REPLAY = "contrafactual"
MAGIC_REPLAY = 1
MOTIVOS_DO_SIMULADOR = ("alvo", "stop")  # saidas que o proprio simulador executa (OCO no servidor)
INTERVALO_AJUSTE_TRAILING_S = 2.0  # igual ao padrao de [planejador].intervalo_ajuste_s
# Regra deterministica PRE-REGISTRADA da sombra/contrafactual "trailing fixo": ativa quando a excursao favoravel
# atinge 1 stop inicial e segue o extremo a 1 stop inicial de distancia (fatores do stop, nao pontos do ativo).
TRAILING_FIXO = {"ativacao_em_stops": 1, "distancia_em_stops": 1}


@dataclass(frozen=True)
class ResultadoReplay:
    liquido: float  # moeda da conta, custos incluidos (posicao aberta no fim dos dados: marcada a mercado)
    motivo: str  # alvo | stop | tempo | regra:<motivo> | fim_dos_dados | sem_entrada
    t_fim: int
    y_alvo: int  # 1 so se a posicao saiu pelo alvo
    t_entrada_ns: int = 0
    preco_entrada: float = 0.0
    trades: tuple[Trade, ...] = ()
    acoes_gestao: int = 0


def semente_de(decisao_id: str) -> int:
    """Semente estavel (63 bits) de ``sha256(decisao_id)``: mesma decisao, mesmos numeros aleatorios."""
    return int(hashlib.sha256(str(decisao_id).encode("utf-8")).hexdigest()[:15], 16)


def trailing_fixo(stop_pontos: float, tick_pontos: float) -> PlanoGestaoCompilado:
    """Gestao deterministica pre-registrada (``TRAILING_FIXO``) para uma operacao com stop inicial ``stop_pontos``."""
    def grade(x: float) -> float:
        return max(tick_pontos, round(round(x / tick_pontos) * tick_pontos, 10))
    regra = RegraCompilada(tipo="trailing", mfe_pontos=grade(stop_pontos * TRAILING_FIXO["ativacao_em_stops"]),
                           distancia_pontos=grade(stop_pontos * TRAILING_FIXO["distancia_em_stops"]))
    return PlanoGestaoCompilado(regras=(regra,), passo_min_pontos=tick_pontos,
                                intervalo_ajuste_s=INTERVALO_AJUSTE_TRAILING_S)


def _gestao_mod() -> Any:
    try:
        return importlib.import_module("robowin.decisao.gestao")
    except ModuleNotFoundError as e:  # pragma: no cover - depende do WP5
        raise RuntimeError("replay com gestao exige robowin.decisao.gestao (simular_caminho)") from e


def _arred(preco: float, tick: float, para_cima: bool) -> float:
    """Mesmo arredondamento do roteador: niveis sempre em direcao ao preco (stop mais curto, nunca mais largo)."""
    return (math.ceil(preco / tick - 1e-9) if para_cima else math.floor(preco / tick + 1e-9)) * tick


def _lado(lado: Lado | str) -> Lado:
    return lado if isinstance(lado, Lado) else Lado(str(lado).lower())


def replay_operacao(ticks: Sequence[Tick], livros: Iterable[LivroSnapshot], perfil: PerfilInstrumento, *,
                    t_entrada_ns: int, lado: Lado | str, stop: float, alvo: float | None, tempo_max_s: int, q: int,
                    gestao: PlanoGestaoCompilado | None, seed: int, referencia: float | None = None,
                    latencia_ns: int = 0, slippage: ParamsSlippage | None = None, custos: Custos | None = None,
                    simbolo: str | None = None) -> ResultadoReplay:
    """Refaz uma operacao: ordem a mercado no primeiro tick com ``t >= t_entrada_ns`` (depois de o simulador ver o
    tick, como no orquestrador), SL/TP a ``stop``/``alvo`` pontos de ``referencia`` (padrao: o meio do tick de
    envio; com ancoragem em t0, passe a ancora) e saida por tempo ``tempo_max_s`` depois da abertura."""
    if q < 1 or stop <= 0 or tempo_max_s <= 0:
        raise ValueError("replay exige q >= 1, stop > 0 e tempo_max_s > 0")
    lado_e = _lado(lado)
    ins = perfil.instrumento(simbolo)
    tk = ins.tick_pontos
    ts = sorted(ticks, key=lambda x: x.t_utc_ns)
    lv = sorted(livros, key=lambda x: x.t_utc_ns)
    if not ts:
        return ResultadoReplay(0.0, "sem_entrada", t_entrada_ns, 0)
    rel = RelogioSimulado(min(ts[0].t_utc_ns, t_entrada_ns))
    sim = SimuladorExecucao(ins, rel, custos or Custos.do_instrumento(ins), latencia_ns=latencia_ns, seed=seed,
                            slippage=slippage)
    sim.registrar_braco(BRACO_REPLAY, MAGIC_REPLAY)
    cart = sim.carteira(BRACO_REPLAY)
    enviado = aberto = False
    ref = 0.0
    prazo = 0
    caminho: list[tuple[int, Any]] = []
    k_acao = n_acoes = 0
    saida_pendente: str | None = None
    motivo_saida: dict[int, str] = {}
    j = 0
    for i, tick in enumerate(ts):
        t = tick.t_utc_ns
        rel.avancar_para(max(rel.agora_ns(), t))
        while j < len(lv) and lv[j].t_utc_ns <= t:
            sim.on_livro(lv[j])
            j += 1
        for ev in sim.on_tick(tick):
            if ev.tipo == "trade" and ev.trade is not None and cart.pos.qtd > 0:
                saida_pendente = None  # parcial executada: libera nova saida
        pos = cart.pos
        if not enviado and t >= t_entrada_ns:
            enviado = True
            ref = float(referencia) if referencia is not None else tick.meio
            compra = lado_e == Lado.COMPRA
            sl = _arred(ref - stop, tk, True) if compra else _arred(ref + stop, tk, False)
            tp = None
            if alvo:
                tp = _arred(ref + alvo, tk, False) if compra else _arred(ref - alvo, tk, True)
            ack = sim.enviar(ComandoOrdem(intencao_id="contrafactual", magic=MAGIC_REPLAY, lado=lado_e,
                                          tipo="mercado", quantidade=q, sl=sl, tp=tp))
            if ack.retcode not in (RET_PLACED, RET_DONE):
                return ResultadoReplay(0.0, "sem_entrada", t, 0)
            continue
        if enviado and not aberto and (pos.qtd > 0 or cart.trades):
            aberto = True
            prazo = (pos.aberta_ns if pos.qtd > 0 else cart.trades[0].t_entrada_ns) + int(tempo_max_s) * NS
            if gestao is not None and pos.qtd > 0:
                g = _gestao_mod()
                it = Intencao(id="contrafactual", braco=BRACO_REPLAY,
                              acao=Acao.COMPRAR if lado_e == Lado.COMPRA else Acao.VENDER, quantidade=q,
                              stop_pontos=stop, alvo_pontos=alvo, tempo_max_s=tempo_max_s, validade_ns=t,
                              preco_referencia=ref)
                est0 = g.iniciar_estado(sim.posicao(BRACO_REPLAY), it, tempo_max_s)
                caminho = list(g.simular_caminho(gestao, est0, ts[i:], tick_pontos=tk, por_cotacao=sim.por_cotacao))
        if aberto and pos.qtd == 0:
            break
        if not aberto:
            continue
        # acoes de gestao ja devidas neste instante (o caminho foi calculado sobre os mesmos ticks)
        while k_acao < len(caminho) and caminho[k_acao][0] <= t and pos.qtd > 0:
            acao = caminho[k_acao][1]
            k_acao += 1
            nome = type(acao).__name__
            if nome == "ApertarStop":
                n_acoes += int(sim.modificar_stop(BRACO_REPLAY, float(acao.nivel)))
            elif nome == "EstenderPrazo":
                prazo, n_acoes = int(acao.prazo_ns), n_acoes + 1
            elif nome == "Reduzir" and saida_pendente is None and 0 < int(acao.q) < pos.qtd:
                saida_pendente = _sair(sim, pos.qtd, int(acao.q), f"regra:{acao.regra}", motivo_saida)
                n_acoes += 1
            elif nome == "Encerrar" and saida_pendente is None:
                m = str(acao.motivo)
                if m in MOTIVOS_DO_SIMULADOR or m.startswith("stop") or m == "tempo":
                    continue  # stop/alvo ficam com o simulador; o tempo com o prazo abaixo
                saida_pendente = _sair(sim, pos.qtd, pos.qtd, f"regra:{m}", motivo_saida)
                n_acoes += 1
        if pos.qtd > 0 and saida_pendente is None and t >= prazo:
            saida_pendente = _sair(sim, pos.qtd, pos.qtd, "tempo", motivo_saida)
    if not aberto:
        return ResultadoReplay(0.0, "sem_entrada", ts[-1].t_utc_ns, 0)
    trades = tuple(cart.trades)
    liquido = sum(tr.liquido_brl for tr in trades)
    if cart.pos.qtd > 0:  # dados acabaram com a posicao aberta: marca a mercado pela ponta de saida (sem custo extra)
        bid, ask = cotacao_valida(ts[-1])
        p = cart.pos
        sinal = 1 if p.lado == Lado.COMPRA else -1
        saida = bid if p.lado == Lado.COMPRA else ask
        liquido += (saida - p.preco_medio) * sinal * p.qtd * ins.ponto_valor_brl * (sim.fator(ts[-1].t_utc_ns) or 1.0)
        return ResultadoReplay(liquido, "fim_dos_dados", ts[-1].t_utc_ns, 0, trades[0].t_entrada_ns if trades
                               else p.aberta_ns, p.preco_medio, trades, n_acoes)
    ult = trades[-1]
    motivo = (ult.motivo_saida if ult.motivo_saida in MOTIVOS_DO_SIMULADOR
              else motivo_saida.get(len(trades) - 1, ult.motivo_saida))
    return ResultadoReplay(liquido, motivo, ult.t_saida_ns, int(motivo == "alvo"), trades[0].t_entrada_ns,
                           trades[0].entrada, trades, n_acoes)


def _sair(sim: SimuladorExecucao, qtd_pos: int, qtd: int, motivo: str, motivos: dict[int, str]) -> str | None:
    p = sim.carteira(BRACO_REPLAY).pos
    lado = Lado.VENDA if p.lado == Lado.COMPRA else Lado.COMPRA
    ack = sim.enviar(ComandoOrdem(intencao_id=f"contrafactual:{motivo}", magic=MAGIC_REPLAY, lado=lado, tipo="saida",
                                  quantidade=min(qtd, qtd_pos), position_ticket=p.ticket))
    if ack.retcode not in (RET_PLACED, RET_DONE):
        return None
    motivos[len(sim.carteira(BRACO_REPLAY).trades)] = motivo  # indice do trade que esta saida vai gerar
    return motivo


# ======================================================================================= sessao inteira
@dataclass
class _Plano:
    decisao_id: str
    braco: str
    lado: str
    stop: float
    alvo: float | None
    tempo_s: int
    t_entrega_ns: int
    ancora: float | None
    executada: bool
    gestao: PlanoGestaoCompilado | None = None
    q: int = 1
    extras: dict[str, Any] = field(default_factory=dict)


def _banco(banco: Any) -> Any:
    if isinstance(banco, (str, Path)):
        from robowin.dados.banco import BancoSQLite
        return BancoSQLite(banco)
    return banco


def _consultar(banco: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    try:
        return list(banco.consultar(sql, params))
    except Exception:  # noqa: BLE001 - banco antigo sem a tabela: nada a fazer
        return []


def _intencao(js: Any) -> Intencao | None:
    if not js:
        return None
    try:
        return Intencao.model_validate(json.loads(js) if isinstance(js, str) else js)
    except (ValueError, TypeError):
        return None


def _planos(banco: Any) -> list[_Plano]:
    decs = {r["id"]: r for r in _consultar(banco, "SELECT id, t_ns, intencao_json FROM decisoes")}
    out: list[_Plano] = []
    for r in _consultar(banco, f"SELECT * FROM {decisao_ia.PREVISOES} ORDER BY t0_ns, id"):
        lado = str(r.get("lado") or "").lower()
        stop, tempo = r.get("stop_pts"), r.get("tempo_s")
        if lado not in (Lado.COMPRA.value, Lado.VENDA.value) or not stop or stop <= 0 or not tempo or tempo <= 0:
            continue
        did = str(r.get("decisao_id") or r["id"])
        dec = decs.get(did, {})
        it = _intencao(dec.get("intencao_json"))
        out.append(_Plano(decisao_id=did, braco=str(r.get("braco") or ""), lado=lado, stop=float(stop),
                          alvo=float(r["alvo_pts"]) if r.get("alvo_pts") else None, tempo_s=int(tempo),
                          t_entrega_ns=int(dec.get("t_ns") or r.get("t0_ns") or 0),
                          ancora=float(r["ancora"]) if r.get("ancora") is not None else None,
                          executada=bool(r.get("executada")), gestao=it.gestao if it else None,
                          q=max(1, int(it.quantidade)) if it else 1, extras={"previsao_id": r["id"]}))
    return out


def _perfil_do_banco(banco: Any) -> PerfilInstrumento:
    from robowin.core.config import carregar_toml
    from robowin.core.instrumentos import perfil_da_config

    from .cli_cmds import ativo_do_banco
    caminho = getattr(banco, "caminho", None)
    ativo = ativo_do_banco(caminho) if caminho and caminho != ":memory:" else None
    cfg = carregar_toml("config/robowin.toml") if Path("config/robowin.toml").is_file() else {}
    return perfil_da_config(cfg, ativo, permitir_a_confirmar=True)


def contrafactuais_da_sessao(banco: Any, ticks: Sequence[Tick], *, perfil: PerfilInstrumento | None = None,
                             livros: Sequence[LivroSnapshot] = (), latencia_ns: int = 0,
                             slippage: ParamsSlippage | None = None, gravar: bool = True) -> list[dict[str, Any]]:
    """Contrafactuais de todas as previsoes do planejador gravadas no ``banco`` (``BancoSQLite`` ou caminho).

    Grava ``contrafactuais`` e resolve ``previsoes_ia.y_alvo`` pelo replay do plano original sem gestao (com
    ``gravar``). Devolve as linhas de ``contrafactuais``. Banco sem as tabelas => lista vazia."""
    b = _banco(banco)
    planos = _planos(b)
    if not planos:
        return []
    perfil = perfil or _perfil_do_banco(b)
    tick_pts = perfil.tick_pontos
    linhas: list[dict[str, Any]] = []
    resolucoes: list[tuple[int, str, int, str]] = []
    for pl in planos:
        seed = semente_de(pl.decisao_id)
        kw: dict[str, Any] = dict(t_entrada_ns=pl.t_entrega_ns, lado=pl.lado, stop=pl.stop, alvo=pl.alvo,
                                  tempo_max_s=pl.tempo_s, q=pl.q, seed=seed, referencia=pl.ancora,
                                  latencia_ns=latencia_ns, slippage=slippage)
        base = replay_operacao(ticks, livros, perfil, gestao=None, **kw)
        politicas: list[tuple[str, ResultadoReplay]] = [("plano_sem_gestao", base)]
        if pl.executada:
            politicas.append(("sem_gestao", base))
            politicas.append(("trailing_fixo", replay_operacao(ticks, livros, perfil,
                                                               gestao=trailing_fixo(pl.stop, tick_pts), **kw)))
        else:
            com = base if pl.gestao is None else replay_operacao(ticks, livros, perfil, gestao=pl.gestao, **kw)
            politicas.append(("plano_ia", com))
        for nome, r in politicas:
            linhas.append(decisao_ia.linha(decisao_ia.CONTRAFACTUAIS, {
                "id": f"{pl.decisao_id}:{nome}", "decisao_id": pl.decisao_id, "braco_ref": pl.braco,
                "politica": nome, "liquido": round(r.liquido, 10), "motivo": r.motivo, "t_fim_ns": r.t_fim,
                "y_alvo": r.y_alvo if r.motivo != "sem_entrada" else None, "seed": seed}))
        if base.motivo not in ("sem_entrada", "fim_dos_dados"):
            resolucoes.append((base.y_alvo, "replay_sem_gestao", base.t_fim, str(pl.extras["previsao_id"])))
    if gravar and decisao_ia.garantir_tabelas(b):
        with b.transacao():
            for ln in linhas:
                b.inserir(decisao_ia.CONTRAFACTUAIS, ln)
            for y, fonte, t_fim, pid in resolucoes:
                b.executar(f"UPDATE {decisao_ia.PREVISOES} SET y_alvo=?, fonte_y=?, resolvido_ns=? WHERE id=?",
                           (y, fonte, t_fim, pid))
    return linhas
