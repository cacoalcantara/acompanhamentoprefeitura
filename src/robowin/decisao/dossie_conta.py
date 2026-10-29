"""Secoes da conta no dossie do braco planejador (docs/16 WP5): custos publicos, envelope, sessao, posicao.

Tudo em PONTOS, UNIDADES e SEGUNDOS, relativo a entrada ou ao preco atual (nunca preco absoluto). A IA nunca ve
o ``ConfigRisco``, arquivos, usuario ou conta: no maximo o ``EnvelopeDecisao`` DERIVADO (calculado so pelo motor de
risco) ou, com ``expor="nenhum"``, a versao reduzida. Nenhum valor em dinheiro de limite entra aqui.

* ``custos_publicos``: custos de ida e volta do perfil (base e estresse) convertidos para pontos e o slippage medio
  de stop CAUSAL (so pregoes anteriores, tabela de episodios). Com menos de ``min_amostras``, a reserva vem dos
  custos do perfil (``avaliacao.relatorio``). Nunca usa o modelo interno do simulador (``ParamsSlippage``);
* ``secoes_planejador``: secoes ``instrumento``, ``custos``, ``envelope``, ``sessao``, ``mercado``, ``posicao``,
  ``plano_vigente``, ``retorno`` e ``latencia_ia`` do dossie v2 (montado por ``ia.dossie.montar_dossie``);
* ``limites_legado``: a secao ``limites`` do dossie v1 com o stop maximo EFETIVO para q=1 (C2, v1.1).
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from robowin.avaliacao.relatorio import SLIPPAGE_ESTRESSE_TICKS, custos_do_perfil
from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import EnvelopeDecisao, PlanoGestaoCompilado, Posicao

from .compilador_plano import envelope_resumido
from .gestao import EstadoGestao

NS = 1_000_000_000
FORMULA_EQUILIBRIO = "acerto_equilibrio = (stop_pontos + custo_ida_volta_pontos) / (stop_pontos + alvo_pontos)"
CHAVES_SESSAO = ("min_desde_abertura", "de", "min_para_fim_entradas", "min_para_zeragem", "min_para_evento")
CHAVES_MERCADO = ("feed_ok", "leilao", "vol_extrema")
CHAVES_LATENCIA = ("p50_s", "p95_s", "n")
FONTE_CAUSAL = "media_causal_pregoes_anteriores"
FONTE_RESERVA = "reserva_custos_do_perfil"


@dataclass(frozen=True)
class CustosPublicos:
    ida_volta_pontos_base: float
    ida_volta_pontos_estresse: float
    meio_spread_pontos: float
    slippage_stop_medio_pontos: float
    fonte_slippage: str

    def como_dict(self) -> dict[str, Any]:
        return {**asdict(self), "formula_equilibrio": FORMULA_EQUILIBRIO}


def _r(x: float) -> float:
    return round(float(x), 6)


def custos_publicos(perfil: PerfilInstrumento, *, spread_pts: float, slippages_stop_anteriores: Sequence[float],
                    min_amostras: int = 30) -> CustosPublicos:
    """Custos por unidade em pontos. ``slippages_stop_anteriores``: slippages de stop (pontos, positivo = contra) de
    pregoes ANTERIORES (o chamador garante a causalidade). Valores nao finitos sao descartados."""
    base, estresse = custos_do_perfil(perfil)
    pv = perfil.ponto_valor_brl
    amostras = [float(x) for x in slippages_stop_anteriores if math.isfinite(float(x))]
    if len(amostras) >= max(int(min_amostras), 1):
        slip, fonte = sum(amostras) / len(amostras), FONTE_CAUSAL
    else:
        slip, fonte = perfil.pontos(SLIPPAGE_ESTRESSE_TICKS), FONTE_RESERVA
    meio = max(float(spread_pts), 0.0) / 2 if math.isfinite(float(spread_pts)) else 0.0
    return CustosPublicos(ida_volta_pontos_base=_r(base / pv), ida_volta_pontos_estresse=_r(estresse / pv),
                          meio_spread_pontos=_r(meio), slippage_stop_medio_pontos=_r(slip), fonte_slippage=fonte)


def _filtrar(m: Mapping[str, Any] | None, chaves: Sequence[str]) -> dict[str, Any]:
    return {k: m[k] for k in chaves if m is not None and k in m}


def _so_codigos(m: Mapping[str, Any] | None) -> dict[str, Any]:
    """``retorno``: so codigos (texto, booleanos e listas de texto), nunca numeros."""
    out: dict[str, Any] = {}
    for k, v in (m or {}).items():
        if isinstance(v, bool | str):
            out[k] = v
        elif isinstance(v, list | tuple) and all(isinstance(x, str) for x in v):
            out[k] = list(v)
    return out


def _latencia(m: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in CHAVES_LATENCIA:
        v = (m or {}).get(k)
        if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v)):
            out[k] = round(float(v), 3)
    return out


def secao_envelope(env: EnvelopeDecisao, expor: str) -> dict[str, Any]:
    if expor == "nenhum":
        return envelope_resumido(env)
    return env.model_dump()


def secao_posicao(pos: Posicao, est: EstadoGestao | None, *, ultimo_preco: float, t_ns: int | None,
                  gestao: PlanoGestaoCompilado | None = None) -> dict[str, Any] | None:
    """Posicao em pontos de RESULTADO desde o preco medio (-30 perde 30; 0 empate; +10 trava 10), niveis ATUAIS.

    ``ultimo_preco``: preco executavel de SAIDA (bid se comprado, ask se vendido)."""
    if pos.lado is None or pos.quantidade <= 0:
        return None
    s = 1 if pos.lado.value == "compra" else -1
    entrada = est.entrada if est is not None else pos.preco_medio
    sl = est.sl if est is not None else pos.stop
    tp = est.tp if est is not None else pos.alvo
    d: dict[str, Any] = {"lado": pos.lado.value, "quantidade": pos.quantidade,
                         "quantidade_inicial": est.qtd_inicial if est is not None else pos.quantidade}
    if math.isfinite(ultimo_preco) and ultimo_preco > 0:
        d["resultado_pontos"] = _r(s * (ultimo_preco - entrada))
        if sl is not None:
            d["distancia_stop_pontos"] = _r(s * (ultimo_preco - sl))
        if tp is not None:
            d["distancia_alvo_pontos"] = _r(s * (tp - ultimo_preco))
    d["stop_resultado_pontos"] = None if sl is None else _r(s * (sl - entrada))
    d["alvo_resultado_pontos"] = None if tp is None else _r(s * (tp - entrada))
    if est is not None:
        d["mfe_pontos"] = _r(est.mfe_pontos)
        d["mae_pontos"] = _r(est.mae_pontos)
        d["tempo_max_s"] = int((est.prazo_ns - est.aberta_ns) // NS)
        if t_ns is not None:
            aberta = max(0, int((t_ns - est.aberta_ns) // NS))
            d["tempo_aberta_s"] = aberta
            d["tempo_restante_s"] = max(0, int((est.prazo_ns - t_ns) // NS))
        d["ajustes_ia"] = est.ajustes_ia
        d["ajustes_auto"] = est.ajustes_auto
        d["parciais"] = est.parciais
        d["origem_ultimo_stop"] = est.origem_ultimo_stop
        if gestao is not None:
            d["regras"] = [{**r.model_dump(exclude_none=True), "disparada": i in est.regras_disparadas}
                           for i, r in enumerate(gestao.regras)]
    elif t_ns is not None and pos.aberta_ns:
        d["tempo_aberta_s"] = max(0, int((t_ns - pos.aberta_ns) // NS))
    return d


def secoes_planejador(*, pos: Posicao, est: EstadoGestao | None, env: EnvelopeDecisao, expor: str,
                      perfil: PerfilInstrumento, custos: CustosPublicos, sessao: Mapping[str, Any],
                      mercado: Mapping[str, Any], plano_vigente: Mapping[str, Any] | None,
                      retorno: Mapping[str, Any], latencia: Mapping[str, Any], ultimo_preco: float,
                      t_ns: int | None = None, gestao: PlanoGestaoCompilado | None = None) -> dict[str, Any]:
    """Secoes v2 do dossie do planejador. ``t_ns`` (instante do dossie) habilita tempo aberta/restante da posicao;
    ``gestao`` (plano de gestao vigente da posicao) lista as regras com o estado de cada uma."""
    return {
        "instrumento": {"tick_pontos": perfil.tick_pontos, "valor_ponto_conta": perfil.ponto_valor_brl,
                        "stop_dispara_por": "ultimo_negocio" if perfil.tem_negocios else "cotacao_de_saida"},
        "custos": custos.como_dict(),
        "envelope": secao_envelope(env, expor),
        "sessao": _filtrar(sessao, CHAVES_SESSAO),
        "mercado": _filtrar(mercado, CHAVES_MERCADO),
        "posicao": secao_posicao(pos, est, ultimo_preco=ultimo_preco, t_ns=t_ns, gestao=gestao),
        "plano_vigente": dict(plano_vigente) if plano_vigente is not None else None,
        "retorno": _so_codigos(retorno),
        "latencia_ia": _latencia(latencia),
    }


def limites_legado(env: EnvelopeDecisao, est_risco: Any, cfg_risco: Any) -> dict[str, float]:
    """Secao ``limites`` do dossie v1, com o stop maximo EFETIVO para q=1 do envelope (C2, v1.1).

    ``est_risco``: estado da contabilidade do braco (``pnl_dia_total``, ``operacoes_dia``); ``cfg_risco``: o
    ``ConfigRisco`` (usado so aqui, no dossie LEGADO, como antes; o planejador nunca recebe esta secao)."""
    stop_q1 = env.stop_max_pontos_por_q[0] if env.stop_max_pontos_por_q else 0.0
    return {"perda_restante_dia_brl": float(cfg_risco.perda_max_dia_brl + est_risco.pnl_dia_total),
            "operacoes_restantes": float(cfg_risco.operacoes_dia_max - est_risco.operacoes_dia),
            "contratos_max": float(cfg_risco.contratos_max), "stop_max_pontos": float(stop_q1)}
