"""CLI: cada modulo expoe `cli_cmds.registrar(sub)` e e descoberto automaticamente."""
from __future__ import annotations

import argparse
import importlib
import pkgutil

import robowin


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="robowin", description="Agente de IA para day trade de WIN (simulacao)")
    sub = p.add_subparsers(dest="cmd")
    for m in pkgutil.iter_modules(robowin.__path__):
        if not m.ispkg:
            continue
        try:
            mod = importlib.import_module(f"robowin.{m.name}.cli_cmds")
        except ModuleNotFoundError as e:
            if e.name == f"robowin.{m.name}.cli_cmds":
                continue
            raise
        mod.registrar(sub)
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
