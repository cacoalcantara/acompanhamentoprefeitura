"""``robowin maquina-do-tempo``: chances do plano (stop e alvo em pontos) por bootstrap das sessoes anteriores.

Atribuicao: ideia e metodo do Time Machine inspirados em day-trader-skill (Jake Schincariol, MIT);
reimplementacao independente (ver THIRD_PARTY_NOTICES.md). SIMULACAO: nao e previsao e nao envia ordem alguma.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from robowin.core.config import ErroConfig
from robowin.core.instrumentos import PerfilInstrumento, argumentos_ativo, perfil_do_cli
from robowin.core.tipos import Lado

from . import maquina_do_tempo as mt


def piscina_de_armazem(raiz: str | Path, perfil: PerfilInstrumento, *, sessoes: int = 60,
                       intervalo_s: int = mt.INTERVALO_PADRAO_S, antes_de: dt.date | None = None) -> mt.PiscinaSessoes:
    """Le as ultimas ``sessoes`` datas do armazem de ticks para o ativo do perfil e as converte em barras.

    ``antes_de`` (opcional): so datas ESTRITAMENTE anteriores (causal; usado pelo nucleo ao vivo com o pregao
    corrente). Nenhum tick de ``antes_de`` ou posterior e lido."""
    from robowin.dados.ticks import ArmazenamentoTicks
    raiz = Path(raiz)
    if not raiz.is_dir():
        raise mt.ErroMaquinaDoTempo(f"diretorio de dados nao encontrado: {raiz}")
    arm = ArmazenamentoTicks(raiz)
    sint_prefixo = f"SINT_{perfil.simbolo_base}"
    simbolos = [s for s in arm.simbolos() if perfil.codigo_pertence(s) or s == sint_prefixo
                or (s.startswith("SINT_") and perfil.codigo_pertence(s[5:]))]
    if not simbolos:
        raise mt.ErroSessoesInsuficientes(f"dados insuficientes: nenhum simbolo de {perfil.simbolo_base} em {raiz}")
    sinteticos = {s.startswith("SINT_") for s in simbolos}
    if len(sinteticos) > 1:
        raise mt.ErroMaquinaDoTempo(
            "dados contaminados: mistura de simbolos sinteticos e reais no mesmo ativo; separe os diretorios")
    sintetico = bool(arm.eh_sintetico or sinteticos == {True})
    datas = sorted({(d, s) for s in simbolos for d in arm.datas(s)})
    ultimas = sorted({d for d, _ in datas if antes_de is None or d < antes_de})[-sessoes:] if sessoes > 0 else []
    ticks = []
    for d in ultimas:
        for s in simbolos:
            ticks.extend(arm.ler_dia(s, d))
    return mt.barras_de_ticks(ticks, perfil, intervalo_s, sintetico=sintetico, origem=f"armazem:{raiz}")


def piscina_sintetica(perfil: PerfilInstrumento, *, sessoes: int, seed: int, n_ticks: int,
                      intervalo_s: int = mt.INTERVALO_PADRAO_S) -> mt.PiscinaSessoes:
    from robowin.dados.sintetico import ConfigSintetico, gerar_dias
    ticks = []
    for dia in gerar_dias(ConfigSintetico.do_perfil(perfil, seed=seed, n_ticks=n_ticks, n_saltos=0), sessoes):
        ticks.extend(dia.ticks)
    return mt.barras_de_ticks(ticks, perfil, intervalo_s, sintetico=True, origem="sintetico")


def formatar(rel: mt.Relatorio, perfil: PerfilInstrumento, plano: mt.PlanoMT) -> str:
    s, h = rel.simulacao, rel.historico
    pc = lambda x: "n/d" if x is None else f"{100 * x:.1f}%"  # noqa: E731
    rot = " / ".join(s.rotulos)
    linhas = [
        f"{rot}: {mt.AVISO}.",
        f"Ativo {perfil.simbolo_base} | {s.lado} | stop {s.stop_pontos:g} pts | alvo {s.alvo_pontos:g} pts "
        f"(R do alvo {s.r_alvo:.2f}) | entrada {plano.entrada} | zeragem {plano.zeragem} | moeda {s.moeda}",
        f"Piscina: {s.n_sessoes} sessoes completas anteriores a {plano.data.isoformat()} | {s.replays} replays | "
        f"seed {s.seed} | janela +-{s.janela} barras | slippage de stop {s.slippage_stop_ticks:g} ticks",
        "",
        f"  alvo primeiro : {pc(s.p_alvo)}",
        f"  stop primeiro : {pc(s.p_stop)}",
        f"  nenhum        : {pc(s.p_nenhum)}   (ainda aberto na zeragem)",
        f"  verde         : {pc(s.p_verde)}   (alvo primeiro, ou a favor na zeragem)",
        f"  R medio bruto {s.r_bruto_medio:+.3f} | R medio liquido {s.r_liquido_medio:+.3f} "
        f"(custo ida e volta {s.custo_ida_volta:.2f} {s.moeda} = {s.custo_r:.3f}R)",
        f"  valor esperado liquido {s.ev_liquido:+.2f} {s.moeda} por lote",
        f"  equilibrio: bruto {pc(s.equilibrio_bruto)} | liquido de custos {pc(s.equilibrio_liquido)} | "
        f"alvos entre resolvidos {pc(s.p_alvo_entre_resolvidos)}",
        "  pontos a favor na zeragem (percentis " + "/".join(s.percentis_pontos) + "): "
        + " / ".join(f"{v:+.0f}" for v in s.percentis_pontos.values()),
        "",
        f"Historico (regra reaplicada nas {h.n_sessoes} sessoes reais): {h.n_disparos} disparos | alvo {h.n_alvo} | "
        f"stop {h.n_stop} | nenhum {h.n_nenhum} | alvos entre resolvidos {pc(h.p_alvo_entre_resolvidos)} | "
        f"R medio liquido {'n/d' if h.r_liquido_medio is None else f'{h.r_liquido_medio:+.3f}'}",
        "",
        "Veredito: " + s.veredito.texto,
    ]
    return "\n".join(linhas)


def _cmd(a: Any) -> int:
    try:
        perfil = perfil_do_cli(a)
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    try:
        if a.sintetico:
            piscina = piscina_sintetica(perfil, sessoes=a.sessoes, seed=a.seed_dados, n_ticks=a.n_ticks)
        else:
            if not a.dados:
                print("ERRO: informe --dados DIR (armazem de ticks) ou --sintetico.")
                return 2
            piscina = piscina_de_armazem(a.dados, perfil, sessoes=a.sessoes)
        data = dt.date.fromisoformat(a.data) if a.data else mt.data_padrao_do_plano(piscina)
        plano = mt.plano_padrao(perfil, Lado(a.lado), a.stop_pontos, a.alvo_pontos, data, a.entrada, a.zeragem,
                                a.preco)
        rel = mt.planejar(piscina, perfil, plano, seed=a.seed, replays=a.replays,
                          slippage_stop_ticks=a.slippage_stop_ticks)
    except mt.ErroMaquinaDoTempo as e:
        print(f"RECUSADO: {e}")
        return 3
    except (ErroConfig, ValueError) as e:
        print(f"ERRO: {e}")
        return 2
    if a.json:
        print(json.dumps({"ativo": perfil.simbolo_base, "data_plano": data.isoformat(), "entrada": plano.entrada,
                          "zeragem": plano.zeragem, **rel.a_dict()}, ensure_ascii=False, indent=2))
    else:
        print(formatar(rel, perfil, plano))
    return 0


def registrar_comando(sub: Any) -> None:
    p = sub.add_parser("maquina-do-tempo", help="chances do plano (stop/alvo em pontos) por bootstrap das sessoes "
                       "anteriores; SIMULACAO, nao e previsao")
    argumentos_ativo(p)
    p.add_argument("--dados", default=None, help="diretorio do armazem de ticks (Parquet)")
    p.add_argument("--sintetico", action="store_true", help="usa dados SINTETICOS gerados (rotulados)")
    p.add_argument("--lado", required=True, choices=[x.value for x in Lado])
    p.add_argument("--stop-pontos", type=float, required=True)
    p.add_argument("--alvo-pontos", type=float, required=True)
    p.add_argument("--entrada", default=None, help="HH:MM, inicio de uma barra (padrao: 1 h apos a abertura)")
    p.add_argument("--zeragem", default=None, help="HH:MM, fronteira de barra (padrao: a da sessao do perfil)")
    p.add_argument("--data", default=None, help="dia do plano AAAA-MM-DD (padrao: pregao seguinte a ultima sessao)")
    p.add_argument("--preco", type=float, default=None, help="nivel de preco do ativo na entrada (padrao: sessao mais recente)")
    p.add_argument("--seed", type=int, default=mt.SEED_PADRAO)
    p.add_argument("--replays", type=int, default=mt.REPLAYS_PADRAO)
    p.add_argument("--slippage-stop-ticks", type=float, default=0.0, help="slippage extra do stop, em ticks (conservador)")
    p.add_argument("--sessoes", type=int, default=60, help="quantas sessoes recentes entram na piscina")
    p.add_argument("--seed-dados", type=int, default=42, help="seed dos dados sinteticos (--sintetico)")
    p.add_argument("--n-ticks", type=int, default=12_000, help="ticks por sessao sintetica (--sintetico)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd)
