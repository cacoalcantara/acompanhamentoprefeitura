"""Estatico (AST): a IA nunca le o risco soberano. Nenhum modulo de src/robowin/ia/ ou src/robowin/decisao/bracos/
importa robowin.risco (absoluto ou relativo) nem referencia, importa ou instancia ConfigRisco. Comentarios e
docstrings podem citar o nome; codigo nao. A IA ve so o envelope DERIVADO (pontos, unidades, segundos)."""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PACOTES = (SRC / "robowin" / "ia", SRC / "robowin" / "decisao" / "bracos")
PROIBIDO_MODULO = "robowin.risco"
PROIBIDO_NOME = "ConfigRisco"


def _docstrings(arvore: ast.AST) -> set[int]:
    ids: set[int] = set()
    for no in ast.walk(arvore):
        if isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and no.body:
            p = no.body[0]
            if isinstance(p, ast.Expr) and isinstance(p.value, ast.Constant) and isinstance(p.value.value, str):
                ids.add(id(p.value))
    return ids


def _modulo_absoluto(arq: Path, no: ast.ImportFrom) -> str:
    if not no.level:
        return no.module or ""
    partes = list(arq.relative_to(SRC).with_suffix("").parts)
    base = partes[:-no.level]  # o pacote do arquivo e partes[:-1]; cada nivel extra sobe um pacote
    return ".".join(base + ([no.module] if no.module else []))


def _proibido(mod: str) -> bool:
    return mod == PROIBIDO_MODULO or mod.startswith(PROIBIDO_MODULO + ".")


def violacoes_do_arquivo(arq: Path) -> list[str]:
    arvore = ast.parse(arq.read_text(encoding="utf-8"))
    docs = _docstrings(arvore)
    rel = arq.relative_to(SRC).as_posix()
    v: list[str] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            for a in no.names:
                if _proibido(a.name):
                    v.append(f"{rel}:{no.lineno}: import {a.name}")
        elif isinstance(no, ast.ImportFrom):
            mod = _modulo_absoluto(arq, no)
            if _proibido(mod) or any(_proibido(f"{mod}.{a.name}") for a in no.names):
                v.append(f"{rel}:{no.lineno}: from {mod} import ...")
            if any(a.name == PROIBIDO_NOME for a in no.names):
                v.append(f"{rel}:{no.lineno}: importa {PROIBIDO_NOME}")
        elif isinstance(no, ast.Name) and no.id == PROIBIDO_NOME:
            v.append(f"{rel}:{no.lineno}: referencia {PROIBIDO_NOME}")
        elif isinstance(no, ast.Attribute) and no.attr == PROIBIDO_NOME:
            v.append(f"{rel}:{no.lineno}: referencia .{PROIBIDO_NOME}")
        elif isinstance(no, ast.Constant) and id(no) not in docs and isinstance(no.value, str) and (
                PROIBIDO_NOME in no.value or _proibido(no.value)):
            v.append(f"{rel}:{no.lineno}: string {no.value[:40]!r} (import dinamico/getattr)")
    return v


def test_ia_e_bracos_nao_leem_o_risco():
    arquivos = [f for p in PACOTES for f in sorted(p.rglob("*.py"))]
    assert len(arquivos) > 10
    erros = [e for f in arquivos for e in violacoes_do_arquivo(f)]
    assert erros == [], erros


def test_detector_pega_as_formas_de_acesso(tmp_path, monkeypatch):
    casos = {
        "a.py": "import robowin.risco.motor\n",
        "b.py": "from robowin.risco import motor\n",
        "c.py": "from robowin import risco\n",
        "d.py": "from ..risco.politica import x\n",
        "e.py": "from robowin.core.tipos import ConfigRisco\n",
        "f.py": "import robowin.core.tipos as t\ncfg = t.ConfigRisco()\n",
        "g.py": "import importlib\nm = importlib.import_module('robowin.risco.motor')\n",
        "h.py": "x = getattr(tipos, 'ConfigRisco')\n",
    }
    pasta = tmp_path / "robowin" / "ia"
    pasta.mkdir(parents=True)
    monkeypatch.setattr(__import__(__name__), "SRC", tmp_path)
    for nome, codigo in casos.items():
        (pasta / nome).write_text(codigo, encoding="utf-8")
        assert violacoes_do_arquivo(pasta / nome), nome
    limpo = pasta / "ok.py"
    limpo.write_text('"""Cita ConfigRisco so na docstring."""\n# ConfigRisco em comentario\n'
                     "from robowin.core.tipos import EnvelopeDecisao\n", encoding="utf-8")
    assert violacoes_do_arquivo(limpo) == []
