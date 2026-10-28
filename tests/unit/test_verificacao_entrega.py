"""docs/ENTREGA.md so aponta para arquivos que existem, tem os 4 status e o aviso de que lucratividade nao e provada."""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
DOC = RAIZ / "docs" / "ENTREGA.md"
PREFIXOS = ("src/", "tests/", "docs/", "config/", "scripts/", "mql4/", ".github/")


def _caminhos() -> set[str]:
    out = set()
    for trecho in re.findall(r"`([^`\n]+)`", DOC.read_text(encoding="utf-8")):
        if trecho.startswith(PREFIXOS) and " " not in trecho and "<" not in trecho:
            out.add(trecho)
    return out


def test_todo_caminho_citado_existe():
    caminhos = _caminhos()
    assert len(caminhos) > 60
    ausentes = []
    for c in sorted(caminhos):
        existe = any(RAIZ.glob(c)) if "*" in c else (RAIZ / c).exists()
        if not existe:
            ausentes.append(c)
    assert not ausentes, ausentes


def test_resumo_honesto_e_status_presentes():
    txt = DOC.read_text(encoding="utf-8")
    topo = txt[: txt.index("## Pedido 1")]
    assert "Lucratividade nunca é provada" in topo.replace("**", "").capitalize() or "nunca é provada" in topo
    for termo in ("Depende do usuário", "terminal MT5/MT4", "compilar", "aprovação de risco", "chaves"):
        assert termo in topo, termo
    for sigla in ("VA", "VD", "NV", "DU"):
        assert f"**{sigla}**" in topo


def test_cobre_todos_os_itens_do_pedido():
    txt = DOC.read_text(encoding="utf-8")
    for h in ("## Pedido 1", "## Pedido 2", "## Pedido 3", "## Pedido 4", "## Pedido 5", "## Pedido 6", "## Pedido 7",
              "## Decisão definitiva", "## Componentes adicionais", "## MT4, ativos/bolsas", "## O que continua não verificado"):
        assert h in txt, h


def test_readme_aponta_para_verificar_e_entrega():
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    assert "robowin verificar" in readme and "docs/ENTREGA.md" in readme
