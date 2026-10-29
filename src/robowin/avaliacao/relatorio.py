"""Relatorio de avaliacao em portugues (dict + Markdown). Recusa o selo 'validado' em dados sinteticos ou
contaminados e avisa que acerto alto nao implica expectativa positiva."""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from robowin.core.instrumentos import PerfilInstrumento
from robowin.core.tipos import ConfigRisco, PreRegistro, Trade

from . import comparacao as comp
from . import estatistica as est
from . import impostos, metricas
from .metricas import dia_pregao

SLIPPAGE_BASE_TICKS = 1.0      # cenario-base: 1 tick de slippage por operacao
SLIPPAGE_ESTRESSE_TICKS = 2.0  # cenario de estresse: 2 ticks
CORRETAGEM_ESTRESSE_BRL = 0.22  # corretagem ida e volta do estresse (igual a execucao.custos)


def custos_do_perfil(perfil: PerfilInstrumento) -> tuple[float, float]:
    """(base, estresse): custo ida e volta por contrato = tarifas B3 do perfil + corretagem + slippage em ticks."""
    tarifas = 2 * perfil.custo_lado_unidade()  # forex/CFD: sem tarifa de bolsa, comissao por lote
    return (tarifas + SLIPPAGE_BASE_TICKS * perfil.tick_valor_brl,
            tarifas + CORRETAGEM_ESTRESSE_BRL + SLIPPAGE_ESTRESSE_TICKS * perfil.tick_valor_brl)


def stop_efetivo_padrao_pontos(perfil: PerfilInstrumento, cfg: ConfigRisco | None = None) -> float:
    """Stop efetivo pela politica de risco: min(stop_max, perda_max_op/pv - folga), na unidade do ativo."""
    from robowin.risco.politica import limites_conservadores, stop_maximo_efetivo_pontos
    cfg = cfg or limites_conservadores(perfil, ConfigRisco())
    return stop_maximo_efetivo_pontos(cfg, perfil.ponto_valor_brl)


def texto_aviso_acerto(perfil: PerfilInstrumento | None = None, stop_pontos: float | None = None,
                       alvo_pontos: float | None = None, risco: ConfigRisco | None = None,
                       custos_brl: tuple[float, float] | None = None) -> str:
    """Aviso sobre acerto, com o acerto de equilibrio calculado dos parametros EFETIVOS do ativo (nada fixo no texto).

    Sem `perfil` o aviso e generico (sem numeros): o equilibrio depende de stop, alvo, custos e valor do ponto."""
    geral = ("Taxa de acerto alta NAO implica expectativa positiva: uma geometria assimetrica faz entradas aleatorias "
             "acertarem muito antes dos custos. A metrica primaria e a expectativa liquida com IC; o acerto deve ser "
             "lido sempre ao lado do controle aleatorio.")
    if perfil is None:
        return geral
    teto = stop_pontos is None
    stop = stop_efetivo_padrao_pontos(perfil, risco) if stop_pontos is None else stop_pontos
    alvo = perfil.alvo_padrao_pontos if alvo_pontos is None else alvo_pontos
    c = custos_brl or custos_do_perfil(perfil)
    pv = perfil.ponto_valor_brl
    lo = est.acerto_equilibrio(stop, alvo, c[0], pv)
    hi = est.acerto_equilibrio(stop, alvo, c[1], pv)
    rot_stop = f"{stop:g} pontos ({ROTULO_TETO})" if teto else f"{stop:g} pontos"
    rot_alvo = f"{alvo:g} pontos (padrao do perfil)" if alvo_pontos is None else f"{alvo:g} pontos"
    return (
        f"Taxa de acerto alta NAO implica expectativa positiva. Ativo {perfil.simbolo_base}: com stop de {rot_stop} e "
        f"alvo de {rot_alvo}, o acerto de equilibrio e de {100 * lo:.0f}% a {100 * hi:.0f}% "
        f"(custos de R$ {c[0]:.2f} a R$ {c[1]:.2f} por operacao) e uma geometria assimetrica "
        "faz entradas aleatorias acertarem muito antes dos custos. A metrica primaria e a expectativa liquida "
        "com IC; o acerto deve ser lido sempre ao lado do controle aleatorio."
    )


ROTULO_TETO = "teto, nao geometria usada"


def geometria_mediana(pares: Iterable[tuple[float | None, float | None]]) -> tuple[float, float] | None:
    """(stop, alvo) REALIZADOS: medianas dos pares das intencoes EXECUTADAS (pares sem stop ou alvo positivos e
    finitos ficam fora). Sem nenhum par valido => None (o aviso cai no teto, rotulado)."""
    ok = [(float(s), float(a)) for s, a in pares
          if s is not None and a is not None and math.isfinite(float(s)) and math.isfinite(float(a))
          and float(s) > 0 and float(a) > 0]
    if not ok:
        return None
    return float(np.median([s for s, _ in ok])), float(np.median([a for _, a in ok]))


def texto_aviso_acerto_por_braco(perfil: PerfilInstrumento | None, geometrias: Mapping[str, tuple[float, float]],
                                 custos: tuple[float, float] | None = None, *, bracos: Iterable[str] = (),
                                 risco: ConfigRisco | None = None) -> dict[str, str]:
    """Aviso de acerto POR BRACO com a geometria REALIZADA (medianas das entradas executadas; C1). Braco listado em
    ``bracos`` sem geometria recebe o aviso do teto, rotulado "teto, nao geometria usada" (nunca uma combinacao de
    stop e alvo que nenhum braco usou apresentada como se fosse a dele)."""
    out: dict[str, str] = {}
    for nome in sorted(set(bracos) | set(geometrias)):
        g = geometrias.get(nome)
        if g is None or perfil is None:
            out[nome] = texto_aviso_acerto(perfil, risco=risco, custos_brl=custos)
            continue
        stop, alvo = float(g[0]), float(g[1])
        c = custos or custos_do_perfil(perfil)
        pv = perfil.ponto_valor_brl
        lo = est.acerto_equilibrio(stop, alvo, c[0], pv)
        hi = est.acerto_equilibrio(stop, alvo, c[1], pv)
        out[nome] = (f"Geometria realizada (mediana das entradas executadas): stop {stop:g} / alvo {alvo:g} pontos; "
                     f"acerto de equilibrio de {100 * lo:.0f}% a {100 * hi:.0f}% (custos de R$ {c[0]:.2f} a "
                     f"R$ {c[1]:.2f} por operacao). Acerto alto NAO implica expectativa positiva.")
    return out


AVISO_SIMULACAO = "SIMULACAO - sem ordens reais. Resultados simulados nao garantem resultado real."
FL_MINIMO = 1.2
DSR_MINIMO = 0.95
MIN_PREGOES = 20  # menos pregoes que isso: o bootstrap por pregao e o DSR diario nao sustentam o selo


def motivos_preregistro(preregistro: PreRegistro | None, *, datas_pregoes: Sequence[str] = (),
                        provedor_falso: bool = False, hashes_variantes: Mapping[str, str] | None = None,
                        dir_preregistro: Path | None = None) -> tuple[list[str], int | None]:
    """(motivos de RECUSA, tentativas da cadeia). Recusa: provedor FALSO; cadeia de ``dir_preregistro`` quebrada
    (``preregistro.verificar``); registro fora da cadeia; e, por ``preregistro.elegivel``, pregao com data <= registro,
    variante com hash divergente ou sem hash. ``tentativas`` = variantes registradas na cadeia (None sem cadeia)."""
    from . import preregistro as pre
    motivos: list[str] = []
    if provedor_falso:
        motivos.append("provedor FALSO (respostas e latencias injetadas) nao valida estrategia")
    tentativas: int | None = None
    if dir_preregistro is not None:
        try:
            cadeia = pre.verificar(Path(dir_preregistro))
        except (pre.ErroPreRegistro, OSError, ValueError) as e:
            motivos.append(f"cadeia de pre-registro invalida: {e}")
            cadeia = []
        else:
            tentativas = pre.n_tentativas(Path(dir_preregistro))
            if preregistro is not None and preregistro.hash_registro not in {p.hash_registro for p in cadeia}:
                motivos.append("pre-registro fora da cadeia registrada")
    if preregistro is None:
        return motivos, tentativas
    if not datas_pregoes:
        motivos.append("nenhum pregao informado para conferir o pre-registro")
    por_motivo: dict[str, list[str]] = {}
    for d in sorted(set(datas_pregoes)):
        _, ms = pre.elegivel(preregistro, data_pregao=d, hashes_variantes=dict(hashes_variantes or {}),
                             provedor_falso=False)
        for m in ms:
            por_motivo.setdefault(m, []).append(d)
    for m, ds in sorted(por_motivo.items()):
        exemplo = ", ".join(ds[:3]) + (f" e mais {len(ds) - 3}" if len(ds) > 3 else "")
        motivos.append(f"pre-registro {preregistro.id_experimento}: {m} ({len(ds)} pregao(oes): {exemplo})")
    return motivos, tentativas


def decidir_selo(*, sintetico: bool | None, contaminado: bool | None, n: int, n_alvo: int | None, exp_ic_inf: float,
                 fator_lucro: float, dsr: float, pregoes: int = 0, tentativas: int = 1,
                 var_sr_tentativas: float | None = None, preregistro: PreRegistro | None = None,
                 datas_pregoes: Sequence[str] = (), provedor_falso: bool = False,
                 hashes_variantes: Mapping[str, str] | None = None, dir_preregistro: Path | None = None) -> dict:
    """Selo 'validado' so se TODOS os criterios passam. Sintetico/contaminado DESCONHECIDO (None) recusa.

    Com ``preregistro`` (ou ``dir_preregistro``), RECUSA tambem pregao com data <= ``t_registro``, hash de variante
    divergente e provedor FALSO (``motivos_preregistro``); as tentativas do DSR passam a ser, no minimo, o total de
    variantes registradas na cadeia."""
    motivos = []
    rec, n_cadeia = motivos_preregistro(preregistro, datas_pregoes=datas_pregoes, provedor_falso=provedor_falso,
                                        hashes_variantes=hashes_variantes, dir_preregistro=dir_preregistro)
    if n_cadeia is not None:
        tentativas = max(tentativas, n_cadeia)
    motivos += rec
    if sintetico is None:
        motivos.append("origem dos dados desconhecida (nao afirmada como nao sintetica)")
    elif sintetico:
        motivos.append("dados sinteticos nao podem validar uma estrategia")
    if contaminado is None:
        motivos.append("contaminacao desconhecida (periodo nao afirmado como posterior ao corte dos LLMs)")
    elif contaminado:
        motivos.append("dados contaminados (periodo anterior ao corte de conhecimento dos LLMs)")
    if motivos:
        return {"selo": "recusado", "motivos": motivos}
    if n_alvo is None:
        motivos.append("n pre-registrado nao informado")
    elif n < n_alvo:
        motivos.append(f"amostra insuficiente (n={n} < n pre-registrado={n_alvo})")
    if pregoes < MIN_PREGOES:
        motivos.append(f"poucos pregoes ({pregoes} < {MIN_PREGOES})")
    if tentativas < 1:
        motivos.append("numero de tentativas invalido")
    elif tentativas > 1 and var_sr_tentativas is None:
        motivos.append("variancia do SR entre as tentativas nao informada (DSR sem deflator confiavel)")
    if not (exp_ic_inf > 0):
        motivos.append("IC inferior da expectativa nao e positivo")
    if not (fator_lucro > FL_MINIMO):
        motivos.append(f"fator de lucro nao excede {FL_MINIMO}")
    if not (dsr > DSR_MINIMO):
        motivos.append(f"DSR nao excede {DSR_MINIMO}")
    return {"selo": "nao_validado" if motivos else "validado", "motivos": motivos}


def _relatorio_braco(nome: str, trades: Sequence[Trade], *, n_alvo: int | None, tentativas: int,
                     sintetico: bool | None, contaminado: bool | None, cfg_ir: impostos.ConfigImpostos | None,
                     n_boot: int, seed: int, var_sr_tentativas: float | None = None,
                     calcula_tributo: bool = True, selo_kw: Mapping[str, Any] | None = None) -> dict:
    r = metricas.resumo(trades)
    x = [t.liquido_brl for t in metricas.ordenar(trades)]
    dias = [dia_pregao(t) for t in metricas.ordenar(trades)]
    wil = est.ic_wilson(r.acertos, r.n)
    cp = est.ic_clopper_pearson(r.acertos, r.n)
    if r.n:
        bs = est.bootstrap_por_pregao(x, dias, n_boot=n_boot, seed=seed)
        bs.pop("replicas_expectativa")
        pnl_dia = list(metricas.pnl_por_dia(trades).values())
        dsr = est.deflated_sharpe(pnl_dia, tentativas, var_sr_tentativas)
    else:
        bs = {"expectativa_ic": (math.nan, math.nan), "fator_lucro_ic": (math.nan, math.nan), "pregoes": 0}
        dsr = {"sr": math.nan, "sr0": math.nan, "dsr": math.nan, "n": 0, "n_tentativas": tentativas}
    selo = decidir_selo(sintetico=sintetico, contaminado=contaminado, n=r.n, n_alvo=n_alvo,
                        exp_ic_inf=bs["expectativa_ic"][0], fator_lucro=r.fator_lucro,
                        dsr=dsr["dsr"] if dsr["dsr"] == dsr["dsr"] else 0.0, pregoes=int(bs.get("pregoes", 0)),
                        tentativas=tentativas, var_sr_tentativas=var_sr_tentativas, **dict(selo_kw or {}))
    return {
        "braco": nome, "metricas": r.como_dict(),
        "acerto": {"n": r.n, "acertos": r.acertos, "taxa": r.taxa_acerto, "ic_wilson_95": wil,
                   "ic_clopper_pearson_95": cp},
        "bootstrap_pregao": bs, "dsr": dsr, "distribuicao_diaria": metricas.distribuicao_diaria(trades),
        "impostos": [a.como_dict() for a in impostos.apurar(trades, cfg_ir)] if calcula_tributo else [],
        "resultado_apos_ir": (impostos.resultado_apos_ir(trades, cfg_ir) if calcula_tributo
                              else {"antes_ir_brl": r.lucro_liquido_brl, "ir_estimado_brl": None, "depois_ir_brl": None}),
        "tributo": "brasil_day_trade" if calcula_tributo else "tributo nao calculado",
        "amostra": {"n": r.n, "n_alvo": n_alvo,
                    "suficiente": None if n_alvo is None else not comp.amostra_insuficiente(r.n, n_alvo)},
        "selo": selo,
    }


# ---------------------------------------------------------------- secoes novas (docs/16, WP8)
# falhas que o provedor FALSO INJETA (decisao/validade.py): diagnostico do encanamento, nunca comparacao
MOTIVOS_INJETADOS = ("resposta_tardia", "erro_http", "divergencia_revisor")
SUFIXO_FALSO = "-FALSO"


def eh_motivo_injetado(motivo: str) -> bool:
    return any(p.strip() in MOTIVOS_INJETADOS for p in str(motivo).split(":"))


def secao_injetados(motivos_por_braco: Mapping[str, Mapping[str, int]],
                    modelos_servidos: Mapping[str, Iterable[str]] | None = None, *,
                    provedor_falso: bool = False) -> dict[str, Any]:
    """Secao "injetado (FALSO)": contagens de ``resposta_tardia``, ``erro_http`` e ``divergencia_revisor`` dos bracos
    servidos pelo FALSO (modelo servido terminando em ``-FALSO``, ou toda a execucao com ``provedor_falso``). Sao
    falhas INJETADAS por seed: ficam so aqui, nunca entram em comparacao entre bracos."""
    falsos = {b for b, ms in (modelos_servidos or {}).items() if any(str(m).endswith(SUFIXO_FALSO) for m in ms)}
    if provedor_falso:
        falsos |= set(motivos_por_braco) | set(modelos_servidos or {})
    bracos: dict[str, dict[str, int]] = {}
    for b in sorted(falsos):
        sel = {m: int(n) for m, n in sorted((motivos_por_braco.get(b) or {}).items()) if eh_motivo_injetado(m)}
        if sel:
            bracos[b] = sel
    return {"falsos": sorted(falsos), "bracos": bracos, "total": sum(sum(c.values()) for c in bracos.values()),
            "aviso": "falhas injetadas pelo provedor FALSO (seed): so testam o encanamento; nunca usadas em comparacao"}


def secao_tentativas_episodios(tentativas: Mapping[str, int], episodios: Sequence[Mapping[str, Any]],
                               trades_por_braco: Mapping[str, Sequence[Trade]]) -> dict[str, dict[str, Any]]:
    """Tentativas de entrada (decisoes de COMPRAR/VENDER, inclusive vetadas e bloqueadas) contra EPISODIOS (posicoes
    abertas). Sem linhas em ``posicoes_episodio`` para o braco, episodio = operacao (``fonte = "trades"``)."""
    eps: Counter[str] = Counter(str(e.get("braco")) for e in episodios)
    out: dict[str, dict[str, Any]] = {}
    for b in sorted(set(tentativas) | set(eps) | set(trades_por_braco)):
        n_ep = eps[b] if eps[b] else len(trades_por_braco.get(b, ()))
        n_t = int(tentativas.get(b, 0))
        out[b] = {"tentativas": n_t, "episodios": n_ep, "fonte": "posicoes_episodio" if eps[b] else "trades",
                  "tentativas_por_episodio": n_t / n_ep if n_ep else math.nan}
    return out


def _mediana(xs: Iterable[Any]) -> float:
    v = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    return float(np.median(v)) if v else math.nan


def secao_gestao(episodios: Sequence[Mapping[str, Any]], eventos: Sequence[Mapping[str, Any]],
                 trades_por_braco: Mapping[str, Sequence[Trade]]) -> dict[str, dict[str, Any]]:
    """Metricas de gestao POR POSICAO (episodio): liquido e acerto por episodio, parciais, ajustes da IA e
    automaticos, MFE/MAE medianos, motivos de saida e eventos de gestao aplicados/nao aplicados por origem."""
    out: dict[str, dict[str, Any]] = {}
    for b in sorted({str(e.get("braco")) for e in episodios}):
        eps = [e for e in episodios if str(e.get("braco")) == b]
        grupos = [g for g in metricas.agrupar_por_episodio(list(trades_por_braco.get(b, ())), eps)
                  if g["origem"] == "episodio"]
        liq = [g["liquido_brl"] for g in grupos]
        evs = [e for e in eventos if str(e.get("braco")) == b]
        aplic: Counter[str] = Counter()
        nao_aplic: Counter[str] = Counter()
        for e in evs:
            (aplic if int(e.get("aplicado") or 0) else nao_aplic)[str(e.get("origem") or "?")] += 1
        n = len(eps)
        out[b] = {
            "episodios": n, "liquido_total_brl": float(sum(liq)),
            "liquido_medio_episodio_brl": float(np.mean(liq)) if liq else math.nan,
            "acerto_episodio": (sum(x > 0 for x in liq) / len(liq)) if liq else math.nan,
            "parciais_por_posicao": sum(int(e.get("n_parciais") or 0) for e in eps) / n if n else math.nan,
            "ajustes_ia_por_posicao": sum(int(e.get("n_ajustes_ia") or 0) for e in eps) / n if n else math.nan,
            "ajustes_auto_por_posicao": sum(int(e.get("n_ajustes_auto") or 0) for e in eps) / n if n else math.nan,
            "mfe_mediano_pts": _mediana(e.get("mfe_pts") for e in eps),
            "mae_mediano_pts": _mediana(e.get("mae_pts") for e in eps),
            "motivos_final": dict(Counter(str(e.get("motivo_final") or "aberta") for e in eps)),
            "eventos_aplicados": dict(aplic), "eventos_nao_aplicados": dict(nao_aplic),
            "motivos_nao_aplicados": dict(Counter(str(e.get("motivo") or "-") for e in evs
                                                  if not int(e.get("aplicado") or 0))),
        }
    return out


def secao_sombras(episodios: Sequence[Mapping[str, Any]], *, n_boot: int = 2000, seed: int = 0) -> dict[str, Any]:
    """Diferencas pareadas POR ENTRADA entre o braco de referencia e cada sombra (``sombra_de``): H3 e a exploratoria
    da sombra com trailing fixo. So pares completos entram no teste; a taxa de pares incompletos e relatada."""
    pares = sorted({(str(e.get("braco")), str(e.get("sombra_de"))) for e in episodios if e.get("sombra_de")})
    out: dict[str, Any] = {}
    for sombra, ref in pares:
        r = [e for e in episodios if str(e.get("braco")) == ref and not e.get("sombra_de")]
        s = [e for e in episodios if str(e.get("braco")) == sombra]
        res = comp.diferencas_por_entrada(r, s, "intencao_id", n_boot=n_boot, seed=seed)
        res.pop("pares", None)
        out[sombra] = {"referencia": ref, **res}
    return out


def gerar_relatorio(trades_por_braco: Mapping[str, Sequence[Trade]], *, tentativas: int,
                    controle: str | None = None, sintetico: bool | None = None, contaminado: bool | None = None,
                    n_alvo: int | None = None, var_sr_tentativas: float | None = None,
                    stop_pontos: float | None = None, alvo_pontos: float | None = None,
                    perfil: PerfilInstrumento | None = None, risco: ConfigRisco | None = None,
                    cfg_impostos: impostos.ConfigImpostos | None = None,
                    n_boot: int = 2000, seed: int = 0, titulo: str = "Relatorio de avaliacao",
                    ids_vetados: Mapping[str, set[str]] | None = None,
                    n_preregistrado_comparacoes: int | None = None,
                    geometrias: Mapping[str, tuple[float, float]] | None = None,
                    preregistro: PreRegistro | None = None, dir_preregistro: Path | None = None,
                    datas_pregoes: Sequence[str] | None = None, provedor_falso: bool = False,
                    hashes_variantes: Mapping[str, str] | None = None,
                    injetados: Mapping[str, Mapping[str, int]] | None = None,
                    modelos_servidos: Mapping[str, Iterable[str]] | None = None,
                    tentativas_entrada: Mapping[str, int] | None = None,
                    episodios: Sequence[Mapping[str, Any]] | None = None,
                    eventos_gestao: Sequence[Mapping[str, Any]] | None = None,
                    dias_perdas_seguidas: Mapping[str, Sequence[str]] | None = None) -> dict[str, Any]:
    """Relatorio por braco (dict). Os argumentos depois de ``n_preregistrado_comparacoes`` sao opcionais e so
    ACRESCENTAM secoes (docs/16, WP8): aviso de acerto por braco com a geometria realizada (``geometrias``), selo
    conferido contra o pre-registro, "injetado (FALSO)", tentativas contra episodios, gestao por posicao, diferencas
    pareadas com as sombras e dias encerrados por perdas seguidas. Sem eles o relatorio e o de antes."""
    bolsa = perfil.bolsa_perfil() if perfil is not None else None
    calcula_tributo = bolsa.calcula_tributo if bolsa is not None else True  # sem perfil: regime brasileiro (legado)
    todos = [t for ts in trades_por_braco.values() for t in ts]
    dias = sorted({dia_pregao(t) for t in todos})
    if dir_preregistro is not None:  # tentativas do DSR = variantes registradas na cadeia (nunca menos)
        _, n_cadeia = motivos_preregistro(None, dir_preregistro=dir_preregistro)
        tentativas = max(tentativas, n_cadeia or 0)
    selo_kw: dict[str, Any] = {}
    if preregistro is not None or dir_preregistro is not None or provedor_falso:
        selo_kw = {"preregistro": preregistro, "dir_preregistro": dir_preregistro, "provedor_falso": provedor_falso,
                   "hashes_variantes": hashes_variantes,
                   "datas_pregoes": tuple(datas_pregoes) if datas_pregoes is not None else tuple(dias)}
    bracos = {nome: _relatorio_braco(nome, ts, n_alvo=n_alvo, tentativas=tentativas, sintetico=sintetico,
                                     contaminado=contaminado, cfg_ir=cfg_impostos, n_boot=n_boot, seed=seed,
                                     var_sr_tentativas=var_sr_tentativas, calcula_tributo=calcula_tributo,
                                     selo_kw=selo_kw)
              for nome, ts in trades_por_braco.items()}
    comparacoes: dict[str, Any] = {}
    if controle and controle in trades_por_braco:
        c = bracos[controle]["acerto"]
        for nome, b in bracos.items():
            if nome == controle:
                continue
            comparacoes[nome] = comp.comparar_com_controle(
                b["acerto"]["acertos"], b["acerto"]["n"], c["acertos"], c["n"],
                n_preregistrado=n_preregistrado_comparacoes if n_preregistrado_comparacoes is not None else n_alvo)
        ps = {n: v["p_valor_braco_maior"] for n, v in comparacoes.items() if "p_valor_braco_maior" in v}
        for n, h in comp.holm(ps).items():
            comparacoes[n]["holm"] = h
    vetos = {}
    for nome, ids in (ids_vetados or {}).items():
        if nome in trades_por_braco:
            vetos[nome] = comp.valor_do_veto(trades_por_braco[nome], ids, n_boot=n_boot, seed=seed)
    extras: dict[str, Any] = {}
    if geometrias is not None:
        extras["avisos_acerto_por_braco"] = texto_aviso_acerto_por_braco(perfil, geometrias, bracos=bracos,
                                                                         risco=risco)
    if injetados is not None or modelos_servidos is not None:
        extras["injetados"] = secao_injetados(injetados or {}, modelos_servidos, provedor_falso=provedor_falso)
    if tentativas_entrada is not None or episodios is not None:
        extras["tentativas_episodios"] = secao_tentativas_episodios(tentativas_entrada or {}, episodios or (),
                                                                    trades_por_braco)
    if episodios is not None:
        extras["gestao"] = secao_gestao(episodios, eventos_gestao or (), trades_por_braco)
        extras["sombras"] = secao_sombras(episodios, n_boot=n_boot, seed=seed)
    if dias_perdas_seguidas is not None:
        extras["dias_perdas_seguidas"] = {b: sorted(set(ds)) for b, ds in sorted(dias_perdas_seguidas.items())}
    return {
        "titulo": titulo, "aviso_simulacao": AVISO_SIMULACAO,
        "aviso_acerto": texto_aviso_acerto(perfil, stop_pontos, alvo_pontos, risco),
        **extras,
        "ativo": perfil.simbolo_base if perfil is not None else None,
        "moeda": perfil.moeda if perfil is not None else "BRL",
        "periodo": {"inicio": dias[0] if dias else None, "fim": dias[-1] if dias else None, "pregoes": len(dias)},
        "dados": {"sintetico": sintetico, "contaminado": contaminado}, "controle": controle,
        "tentativas": tentativas, "var_sr_tentativas": var_sr_tentativas, "bracos": bracos, "comparacoes_com_controle": comparacoes,
        "valor_do_veto": vetos,
    }


# ---------------------------------------------------------------- Markdown
def _f(v: Any, casas: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        if math.isnan(v):
            return "n/d"
        if math.isinf(v):
            return "infinito"
        return f"{v:.{casas}f}".replace(".", ",")
    return str(v)


def _pct(v: float) -> str:
    return "n/d" if v != v else f"{100 * v:.1f}%".replace(".", ",")


def _ic(par: Sequence[float], casas: int = 2) -> str:
    return f"[{_f(float(par[0]), casas)}; {_f(float(par[1]), casas)}]"


def _markdown_secoes_novas(rel: Mapping[str, Any]) -> list[str]:
    L: list[str] = []
    te = rel.get("tentativas_episodios")
    if te:
        L += ["## Tentativas de entrada contra episodios", "",
              "| braco | tentativas | episodios | tentativas/episodio | fonte |", "|---|---|---|---|---|"]
        for b, x in te.items():
            L.append(f"| {b} | {x['tentativas']} | {x['episodios']} | {_f(x['tentativas_por_episodio'])} | {x['fonte']} |")
        L.append("")
    g = rel.get("gestao")
    if g:
        L += ["## Gestao por posicao (episodio)", "",
              "| braco | episodios | liquido/episodio | acerto/episodio | parciais | ajustes IA | ajustes auto | "
              "MFE mediano (pts) | MAE mediano (pts) | eventos nao aplicados |", "|---|---|---|---|---|---|---|---|---|---|"]
        for b, x in g.items():
            L.append(f"| {b} | {x['episodios']} | R$ {_f(x['liquido_medio_episodio_brl'])} | {_pct(x['acerto_episodio'])} | "
                     f"{_f(x['parciais_por_posicao'])} | {_f(x['ajustes_ia_por_posicao'])} | "
                     f"{_f(x['ajustes_auto_por_posicao'])} | {_f(x['mfe_mediano_pts'], 1)} | "
                     f"{_f(x['mae_mediano_pts'], 1)} | {sum(x['eventos_nao_aplicados'].values())} |")
        L.append("")
    s = rel.get("sombras")
    if s:
        L += ["## Diferencas pareadas com as sombras (por entrada, so pares completos)", "",
              "| sombra | referencia | pares | media ref - sombra | IC 95% | p (ref > sombra) | pares incompletos |",
              "|---|---|---|---|---|---|---|"]
        for nome, x in s.items():
            ic = _ic(x["ic_bootstrap"]) if "ic_bootstrap" in x else "-"
            L.append(f"| {nome} | {x['referencia']} | {x.get('n_pares', 0)} | R$ {_f(x.get('media_dif'))} | {ic} | "
                     f"{_f(x.get('p_valor_unilateral'), 4)} | {_pct(x.get('taxa_incompletos', math.nan))} |")
        L.append("")
    dps = rel.get("dias_perdas_seguidas")
    if dps is not None:
        L += ["## Dias encerrados por perdas seguidas", ""]
        L += [f"- {b}: {len(ds)} pregao(oes) ({', '.join(ds)})" for b, ds in dps.items() if ds] or ["- nenhum"]
        L.append("")
    inj = rel.get("injetados")
    if inj and inj.get("falsos"):
        L += ["## Injetado (FALSO) - fora de qualquer comparacao", "", f"> {inj['aviso']}", ""]
        L += [f"- {b}: " + ", ".join(f"{m} x{n}" for m, n in c.items()) for b, c in inj["bracos"].items()] or [
            "- nenhuma falha injetada registrada"]
        L.append("")
    return L


def renderizar_markdown(rel: Mapping[str, Any]) -> str:
    L: list[str] = [f"# {rel['titulo']}", "", f"> {rel['aviso_simulacao']}", ""]
    if rel["dados"]["sintetico"]:
        L += ["> **DADOS SINTETICOS** - resultados nao validam a estrategia.", ""]
    if rel["dados"]["sintetico"] is None or rel["dados"]["contaminado"] is None:
        L += ["> **ORIGEM/CONTAMINACAO DOS DADOS NAO AFIRMADA** - o selo 'validado' fica recusado.", ""]
    if rel["dados"]["contaminado"]:
        L += ["> **DADOS CONTAMINADOS** - periodo anterior ao corte de conhecimento dos LLMs.", ""]
    p = rel["periodo"]
    if rel.get("ativo"):
        L += [f"Ativo: **{rel['ativo']}**", ""]
    L += [f"Periodo: {p['inicio'] or 'n/d'} a {p['fim'] or 'n/d'} ({p['pregoes']} pregoes). "
          f"Tentativas contadas no DSR: {rel['tentativas']}.", "", f"**Aviso:** {rel['aviso_acerto']}", ""]
    ctrl = rel.get("controle")
    for nome, b in rel["bracos"].items():
        m, a, bs, d = b["metricas"], b["acerto"], b["bootstrap_pregao"], b["dsr"]
        L += [f"## Braco {nome}" + (" (controle aleatorio)" if nome == ctrl else ""), "",
              f"- Selo: **{b['selo']['selo']}**" + (f" - {'; '.join(b['selo']['motivos'])}" if b["selo"]["motivos"] else ""),
              f"- n = {m['n']} operacoes em {m['dias']} pregoes",
              f"- Taxa de acerto (liquido > 0): {_pct(a['taxa'])}; IC de Wilson 95%: "
              f"[{_pct(a['ic_wilson_95'][0])}; {_pct(a['ic_wilson_95'][1])}]",
              f"- Lucro liquido: R$ {_f(m['lucro_liquido_brl'])}; "
              + (f"apos IR estimado: R$ {_f(b['resultado_apos_ir']['depois_ir_brl'])}" if b["tributo"] == "brasil_day_trade"
                 else "tributo nao calculado (regime tributario fora do Brasil)"),
              f"- Expectativa por operacao: R$ {_f(m['expectativa_brl'])}; IC 95% (bootstrap por pregao): "
              f"{_ic(bs['expectativa_ic'])}",
              f"- Fator de lucro: {_f(m['fator_lucro'])}; IC 95%: {_ic(bs['fator_lucro_ic'])}",
              f"- Ganho medio R$ {_f(m['ganho_medio_brl'])} / perda media R$ {_f(m['perda_media_brl'])}",
              f"- Drawdown maximo: R$ {_f(m['drawdown_max_brl'])} ({m['drawdown_duracao_operacoes']} operacoes, "
              f"{_f(m['drawdown_duracao_dias'], 1)} dias)",
              f"- Slippage medio: R$ {_f(m['slippage_medio_brl'])}; custos (tarifas+corretagem): R$ {_f(m['custos_brl'])}",
              f"- DSR: {_f(d['dsr'], 3)} (SR {_f(d['sr'], 3)}, SR0 {_f(d['sr0'], 3)}, tentativas {d['n_tentativas']})"]
        aviso_b = (rel.get("avisos_acerto_por_braco") or {}).get(nome)
        if aviso_b:
            L.append(f"- Aviso de acerto: {aviso_b}")
        am = b["amostra"]
        if am["suficiente"] is False:
            L.append(f"- **Amostra insuficiente**: n={am['n']} < n pre-registrado={am['n_alvo']}; sem conclusao.")
        if nome in rel["comparacoes_com_controle"]:
            c = rel["comparacoes_com_controle"][nome]
            if "p_controle" in c:
                L.append(f"- Controle aleatorio: {_pct(c['p_controle'])} (IC de Wilson "
                         f"[{_pct(c['ic_wilson_controle'][0])}; {_pct(c['ic_wilson_controle'][1])}]); "
                         f"p-valor (braco > controle): {_f(c['p_valor_braco_maior'], 4)}"
                         + (f"; ajustado por Holm: {_f(c['holm']['p_ajustado'], 4)}" if "holm" in c else ""))
        if nome in rel["valor_do_veto"]:
            v = rel["valor_do_veto"][nome]
            if "valor_veto_total_brl" in v:
                L.append(f"- Valor do veto do revisor: R$ {_f(v['valor_veto_total_brl'])} em "
                         f"{v['n_rejeitadas']} operacoes rejeitadas (expectativa das rejeitadas R$ "
                         f"{_f(v['expectativa_rejeitadas_brl'])})")
        if b["impostos"]:
            L += ["", "| Mes | Resultado (R$) | IRRF (R$) | Base (R$) | DARF 6015 (R$) | Vencimento |",
                  "|---|---|---|---|---|---|"]
            for i in b["impostos"]:
                L.append(f"| {i['mes']} | {_f(i['resultado_liquido_brl'])} | {_f(i['irrf_brl'])} | "
                         f"{_f(i['base_calculo_brl'])} | {_f(i['darf_brl'])} | {i['vencimento']} |")
        L.append("")
    L += _markdown_secoes_novas(rel)
    texto = "\n".join(L)
    moeda = rel.get("moeda", "BRL")
    return texto if moeda == "BRL" else texto.replace("R$", moeda)  # valores na moeda do instrumento


def para_json_seguro(obj: Any) -> Any:
    """Converte NaN/inf em None e numpy/tuplas em tipos JSON."""
    if isinstance(obj, dict):
        return {str(k): para_json_seguro(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [para_json_seguro(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
