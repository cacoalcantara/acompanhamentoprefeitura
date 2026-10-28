"""Teste ESTATICO (AST): `order_send` so e chamado em mt5/execucao_demo.py em todo src/robowin."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import robowin

RAIZ = Path(robowin.__file__).parent
UNICO_CHAMADOR = RAIZ / "mt5" / "execucao_demo.py"
# arquivos que podem DEFINIR/ENCAMINHAR o nome sem chama-lo
PODE_CITAR = {"execucao_demo.py", "conector.py", "duble.py", "protocolos.py"}


def _arquivos() -> list[Path]:
    return sorted(p for p in RAIZ.rglob("*.py"))


def _chamadas_order_send(arvore: ast.AST) -> list[int]:
    linhas = []
    for n in ast.walk(arvore):
        if isinstance(n, ast.Call):
            f = n.func
            nome = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if nome is not None and nome.startswith("order_send"):
                linhas.append(n.lineno)
    return linhas


def test_order_send_so_e_chamado_em_execucao_demo():
    violacoes = []
    total_no_unico = 0
    for p in _arquivos():
        linhas = _chamadas_order_send(ast.parse(p.read_text(encoding="utf-8")))
        if p == UNICO_CHAMADOR:
            total_no_unico = len(linhas)
        elif linhas:
            violacoes.append(f"{p.relative_to(RAIZ)}: linhas {linhas}")
    assert not violacoes, "order_send chamado fora de execucao_demo.py: " + "; ".join(violacoes)
    assert total_no_unico == 1, "execucao_demo.py deve ter exatamente UMA chamada a order_send"


def test_nenhuma_referencia_dinamica_a_order_send_fora_da_lista():
    """getattr(x, 'order_send') / strings contendo o nome fora dos arquivos permitidos seriam burla."""
    suspeitos = []
    for p in _arquivos():
        if p.name in PODE_CITAR:
            continue
        for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and re.fullmatch(r"\s*order_send\w*\s*", n.value):
                suspeitos.append(f"{p.relative_to(RAIZ)}:{n.lineno}")
            if isinstance(n, ast.Attribute) and n.attr.startswith("order_send"):
                suspeitos.append(f"{p.relative_to(RAIZ)}:{n.lineno}")
    assert not suspeitos, suspeitos


def test_conector_nao_chama_order_send_apenas_encaminha():
    arv = ast.parse((RAIZ / "mt5" / "conector.py").read_text(encoding="utf-8"))
    assert _chamadas_order_send(arv) == []


def test_detector_de_ast_pega_chamada_proibida():
    assert _chamadas_order_send(ast.parse("def f(t):\n    return t.order_send({})\n")) == [2]
    assert _chamadas_order_send(ast.parse("order_send({})")) == [1]
    assert _chamadas_order_send(ast.parse("x = t.order_send")) == []


def test_import_metatrader5_e_tardio():
    src = (RAIZ / "mt5" / "conector.py").read_text(encoding="utf-8")
    arv = ast.parse(src)
    topo = [n for n in arv.body if isinstance(n, ast.Import | ast.ImportFrom)]
    assert not any(getattr(a, "name", "") == "MetaTrader5" for n in topo for a in n.names)
    # nenhum outro modulo do projeto importa o pacote no topo
    for p in _arquivos():
        for n in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(n, ast.Import):
                assert all(a.name != "MetaTrader5" for a in n.names), p


def test_conector_mt4_define_order_send_mas_nao_chama_nem_cita():
    """A ponte do MT4 cumpre o Protocolo com um METODO order_send; o unico CHAMADOR segue sendo execucao_demo.py."""
    for nome in ("conector_mt4.py", "duble_mt4.py", "protocolo_mt4.py", "capacidades.py"):
        arv = ast.parse((RAIZ / "mt5" / nome).read_text(encoding="utf-8"))
        assert _chamadas_order_send(arv) == [], nome
    arv = ast.parse((RAIZ / "mt5" / "conector_mt4.py").read_text(encoding="utf-8"))
    defs = [n.name for n in ast.walk(arv) if isinstance(n, ast.FunctionDef)]
    assert "order_send" in defs
    # os comandos de ordem do protocolo da ponte nao usam o identificador (nem como string)
    from robowin.mt5 import protocolo_mt4 as P
    assert not any(c.startswith("order_send") for c in P.COMANDOS)
