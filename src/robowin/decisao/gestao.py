"""Gestao automatica da posicao do braco planejador (docs/16 WP5): codigo PURO executado a cada tick.

A IA declara regras (``RegraGestao``) que o compilador poe na grade do tick (``PlanoGestaoCompilado``); aqui elas
viram ACOES deterministicas entre as chamadas de IA. Nada aqui envia ordem nem conhece limites: cada acao vira uma
``Intencao`` que o orquestrador passa pelo roteador e pelo ``MotorRiscoPadrao`` (veto final).

Regras:

=================  =====================================================================================
``breakeven``      com MFE >= X, stop em entrada +/- (custo ida e volta + folga)
``trailing``       com MFE >= ativacao, stop = extremo favoravel -/+ distancia
``parcial``        com MFE >= X, reduz ``quantidade`` (inerte com q=1; uma vez por posicao)
``sem_progresso``  depois de T s, se o MFE < Y, encerra
``apertar_apos``   em t >= T, stop = preco -/+ distancia
``ao_vencer_tempo`` no prazo: ``encerrar``; ou ``proteger_e_estender`` so se o stop ja trava lucro LIQUIDO,
                   estendendo o prazo ate ``teto_prazo_ns``
=================  =====================================================================================

Garantias: o stop so aperta (monotonico), fica na grade de ``passo_min_pontos``, com pelo menos
``intervalo_ajuste_s`` entre pedidos de ajuste e sempre a pelo menos ``dist_min`` do preco EXECUTAVEL de saida;
a quantidade nunca cresce; o prazo nunca passa do teto. Os eventos de reavaliacao (``perto_do_stop``,
``perto_do_alvo``, ``metade_do_tempo``, ``regra_disparou``, ``lucro_acima_stop``) saem uma vez por posicao e so
quando pedidos em ``reavaliar_se``.

Estado: ``avaliar_gestao`` so marca no estado o que ja foi EMITIDO (regras de uma vez, eventos e o instante do
ultimo pedido de ajuste). O efeito de uma acao (stop, quantidade, prazo) so entra no estado por ``aplicar_acao``,
quando o roteador a aplicou de fato, ou por ``sincronizar_posicao`` com a posicao real.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from robowin.core.tipos import Intencao, Lado, PlanoGestaoCompilado, Posicao

NS = 1_000_000_000
EVENTOS_REAVALIACAO = ("perto_do_stop", "perto_do_alvo", "metade_do_tempo", "regra_disparou", "lucro_acima_stop")
_EPS = 1e-9


# --------------------------------------------------------------------------------------------------
# acoes
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ApertarStop:
    nivel: float  # preco absoluto do novo stop (so aperta)
    regra: str


@dataclass(frozen=True)
class Reduzir:
    q: int  # unidades a realizar (1..q-1)
    regra: str


@dataclass(frozen=True)
class Encerrar:
    motivo: str


@dataclass(frozen=True)
class EstenderPrazo:
    prazo_ns: int  # novo prazo absoluto (<= teto)
    regra: str


@dataclass(frozen=True)
class EventoReavaliacao:
    nome: str


AcaoGestao = ApertarStop | Reduzir | Encerrar | EstenderPrazo | EventoReavaliacao


# --------------------------------------------------------------------------------------------------
# estado
# --------------------------------------------------------------------------------------------------
@dataclass
class EstadoGestao:
    lado: Lado
    entrada: float
    qtd_inicial: int
    qtd: int
    aberta_ns: int
    prazo_ns: int
    sl: float | None
    tp: float | None
    extremo_favoravel: float
    mfe_pontos: float = 0.0
    mae_pontos: float = 0.0
    regras_disparadas: set[int] = field(default_factory=set)
    ultimo_ajuste_ns: int | None = None
    eventos_emitidos: set[str] = field(default_factory=set)
    ajustes_ia: int = 0
    ajustes_auto: int = 0
    parciais: int = 0
    liquido_parcial: float = 0.0
    origem_ultimo_stop: str = "inicial"  # inicial | auto | ia
    stop_inicial_pontos: float = 0.0  # distancia entrada-stop na abertura (referencia de "perto" e "lucro > stop")

    @property
    def compra(self) -> bool:
        return self.lado == Lado.COMPRA

    @property
    def sinal(self) -> int:
        return 1 if self.compra else -1


def iniciar_estado(pos: Posicao, it: Intencao, tempo_max_s: int) -> EstadoGestao:
    """Estado na abertura: niveis da POSICAO real (``pos.stop``/``pos.alvo``) e prazo = abertura + tempo."""
    if pos.lado is None or pos.quantidade <= 0:
        raise ValueError("iniciar_estado exige posicao aberta")
    if tempo_max_s <= 0:
        raise ValueError("tempo_max_s deve ser positivo")
    stop_ini = abs(pos.preco_medio - pos.stop) if pos.stop is not None else float(it.stop_pontos or 0.0)
    return EstadoGestao(
        lado=pos.lado, entrada=pos.preco_medio, qtd_inicial=pos.quantidade, qtd=pos.quantidade,
        aberta_ns=pos.aberta_ns, prazo_ns=pos.aberta_ns + int(tempo_max_s) * NS, sl=pos.stop, tp=pos.alvo,
        extremo_favoravel=pos.preco_medio, stop_inicial_pontos=round(stop_ini, 10))


def preco_disparo(est: EstadoGestao, *, bid: float, ask: float, last: float, por_cotacao: bool) -> float:
    """Mesma referencia do disparo do stop no simulador: ultimo negocio (bolsa) ou cotacao de saida (forex/CFD)."""
    saida = bid if est.compra else ask
    if por_cotacao or not (math.isfinite(last) and last > 0):
        return saida
    return last


def _finitos(*v: float) -> bool:
    return all(math.isfinite(x) for x in v)


def atualizar_excursao(est: EstadoGestao, *, bid: float, ask: float, last: float, por_cotacao: bool) -> None:
    """Atualiza extremo favoravel, MFE e MAE (pontos, >= 0) pela referencia de disparo do stop."""
    if not _finitos(bid, ask) or bid <= 0 or ask <= 0:
        return
    p = preco_disparo(est, bid=bid, ask=ask, last=last, por_cotacao=por_cotacao)
    s = est.sinal
    if s * (p - est.extremo_favoravel) > 0:
        est.extremo_favoravel = p
    r = s * (p - est.entrada)
    est.mfe_pontos = max(est.mfe_pontos, round(r, 10))
    est.mae_pontos = max(est.mae_pontos, round(-r, 10))


def resultado_pontos(est: EstadoGestao, *, bid: float, ask: float) -> float:
    """Resultado bruto em pontos pela cotacao de saida (o que se realizaria agora)."""
    return round(est.sinal * ((bid if est.compra else ask) - est.entrada), 10)


# --------------------------------------------------------------------------------------------------
# avaliacao
# --------------------------------------------------------------------------------------------------
def _quantizar(nivel: float, passo: float, compra: bool) -> float:
    """Na grade do passo, para o lado FOLGADO (compra: para baixo; venda: para cima): nunca encosta no preco."""
    q = math.floor(nivel / passo + _EPS) if compra else math.ceil(nivel / passo - _EPS)
    return round(q * passo, 10)


def _candidatos_stop(plano: PlanoGestaoCompilado, est: EstadoGestao, *, ref: float, decorrido_s: float,
                     custo: float, ativadas: set[int]) -> list[tuple[float, str]]:
    s = est.sinal
    out: list[tuple[float, str]] = []
    for i, r in enumerate(plano.regras):
        mfe_min = float(r.mfe_pontos or 0.0)
        if r.tipo == "breakeven" and est.mfe_pontos >= mfe_min - _EPS:
            out.append((est.entrada + s * (custo + float(r.folga_pontos or 0.0)), r.tipo))
            ativadas.add(i)
        elif r.tipo == "trailing" and r.distancia_pontos and est.mfe_pontos >= mfe_min - _EPS:
            out.append((est.extremo_favoravel - s * float(r.distancia_pontos), r.tipo))
            ativadas.add(i)
        elif r.tipo == "apertar_apos" and r.distancia_pontos and r.apos_s is not None and decorrido_s >= r.apos_s:
            out.append((ref - s * float(r.distancia_pontos), r.tipo))
            ativadas.add(i)
    return out


def avaliar_gestao(plano: PlanoGestaoCompilado, est: EstadoGestao, *, bid: float, ask: float, last: float,
                   t_ns: int, tick: float, dist_min: float, custo_ida_volta_pontos: float,
                   teto_prazo_ns: int, por_cotacao: bool, perto_frac: float) -> list[AcaoGestao]:
    """Acoes de gestao neste instante (vazio = nada a fazer). ``Encerrar`` e terminal e vem sozinho.

    ``dist_min``: distancia minima stop-preco executavel (o chamador soma o spread). ``custo_ida_volta_pontos``: por
    unidade, em pontos (breakeven e "trava lucro liquido"). ``teto_prazo_ns``: o prazo nunca passa disto."""
    if est.qtd <= 0 or not _finitos(bid, ask, tick, dist_min, custo_ida_volta_pontos) or bid <= 0 or ask <= 0:
        return []
    if tick <= 0:
        return []
    s = est.sinal
    compra = est.compra
    saida = bid if compra else ask
    ref = preco_disparo(est, bid=bid, ask=ask, last=last, por_cotacao=por_cotacao)
    decorrido_s = (t_ns - est.aberta_ns) / NS
    custo = max(custo_ida_volta_pontos, 0.0)
    antes = set(est.regras_disparadas)
    acoes: list[AcaoGestao] = []

    # 1) regras terminais e de uma vez (sem_progresso, ao_vencer_tempo, parcial)
    for i, r in enumerate(plano.regras):
        if i in est.regras_disparadas:
            continue
        if r.tipo == "sem_progresso" and r.apos_s is not None and decorrido_s >= r.apos_s:
            est.regras_disparadas.add(i)
            if est.mfe_pontos < float(r.mfe_pontos if r.mfe_pontos is not None else tick) - _EPS:
                return [Encerrar("sem_progresso")]
        elif r.tipo == "ao_vencer_tempo" and t_ns >= est.prazo_ns:
            est.regras_disparadas.add(i)
            if (r.acao_vencimento or "encerrar") == "encerrar":
                return [Encerrar("ao_vencer_tempo")]
            trava_liquido = est.sl is not None and s * (est.sl - est.entrada) > custo + _EPS
            if trava_liquido and r.extensao_s:
                total_s = (est.prazo_ns - est.aberta_ns) // NS
                teto_s = (teto_prazo_ns - est.aberta_ns) // NS
                novo = est.aberta_ns + int(min(total_s + int(r.extensao_s), teto_s)) * NS
                if novo > est.prazo_ns:
                    acoes.append(EstenderPrazo(novo, r.tipo))
        elif r.tipo == "parcial" and r.quantidade and est.qtd >= 2 \
                and est.mfe_pontos >= float(r.mfe_pontos or 0.0) - _EPS:
            q = min(int(r.quantidade), est.qtd - 1)
            if q >= 1:
                est.regras_disparadas.add(i)
                acoes.append(Reduzir(q, r.tipo))

    # 2) stop: a regra mais apertada vence; limitado por dist_min do preco executavel e quantizado no passo
    ativadas: set[int] = set()
    cands = _candidatos_stop(plano, est, ref=ref, decorrido_s=decorrido_s, custo=custo, ativadas=ativadas)
    est.regras_disparadas |= ativadas
    if cands:
        nivel, nome = (max if compra else min)(cands, key=lambda c: c[0])
        limite = saida - s * dist_min
        nivel = min(nivel, limite) if compra else max(nivel, limite)
        passo = max(plano.passo_min_pontos, tick)
        nivel = _quantizar(nivel, passo, compra)
        aperta = est.sl is None or s * (nivel - est.sl) >= passo - _EPS
        no_intervalo = est.ultimo_ajuste_ns is None or t_ns - est.ultimo_ajuste_ns >= plano.intervalo_ajuste_s * NS
        longe = s * (saida - nivel) >= dist_min - _EPS
        if aperta and no_intervalo and longe:
            est.ultimo_ajuste_ns = t_ns
            acoes.append(ApertarStop(nivel, nome))

    # 3) eventos de reavaliacao (uma vez por posicao, so os pedidos)
    for nome in plano.reavaliar_se:
        if nome in est.eventos_emitidos or nome not in EVENTOS_REAVALIACAO:
            continue
        if _evento(nome, est, ref=ref, t_ns=t_ns, tick=tick, perto_frac=perto_frac, novas=est.regras_disparadas - antes):
            est.eventos_emitidos.add(nome)
            acoes.append(EventoReavaliacao(nome))
    return acoes


def _evento(nome: str, est: EstadoGestao, *, ref: float, t_ns: int, tick: float, perto_frac: float,
            novas: set[int]) -> bool:
    s = est.sinal
    if nome == "perto_do_stop":
        return est.sl is not None and s * (ref - est.sl) <= perto_frac * max(est.stop_inicial_pontos, tick) + _EPS
    if nome == "perto_do_alvo":
        if est.tp is None:
            return False
        return s * (est.tp - ref) <= perto_frac * max(s * (est.tp - est.entrada), tick) + _EPS
    if nome == "metade_do_tempo":
        return est.prazo_ns > est.aberta_ns and t_ns >= est.aberta_ns + (est.prazo_ns - est.aberta_ns) // 2
    if nome == "regra_disparou":
        return bool(novas) or bool(est.regras_disparadas)
    if nome == "lucro_acima_stop":
        return est.stop_inicial_pontos > 0 and s * (ref - est.entrada) >= est.stop_inicial_pontos - _EPS
    return False


# --------------------------------------------------------------------------------------------------
# aplicacao (so depois que o roteador aplicou) e sincronizacao
# --------------------------------------------------------------------------------------------------
def aplicar_acao(est: EstadoGestao, acao: AcaoGestao, *, origem: str = "auto") -> None:
    """Reflete no estado uma acao APLICADA. Nunca afrouxa o stop, nunca aumenta a quantidade."""
    s = est.sinal
    if isinstance(acao, ApertarStop):
        if est.sl is None or s * (acao.nivel - est.sl) > 0:
            est.sl = acao.nivel
            est.origem_ultimo_stop = origem
            _contar(est, origem)
    elif isinstance(acao, Reduzir):
        if 1 <= acao.q < est.qtd:
            est.qtd -= acao.q
            est.parciais += 1
    elif isinstance(acao, EstenderPrazo):
        if acao.prazo_ns > est.prazo_ns:
            est.prazo_ns = acao.prazo_ns
            _contar(est, origem)


def _contar(est: EstadoGestao, origem: str) -> None:
    if origem == "ia":
        est.ajustes_ia += 1
    else:
        est.ajustes_auto += 1


def sincronizar_posicao(est: EstadoGestao, pos: Posicao, *, origem: str | None = None) -> None:
    """Le stop, alvo e quantidade da posicao REAL (simulador/espelho). ``origem`` marca quem moveu o stop."""
    if pos.quantidade > 0 and pos.quantidade < est.qtd:
        est.qtd = pos.quantidade
    if pos.stop is not None and pos.stop != est.sl:
        est.sl = pos.stop
        if origem is not None:
            est.origem_ultimo_stop = origem
    if pos.alvo != est.tp:
        est.tp = pos.alvo


def registrar_liquido_parcial(est: EstadoGestao, liquido: float) -> None:
    est.liquido_parcial = round(est.liquido_parcial + float(liquido), 10)


# --------------------------------------------------------------------------------------------------
# caminho simulado (contrafactuais, WP9)
# --------------------------------------------------------------------------------------------------
class _TickLike(Protocol):
    t_utc_ns: int
    bid: float
    ask: float
    last: float


def simular_caminho(plano: PlanoGestaoCompilado, est0: EstadoGestao, ticks: Iterable[_TickLike], *,
                    tick_pontos: float | None = None, dist_min: float | None = None,
                    custo_ida_volta_pontos: float = 0.0, teto_prazo_ns: int | None = None,
                    por_cotacao: bool = False, perto_frac: float = 0.3,
                    incluir_eventos: bool = False) -> list[tuple[int, AcaoGestao]]:
    """Aplica a gestao a um caminho de ticks, supondo que toda acao e aplicada (sem veto, sem latencia).

    ``est0`` nao e alterado. Por tick: excursao; stop/alvo da POSICAO (antes da gestao, como no simulador);
    gestao; prazo. O caminho termina no primeiro desfecho, marcado com ``Encerrar``: ``stop_inicial`` ou
    ``stop_movido_<origem>``, ``alvo``, ``tempo`` ou o motivo da regra que encerrou. Os eventos de reavaliacao so
    entram com ``incluir_eventos``. Padroes: ``tick_pontos`` = passo do plano, ``dist_min`` = 1 tick, teto = prazo."""
    est = copy.deepcopy(est0)
    tick = float(tick_pontos if tick_pontos is not None else plano.passo_min_pontos)
    dmin = float(dist_min if dist_min is not None else tick)
    teto = int(teto_prazo_ns if teto_prazo_ns is not None else est.prazo_ns)
    s = est.sinal
    out: list[tuple[int, AcaoGestao]] = []
    for tk in ticks:
        t = int(tk.t_utc_ns)
        if t < est.aberta_ns or est.qtd <= 0:
            continue
        bid, ask, last = float(tk.bid), float(tk.ask), float(tk.last)
        atualizar_excursao(est, bid=bid, ask=ask, last=last, por_cotacao=por_cotacao)
        ref = preco_disparo(est, bid=bid, ask=ask, last=last, por_cotacao=por_cotacao)
        if est.sl is not None and s * (ref - est.sl) <= 0:
            motivo = "stop_inicial" if est.origem_ultimo_stop == "inicial" else f"stop_movido_{est.origem_ultimo_stop}"
            out.append((t, Encerrar(motivo)))
            return out
        alvo_ref = bid if est.compra else ask
        if est.tp is not None and s * (alvo_ref - est.tp) >= 0:
            out.append((t, Encerrar("alvo")))
            return out
        for a in avaliar_gestao(plano, est, bid=bid, ask=ask, last=last, t_ns=t, tick=tick, dist_min=dmin,
                                custo_ida_volta_pontos=custo_ida_volta_pontos, teto_prazo_ns=teto,
                                por_cotacao=por_cotacao, perto_frac=perto_frac):
            if isinstance(a, EventoReavaliacao) and not incluir_eventos:
                continue
            out.append((t, a))
            if isinstance(a, Encerrar):
                return out
            aplicar_acao(est, a)
        if t >= est.prazo_ns:
            out.append((t, Encerrar("tempo")))
            return out
    return out
