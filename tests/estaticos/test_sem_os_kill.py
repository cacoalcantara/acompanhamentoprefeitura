"""Estatico: os.kill so no helper multiplataforma _pid_vivo (no Windows os.kill(pid, 0) envia CTRL_C_EVENT)."""
import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2] / "src" / "robowin"
PERMITIDO = {("execucao/ponte_gateway.py", "_pid_vivo")}


def test_os_kill_somente_no_helper():
    achados = []
    for arq in RAIZ.rglob("*.py"):
        arvore = ast.parse(arq.read_text(encoding="utf-8"))
        for func in [n for n in ast.walk(arvore) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] + [arvore]:
            nome = getattr(func, "name", "<modulo>")
            for n in ast.walk(func):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("kill", "killpg")
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == "os"):
                    rel = arq.relative_to(RAIZ).as_posix()
                    if (rel, nome) not in PERMITIDO and nome != "<modulo>":
                        achados.append(f"{rel}:{n.lineno} ({nome})")
    assert not achados, f"os.kill fora do helper multiplataforma: {achados}"
