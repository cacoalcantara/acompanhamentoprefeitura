"""Comando `robowin aceitacao`: roteiro de aceitacao SOMENTE DEMO para rodar no Windows do usuario.

Sem `--confirmo-conta-demo` so ha etapas de LEITURA (nenhuma ordem). Com a flag, as ordens de teste passam EXCLUSIVAMENTE por
`mt5.execucao_demo.ExecucaoDemo`. Conta real/contest, servidor sem 'DEMO' ou login diferente do esperado => aborta.
`--duble` roda tudo contra o duble sintetico com relogio simulado (rapido; NAO vale como aceitacao real).

Codigos de saida: 0 sem REPROVADO; 1 algum REPROVADO (inclui conta recusada); 2 erro de uso/configuracao; 3 terminal indisponivel.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import time
from pathlib import Path
from typing import Any

from robowin.core.config import ErroConfig, carregar_toml
from robowin.core.instrumentos import PerfilInstrumento, perfil_da_config
from robowin.core.relogio import Relogio, RelogioReal, RelogioSimulado
from robowin.core.tipos import ConfigRisco
from robowin.mt5.capacidades import capacidades_do_terminal, normalizar_plataforma
from robowin.mt5.cli_cmds import _comuns, _config_mt5, _servidor_duble, _terminal
from robowin.verificacao.checagens import achar_raiz

from .etapas import Contexto, executar
from .matriz import ResultadoEtapa, codigo_de_saida, higienizar, reprovada
from .relatorio import gravar, montar

SP = dt.timezone(dt.timedelta(hours=-3))
# O duble tem relogio proprio (simulado): terca-feira 27/10/2026, 11:00 em Sao Paulo, dentro do pregao e da janela de entradas.
INICIO_DUBLE = dt.datetime(2026, 10, 27, 11, 0, tzinfo=SP)
LOGIN_DUBLE = 900001


class _TerminalIndisponivel:
    """Terminal que nao abriu: a A1 reporta o motivo e a execucao aborta (nada mais e tentado)."""

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo

    def initialize(self, **kw: Any) -> bool:
        return False

    def last_error(self) -> tuple[int, str]:
        return (-1, self.motivo)

    def shutdown(self) -> None:
        return None


def criar_duble_sim(plataforma: str, relogio: Relogio, n_ticks: int, **kw: Any) -> Any:
    """Duble MT5 ou MT4 (sem rede, sem threads) sobre o relogio simulado. `kw` vai ao duble (ex.: trade_mode='real' nos testes)."""
    if plataforma == "mt4":
        from robowin.mt5.conector_mt4 import ConectorMT4, TransporteLocal
        from robowin.mt5.duble_mt4 import criar_duble_mt4_padrao
        d4 = criar_duble_mt4_padrao(relogio, n_ticks=n_ticks, **kw)
        return ConectorMT4(transporte=TransporteLocal(d4))
    from robowin.mt5.duble import criar_duble_padrao
    return criar_duble_padrao(relogio, n_ticks=n_ticks, **kw)


def _n_ticks_duble(minutos: float, repeticoes: int, pausa_s: float) -> int:
    """Ticks (a cada 100 ms) para cobrir todas as etapas no tempo simulado, com folga."""
    seg = minutos * 60 + 120 + repeticoes * (pausa_s + 8) + 400 + 400 + 300
    return int(seg * 10) + 2000


def _risco(raiz: Path | None) -> tuple[ConfigRisco, str]:
    if raiz is not None and (raiz / "config" / "risco.toml").is_file():
        return ConfigRisco(**carregar_toml(raiz / "config" / "risco.toml")), "config/risco.toml"
    return ConfigRisco(), "padrao embutido (config/risco.toml ausente)"


def _config(args: argparse.Namespace, raiz: Path | None) -> tuple[str, dict[str, Any]]:
    caminho = Path(args.config)
    if not caminho.is_file() and raiz is not None and (raiz / "config" / "robowin.toml").is_file():
        caminho = raiz / "config" / "robowin.toml"
    bruto = carregar_toml(caminho) if caminho.is_file() else {}
    return str(caminho), bruto


def formatar_texto(rel: dict[str, Any], arquivos: tuple[Path, Path] | None = None) -> str:
    larg = max([len(r["etapa"]) for r in rel["etapas"]] + [4])
    L = ["=" * 100, f"  robowin aceitacao - {rel['plataforma'].upper()} - ativo {rel['ativo']} - "
         f"{'DUBLE SINTETICO (NAO e o terminal real)' if rel['duble'] else 'conta DEMO real'}",
         f"  ordens enviadas: {rel['ordens_enviadas']}   --confirmo-conta-demo: {'sim' if rel['confirmou_conta_demo'] else 'NAO (so leitura)'}",
         "=" * 100]
    for r in rel["etapas"]:
        L.append(f"[{r['estado']:<20}] {r['etapa']:<{larg}} {r['nome']}: {r['detalhe']}".rstrip())
    c = rel["contagem"]
    L += ["-" * 100, f"Resumo: {c['APROVADO']} aprovada(s), {c['REPROVADO']} reprovada(s), {c['NAO VERIFICAVEL AQUI']} nao verificavel(is).",
          f"VEREDITO: {rel['veredito']}"]
    if arquivos:
        L.append(f"Relatorio: {arquivos[0]}  e  {arquivos[1]}")
    return "\n".join(L)


def rodar(args: argparse.Namespace, *, terminal: Any = None, relogio: Relogio | None = None,
          dormir: Any = None) -> int:
    raiz = achar_raiz(getattr(args, "raiz", None))
    caminho_cfg, bruto = _config(args, raiz)
    args.config = caminho_cfg
    try:
        perfil: PerfilInstrumento = perfil_da_config(bruto, args.ativo, permitir_a_confirmar=True)
    except ErroConfig as e:
        print(f"ERRO: {e}")
        return 2
    cfg_mt5 = _config_mt5(args)
    try:
        plat = normalizar_plataforma(getattr(args, "plataforma", None) or cfg_mt5.get("plataforma"))
    except ValueError as e:
        print(f"ERRO: {e}")
        return 2
    risco, origem_risco = _risco(raiz)
    login = args.login or cfg_mt5.get("login") or (LOGIN_DUBLE if args.duble else None)
    servidor = args.servidor or cfg_mt5.get("servidor") or (_servidor_duble(plat) if args.duble else None)
    login = int(login) if login else None

    if args.duble:
        relogio = relogio or RelogioSimulado(int(INICIO_DUBLE.timestamp() * 1e9))
        if dormir is None:
            sim = relogio

            def dormir(s: float) -> None:  # noqa: E306 - tempo simulado: dormir = avancar o relogio
                sim.avancar(int(s * 1e9))  # type: ignore[attr-defined]
        intervalo = 0.5
        if terminal is None:
            terminal = criar_duble_sim(plat, relogio, _n_ticks_duble(args.minutos_coleta, args.repeticoes, args.pausa_s))
    else:
        relogio = relogio or RelogioReal()
        dormir = dormir or time.sleep
        intervalo = 0.25
        if terminal is None:
            try:
                terminal = _terminal(args, cfg_mt5, relogio)
            except Exception as e:  # noqa: BLE001 - MT5IndisponivelErro, ZeroMQ ausente etc.
                terminal = _TerminalIndisponivel(f"nao foi possivel abrir o terminal {plat.upper()}: {type(e).__name__}: {e}")

    ctx = Contexto(
        terminal=terminal, plataforma=plat, caps=capacidades_do_terminal(terminal, plat), perfil=perfil, risco=risco, cfg=bruto,
        relogio=relogio, dormir=dormir, duble=bool(args.duble), login=login, servidor=servidor,
        confirmo=bool(args.confirmo_conta_demo), saida=Path(args.saida), minutos_coleta=float(args.minutos_coleta),
        repeticoes=int(args.repeticoes), pausa_s=float(args.pausa_s), limite_latencia_ms=float(args.limite_latencia_ms),
        intervalo_poll_s=intervalo, pular_ia=bool(args.pular_ia))
    ctx.versoes["risco_origem"] = origem_risco
    indisponivel = isinstance(terminal, _TerminalIndisponivel)
    if not ctx.confirmo:
        print("AVISO: sem --confirmo-conta-demo NENHUMA ordem sera enviada (somente etapas de leitura).")
    res: list[ResultadoEtapa] = executar(ctx)
    try:
        terminal.shutdown()
    except Exception:  # noqa: BLE001
        pass
    if not ctx.confirmo and ctx.envios != 0:  # defesa em profundidade: nunca deve ocorrer
        res.append(reprovada("SEG", "Seguranca", "sem a flag, zero ordens", f"{ctx.envios} ordem(ns) enviada(s) sem a flag"))
    rel = montar(ctx, res, raiz)
    p_md, p_js, rel_h = gravar(rel, Path(args.saida), logins=(ctx.login, ctx.conta.get("login")))
    if args.json:
        print(json.dumps(rel_h, ensure_ascii=False, indent=2))
    else:
        print(higienizar(formatar_texto(rel_h, (p_md, p_js)), (ctx.login, ctx.conta.get("login"))))
    if indisponivel:
        return 3
    return codigo_de_saida(res)


def _cmd_aceitacao(args: argparse.Namespace) -> int:
    if args.minutos_coleta <= 0 or args.repeticoes < 1 or math.isnan(args.minutos_coleta):
        print("ERRO: --minutos-coleta deve ser > 0 e --repeticoes >= 1")
        return 2
    return rodar(args)


def registrar(sub: Any) -> None:
    p = sub.add_parser(
        "aceitacao", help="aceitacao no terminal (SOMENTE DEMO): roteiro A1..A11 com relatorio aceitacao-<data>.md/.json")
    _comuns(p)
    p.add_argument("--confirmo-conta-demo", action="store_true",
                   help="OBRIGATORIA para enviar ordens de teste (1 contrato) na conta DEMO; sem ela so ha etapas de leitura")
    p.add_argument("--minutos-coleta", type=float, default=5, help="duracao da coleta de ticks/livro (A4), em minutos")
    p.add_argument("--repeticoes", type=int, default=10, help="repeticoes da medicao de latencia (A6) e de chamadas de IA (A10)")
    p.add_argument("--pausa-s", type=float, default=3.0, help="pausa entre repeticoes/etapas de ordem, em segundos")
    p.add_argument("--limite-latencia-ms", type=float, default=1500.0, help="criterio de sanidade: p95 do order_send->confirmacao")
    p.add_argument("--saida", default="dados_locais/aceitacao", help="pasta do relatorio aceitacao-<data>.md/.json")
    p.add_argument("--json", action="store_true", help="imprime o relatorio em JSON em vez da matriz em texto")
    p.add_argument("--pular-ia", action="store_true", help="marca a A10 (IA ao vivo) como nao aplicavel")
    p.add_argument("--raiz", default=None, help="raiz do repositorio (padrao: pasta atual ou a de origem do pacote)")
    p.set_defaults(func=_cmd_aceitacao)
