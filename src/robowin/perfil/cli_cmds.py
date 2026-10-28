"""Comandos `robowin perfil listar` e `robowin perfil confirmar <ATIVO|bolsa:CODIGO>`."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..auditoria.cadeia import RegistradorAuditoria
from ..core.config import ErroConfig, carregar_toml, dir_dados
from .confirmacao import PREFIXO_BOLSA, ConfirmacaoRecusada, confirmar_perfil, interpretar_alvo, listar_status


def _ler(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # sem terminal interativo e sem --confirmo: recusa
        return ""


def _ativo_configurado(config: str) -> str | None:
    try:
        return str(carregar_toml(config).get("geral", {}).get("simbolo_base") or "").upper() or None
    except (OSError, ValueError):
        return None


def _cmd_listar(a: argparse.Namespace) -> int:
    try:
        st = listar_status(a.instrumentos, a.bolsas)
    except (ErroConfig, OSError, ValueError) as e:
        print(f"ERRO: {e}")
        return 2
    atual = _ativo_configurado(a.config)
    print(f"{'tipo':<6} {'codigo':<8} {'status':<26} {'fonte (data)':<48} confirmado por (em)")
    pendentes = []
    for s in st:
        if s.confirmado and s.bloqueado_por_bolsa:
            status = f"BLOQUEADO (bolsa {s.bloqueado_por_bolsa})"
        elif s.confirmado:
            status = "CONFIRMADO"
        else:
            status = f"A CONFIRMAR ({s.itens_pendentes} itens)"
            pendentes.append(s)
        marca = " *" if s.tipo == "ativo" and s.codigo == atual else ""
        fonte = (f"{s.fonte} ({s.fonte_data})" if s.fonte else "-")[:48]
        quem = f"{s.confirmado_por} ({s.confirmado_em})" if s.confirmado_por else "-"
        print(f"{s.tipo:<6} {s.codigo + marca:<8} {status:<26} {fonte:<48} {quem}")
    if atual:
        print(f"\n* ativo configurado em {a.config} ([geral].simbolo_base)")
    if pendentes:
        print("\nPara confirmar (depois de conferir CADA valor na B3/corretora):")
        for s in pendentes:
            alvo = f"{PREFIXO_BOLSA}{s.codigo}" if s.tipo == "bolsa" else s.codigo
            extra = " --comparar-terminal" if s.tipo == "ativo" else ""
            print(f'  uv run robowin perfil confirmar {alvo} --responsavel "<seu nome>" --fonte "<fonte>"{extra}')
    return 0


def _abrir_terminal(a: argparse.Namespace) -> Any:
    from ..core.relogio import RelogioReal
    from ..mt5.cli_cmds import _config_mt5, _terminal
    return _terminal(a, _config_mt5(a), RelogioReal())


def _cmd_confirmar(a: argparse.Namespace, entrada: Callable[[str], str] = _ler) -> int:
    try:
        alvo = interpretar_alvo(a.alvo)
    except ConfirmacaoRecusada as e:
        print(f"RECUSADO: {e}")
        return 2

    def confirmar(resumo: list[str]) -> str:
        print(f"=== Confirmacao do perfil: {alvo.rotulo} (voce declara que conferiu cada valor) ===")
        print("\n".join(resumo))
        print()
        if a.confirmo is not None:
            print(f"--confirmo informado ({a.confirmo}).")
            return str(a.confirmo)
        return entrada(f"Digite {alvo.codigo} para confirmar (qualquer outra coisa cancela): ")

    terminal = None
    if a.comparar_terminal:
        try:
            terminal = _abrir_terminal(a)
        except Exception as e:  # noqa: BLE001 - MT5IndisponivelErro etc.
            print(f"RECUSADO: nao foi possivel abrir o terminal para comparar: {e}")
            return 3
    try:
        res = confirmar_perfil(alvo, instrumentos=a.instrumentos, bolsas=a.bolsas, responsavel=a.responsavel or "",
                               confirmar=confirmar, fonte=a.fonte, fonte_data=a.fonte_data, terminal=terminal,
                               auditoria=RegistradorAuditoria(Path(a.auditoria) if a.auditoria
                                                              else dir_dados(a.config) / "auditoria"))
    except ConfirmacaoRecusada as e:
        print(f"RECUSADO: {e}")
        return 1
    finally:
        if terminal is not None:
            try:
                terminal.shutdown()
            except Exception:  # noqa: BLE001
                pass
    for m in res.mensagens:
        print(m)
    if res.alterado:
        print(f"CONFIRMADO: {alvo.rotulo} com a_confirmar = false em {res.arquivo} (resto do arquivo preservado).")
        print("Evento 'perfil_confirmado' gravado na cadeia de auditoria. Confira com: uv run robowin perfil listar")
    return 0


def registrar(sub: Any) -> None:
    p = sub.add_parser("perfil", help="perfis de ativo/bolsa: status (listar) e confirmacao pelo usuario (confirmar)")
    s = p.add_subparsers(dest="acao_perfil")

    ls = s.add_parser("listar", help="status (CONFIRMADO / A CONFIRMAR / BLOQUEADO) de todos os perfis")
    ls.add_argument("--instrumentos", default="config/instrumentos.toml")
    ls.add_argument("--bolsas", default="config/bolsas.toml")
    ls.add_argument("--config", default="config/robowin.toml")
    ls.set_defaults(func=_cmd_listar)

    c = s.add_parser("confirmar", help="mostra os valores e, digitando o simbolo, troca a_confirmar para false")
    c.add_argument("alvo", help="simbolo_base do ativo (ex.: o de [geral].simbolo_base) ou bolsa:<CODIGO>")
    c.add_argument("--responsavel", required=True, help="quem conferiu os valores (nao pode ser vazio nem 'pendente')")
    c.add_argument("--fonte", default=None, help="de onde vieram os valores (obrigatoria se o TOML nao tiver `fonte`)")
    c.add_argument("--fonte-data", default=None, help="data da fonte (AAAA-MM-DD; padrao: hoje)")
    c.add_argument("--confirmo", default=None, metavar="SIMBOLO",
                   help="uso NAO interativo: o simbolo que voce digitaria (tem de ser o do alvo)")
    c.add_argument("--comparar-terminal", action="store_true",
                   help="compara tick, valor do ponto, lote, moeda e vencimento com o terminal; divergencia => recusa")
    c.add_argument("--duble", action="store_true", help="com --comparar-terminal: usa o duble sintetico (ensaio)")
    c.add_argument("--plataforma", choices=("mt5", "mt4"), default=None)
    c.add_argument("--terminal", default=None, help="caminho do terminal portatil (terminal64.exe)")
    c.add_argument("--login", type=int, default=None)
    c.add_argument("--servidor", default=None)
    c.add_argument("--mt4-endereco", default=None)
    c.add_argument("--instrumentos", default="config/instrumentos.toml")
    c.add_argument("--bolsas", default="config/bolsas.toml")
    c.add_argument("--config", default="config/robowin.toml")
    c.add_argument("--auditoria", default=None, help="pasta da cadeia de auditoria (padrao: <dados>/auditoria)")
    c.set_defaults(func=_cmd_confirmar)

    p.set_defaults(func=lambda _a: (p.print_help(), 2)[1])
