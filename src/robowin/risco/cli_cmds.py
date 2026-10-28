"""Comandos `robowin risco aprovar` e `robowin risco status` (aprovacao da politica de risco pelo USUARIO)."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..auditoria.cadeia import RegistradorAuditoria
from ..core.config import dir_dados
from .aprovar import PALAVRA_APROVACAO, AprovacaoRecusada, aprovar_politica, estado_aprovacao


def _ler(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # stdin fechado (uso nao interativo sem --sim-eu-aprovo): conta como recusa
        return ""


def _dir_auditoria(a: argparse.Namespace) -> Path:
    return Path(a.auditoria) if a.auditoria else dir_dados(a.config) / "auditoria"


def _cmd_aprovar(a: argparse.Namespace, entrada: Callable[[str], str] = _ler) -> int:
    def confirmar(resumo: list[str]) -> str:
        print("=== Aprovacao da politica de risco (SIMULACAO/DEMO; voce assume estes limites) ===")
        print("\n".join(resumo))
        print()
        if a.sim_eu_aprovo:
            print(f"--sim-eu-aprovo informado: equivale a digitar {PALAVRA_APROVACAO}.")
            return PALAVRA_APROVACAO
        return entrada(f"Digite {PALAVRA_APROVACAO} para aprovar (qualquer outra coisa cancela): ")

    try:
        res = aprovar_politica(a.risco, a.aprovacoes, a.responsavel or "", a.motivo or "", confirmar=confirmar,
                               auditoria=RegistradorAuditoria(_dir_auditoria(a)))
    except AprovacaoRecusada as e:
        print(f"RECUSADO: {e}")
        return 1
    print(f"APROVADO: registro gravado em {res.arquivo} (sha256 {res.sha256[:16]}...).")
    if res.versao_anterior is not None:
        print(f"O registro anterior ({res.versao_anterior.name}) fica como historico; vale a versao mais recente valida.")
    print("Evento 'risco_aprovado' gravado na cadeia de auditoria. Confira com: uv run robowin verificar --somente risco")
    print("Se alterar config/risco.toml, o hash deixa de conferir e e preciso aprovar de novo.")
    return 0


def _cmd_status(a: argparse.Namespace) -> int:
    estado, detalhe = estado_aprovacao(a.risco, a.aprovacao)
    print(f"Politica de risco: {estado} - {detalhe}")
    if estado != "APROVADO":
        print('Para aprovar: uv run robowin risco aprovar --responsavel "<seu nome>" --motivo "<motivo>"')
    return 0 if estado == "APROVADO" else 1


def registrar(sub: Any) -> None:
    p = sub.add_parser("risco", help="politica de risco: aprovacao pelo usuario (aprovar) e estado (status)")
    s = p.add_subparsers(dest="acao_risco")

    ap = s.add_parser("aprovar", help="mostra os limites de config/risco.toml e grava novo registro de aprovacao (APROVO)")
    ap.add_argument("--responsavel", required=True, help="nome de quem assume o risco (nao pode ser vazio nem 'pendente')")
    ap.add_argument("--motivo", default="", help="por que estes limites estao sendo aprovados")
    ap.add_argument("--sim-eu-aprovo", action="store_true",
                    help="uso NAO interativo (scripts): equivale a digitar APROVO; o resumo e impresso do mesmo jeito")
    ap.add_argument("--risco", default="config/risco.toml")
    ap.add_argument("--aprovacoes", default="config/aprovacoes", help="pasta dos registros risco-vN.toml")
    ap.add_argument("--config", default="config/robowin.toml", help="para achar [geral].dados (auditoria)")
    ap.add_argument("--auditoria", default=None, help="pasta da cadeia de auditoria (padrao: <dados>/auditoria)")
    ap.set_defaults(func=_cmd_aprovar)

    st = s.add_parser("status", help="estado da aprovacao efetiva (versao mais recente valida)")
    st.add_argument("--risco", default="config/risco.toml")
    st.add_argument("--aprovacao", default="config/aprovacoes/risco-v1.toml")
    st.set_defaults(func=_cmd_status)

    p.set_defaults(func=lambda _a: (p.print_help(), 2)[1])
