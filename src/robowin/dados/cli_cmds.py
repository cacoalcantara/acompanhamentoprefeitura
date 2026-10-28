"""CLI: `robowin dados resumo|verificar|sintetico`."""
from __future__ import annotations

import argparse
import datetime as dt

from robowin.core.calendario_b3 import SP
from robowin.core.instrumentos import argumentos_ativo, perfil_do_cli

from .qualidade import ConfigQualidade, RelatorioQualidade, verificar
from .sintetico import ROTULO_SINTETICO, ConfigSintetico, gerar_dias
from .ticks import ArmazenamentoTicks, dia_ns


def _fmt(ns: int) -> str:
    return dt.datetime.fromtimestamp(ns / 1e9, SP).strftime("%Y-%m-%d %H:%M:%S")


def _resumo(a: argparse.Namespace) -> int:
    arm = ArmazenamentoTicks(a.dir)
    if arm.eh_sintetico:
        print(f"*** {ROTULO_SINTETICO} ***")
    simbolos = arm.simbolos()
    if not simbolos:
        print("sem dados")
        return 0
    for s in simbolos:
        off = arm.ler_offset_servidor(s)
        print(f"{s}: offset_servidor_ms={off}")
        for d in arm.datas(s):
            tk = arm.ler_dia(s, d)
            lv = arm.ler_livros(s, dia_ns(d), dia_ns(d + dt.timedelta(days=1)))
            if tk:
                print(f"  {d} ticks={len(tk)} livros={len(lv)} {_fmt(tk[0].t_utc_ns)} -> {_fmt(tk[-1].t_utc_ns)}")
            else:
                print(f"  {d} ticks=0 livros={len(lv)}")
    return 0


def _verificar(a: argparse.Namespace) -> int:
    arm = ArmazenamentoTicks(a.dir)
    sint = arm.eh_sintetico
    cfg = ConfigQualidade()
    geral = RelatorioQualidade(sintetico=sint)
    for s in arm.simbolos():
        if a.simbolo and s != a.simbolo:
            continue
        for d in arm.datas(s):
            ini, fim = dia_ns(d), dia_ns(d + dt.timedelta(days=1))
            rel = verificar(arm.ler(s, ini, fim), arm.ler_livros(s, ini, fim), cfg, sintetico=sint)
            print(f"{s} {d}: {rel.texto()}")
            geral = geral.unir(rel)
    if sint:
        print(f"*** {ROTULO_SINTETICO} ***")
    print("RESULTADO:", "CRITICO - bloqueia entradas" if geral.bloqueia_entradas else "ok")
    return 1 if geral.bloqueia_entradas else 0


def _sintetico(a: argparse.Namespace) -> int:
    arm = ArmazenamentoTicks(a.dir)
    perfil = perfil_do_cli(a)
    cfg = ConfigSintetico.do_perfil(perfil, seed=a.seed, n_ticks=a.n_ticks, data=dt.date.fromisoformat(a.data))
    for r in gerar_dias(cfg, a.dias):
        n = r.gravar(arm)
        print(f"{r.config.data} novos={n} hash={r.hash()[:12]}  [{ROTULO_SINTETICO}]")
    return 0


def registrar(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("dados", help="armazenamento e qualidade de dados")
    ss = p.add_subparsers(dest="dados_cmd")
    r = ss.add_parser("resumo", help="inventario do diretorio de dados")
    r.add_argument("--dir", default="dados_locais/ticks")
    r.set_defaults(func=_resumo)
    v = ss.add_parser("verificar", help="verificacao de qualidade (saida 1 se houver achado critico)")
    v.add_argument("--dir", default="dados_locais/ticks")
    v.add_argument("--simbolo", default=None)
    v.set_defaults(func=_verificar)
    s = ss.add_parser("sintetico", help="gera dados SINTETICOS (rotulados) para desenvolvimento")
    s.add_argument("--dir", default="dados_locais/sintetico")
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--n-ticks", type=int, default=20000)
    s.add_argument("--dias", type=int, default=1)
    s.add_argument("--data", default="2026-03-10")
    argumentos_ativo(s)
    s.set_defaults(func=_sintetico)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])
