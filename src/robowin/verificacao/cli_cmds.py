"""Comando `robowin verificar`: bateria RAPIDA de autoverificacao (sem a suite completa, sem rede, sem ordens)."""
from __future__ import annotations

import argparse
import time

from .checagens import executar_todas
from .matriz import codigo_de_saida, formatar, para_json


def _cmd_verificar(a: argparse.Namespace) -> int:
    t0 = time.monotonic()
    res = executar_todas(a.raiz, pular=set(a.pular or []), somente=set(a.somente or []) or None)
    if a.json:
        print(para_json(res))
    else:
        print(formatar(res))
        print(f"Duracao: {time.monotonic() - t0:.1f} s")
    return codigo_de_saida(res)


def _cmd_pendencias(a: argparse.Namespace) -> int:
    from .checagens import achar_raiz
    from .pendencias import formatar_pendencias, listar_pendencias
    raiz = achar_raiz(a.raiz)
    if raiz is None:
        print("ERRO: raiz do repositorio nao encontrada (use --raiz)")
        return 2
    print(formatar_pendencias(listar_pendencias(raiz)))
    return 0


def registrar(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("verificar", help="bateria rapida de autoverificacao: APROVADO / REPROVADO / NAO VERIFICAVEL AQUI")
    p.add_argument("--json", action="store_true", help="saida de maquina (JSON) em vez da matriz em texto")
    p.add_argument("--raiz", default=None, help="raiz do repositorio (padrao: pasta atual ou a de origem do pacote)")
    p.add_argument("--pular", action="append", default=None, metavar="CHECAGEM",
                   help="pula uma checagem pelo codigo (ex.: ruff, painel); repetivel")
    p.add_argument("--somente", action="append", default=None, metavar="CHECAGEM",
                   help="roda so as checagens indicadas (codigos de `checagens()`); repetivel")
    p.set_defaults(func=_cmd_verificar)

    pd = sub.add_parser("pendencias", help="lista o que ainda depende do usuario (risco, perfis, chaves) com o comando exato")
    pd.add_argument("--raiz", default=None, help="raiz do repositorio (padrao: pasta atual ou a de origem do pacote)")
    pd.set_defaults(func=_cmd_pendencias)
