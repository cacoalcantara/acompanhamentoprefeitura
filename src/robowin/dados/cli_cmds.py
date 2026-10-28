"""CLI: `robowin dados resumo|verificar|sintetico|baixar-b3|importar-b3`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

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


def _cfg(a: argparse.Namespace) -> dict:
    from robowin.core.config import carregar_toml
    return carregar_toml(a.config) if Path(a.config).is_file() else {}


def _baixar_b3(a: argparse.Namespace) -> int:
    from robowin.core.config import dir_dados

    from .b3_negocios import baixar_b3, config_b3
    perfil = perfil_do_cli(a)
    cfg_b3 = config_b3(_cfg(a), tentativas=a.tentativas)
    cache = Path(a.saida) if a.saida else (Path(cfg_b3.cache) if cfg_b3.cache else dir_dados(a.config) / "b3" / "negocios")
    try:
        res = baixar_b3(perfil, cfg_b3, cache, de=dt.date.fromisoformat(a.de) if a.de else None,
                        ate=dt.date.fromisoformat(a.ate) if a.ate else None, ultimos=a.ultimos,
                        ao_progresso=lambda r: print(r.linha(), flush=True))
    except ValueError as e:
        print(f"ERRO: {e}")
        return 2
    ok = [r for r in res if r.disponivel]
    falhas = [r for r in res if r.status == "falha"]
    print(f"baixados/em cache: {len(ok)}; sem dados: {sum(r.status == 'sem_dados' for r in res)}; "
          f"falhas: {len(falhas)}; cache em {cache}")
    if a.json:
        Path(a.json).write_text(json.dumps([r.__dict__ for r in res], default=str, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return 0 if ok or not res else 1


def _importar_b3(a: argparse.Namespace) -> int:
    from robowin.core.config import dir_dados

    from .b3_negocios import ErroFormatoB3, arquivos_de, config_b3, formatar_qualidade, importar_arquivo
    from .ticks import MisturaDeDados
    perfil = perfil_do_cli(a)
    cfg_b3 = config_b3(_cfg(a), atraso_entrega_ms=a.atraso_ms, agregacao=a.agregacao)
    arm = ArmazenamentoTicks(a.armazem or dir_dados(a.config) / "ticks_b3")
    arquivos = arquivos_de(a.arquivo)
    if not arquivos:
        print("ERRO: nenhum arquivo .zip/.txt encontrado")
        return 2
    relatorio: list[dict] = []
    erros = 0
    for arq in arquivos:
        try:
            r = importar_arquivo(arq, arm, perfil, cfg_b3, ticker=a.ticker)
        except MisturaDeDados as e:
            print(f"ERRO (mistura de dados recusada): {e}")
            return 2
        except (ErroFormatoB3, OSError, ValueError) as e:
            print(f"ERRO em {arq}: {e}")
            erros += 1
            continue
        print(f"{arq.name}: {r.linhas} linhas, {r.invalidas} invalidas, {r.outros_instrumentos} de outros instrumentos")
        for q in r.dias:
            print("  " + formatar_qualidade(q).replace("\n", "\n  "))
        relatorio.append(r.como_dict())
    print(f"armazem REAL: {arm.raiz} (origem real_b3_negocios; lado INFERIDO pela regra do tick; sem livro)")
    if a.relatorio:
        destino = Path(a.relatorio)
        destino.parent.mkdir(parents=True, exist_ok=True)
        if destino.suffix.lower() == ".json":
            destino.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        else:
            linhas = ["# Qualidade dos negocios B3 importados", "",
                      "> Dados reais (negocios publicos da B3); lado agressor INFERIDO pela regra do tick; sem livro.", ""]
            for r_ in relatorio:
                linhas += [f"- `{formatar_qualidade(q)}`".replace("\n", "`\n  - `") for q in r_["dias"]]
            destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return 1 if erros and not relatorio else 0


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
    b = ss.add_parser("baixar-b3", help="baixa negocios intradiarios PUBLICOS da B3 (contrato vigente de cada pregao)")
    b.add_argument("--de", default=None, help="AAAA-MM-DD")
    b.add_argument("--ate", default=None, help="AAAA-MM-DD (padrao: pregao anterior a hoje)")
    b.add_argument("--ultimos", type=int, default=None, help="os ultimos N pregoes DISPONIVEIS (pula dias sem dados)")
    b.add_argument("--saida", default=None, help="cache dos ZIPs (padrao [dados_b3].cache ou <dados>/b3/negocios)")
    b.add_argument("--tentativas", type=int, default=None, help="novas tentativas por pregao (padrao [dados_b3])")
    b.add_argument("--json", default=None, help="grava o resultado do download neste arquivo JSON")
    argumentos_ativo(b)
    b.set_defaults(func=_baixar_b3)
    m = ss.add_parser("importar-b3", help="importa ZIP/TXT de negocios da B3 para um armazem de ticks REAL")
    m.add_argument("--arquivo", nargs="+", required=True, help="ZIP(s), TXT(s) ou diretorio(s)")
    m.add_argument("--armazem", default=None, help="armazem de ticks (padrao <dados>/ticks_b3; nunca o sintetico)")
    m.add_argument("--ticker", default=None, help="contrato (padrao: o do nome do arquivo)")
    m.add_argument("--atraso-ms", type=float, default=None, help="atraso de entrega (padrao [dados_b3])")
    m.add_argument("--agregacao", choices=["ms_preco", "nenhuma"], default=None)
    m.add_argument("--relatorio", default=None, help="grava o relatorio de qualidade (.md ou .json)")
    argumentos_ativo(m)
    m.set_defaults(func=_importar_b3)
    p.set_defaults(func=lambda a: (p.print_help(), 2)[1])
