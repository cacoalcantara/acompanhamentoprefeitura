"""Testes do linter estatico MQL4 (scripts/lint_mql4.py): erros plantados sao detectados e o EA real passa sem ERRO."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("lint_mql4", RAIZ / "scripts" / "lint_mql4.py")
L = importlib.util.module_from_spec(_spec)
sys.modules["lint_mql4"] = L
_spec.loader.exec_module(L)  # type: ignore[union-attr]

EA = (RAIZ / "mql4" / "RobowinBridge.mq4").read_text(encoding="utf-8")
MD = (RAIZ / "mql4" / "PROTOCOLO.md").read_text(encoding="utf-8")
PY = (RAIZ / "src" / "robowin" / "mt5" / "protocolo_mt4.py").read_text(encoding="utf-8")
CN = (RAIZ / "src" / "robowin" / "mt5" / "conector_mt4.py").read_text(encoding="utf-8")


def achar(fonte: str, **kw):
    return L.lint_fonte(fonte, **kw)[0]


def codigos(fonte: str, sev: str | None = None, **kw) -> set[str]:
    return {a.codigo for a in achar(fonte, **kw) if sev is None or a.severidade == sev}


def corpo(txt: str) -> str:
    return "int OnInit()\n{\n" + txt + "\n return(INIT_SUCCEEDED);\n}\n"


def trocar(fonte: str, antigo: str, novo: str) -> str:
    assert fonte.count(antigo) >= 1, antigo
    return fonte.replace(antigo, novo, 1)


# ------------------------------------------------------------------ o EA real
def test_ea_real_passa_sem_erro():
    achados = achar(EA, md=MD, py=PY, conector=CN)
    erros = [a for a in achados if a.severidade == L.ERRO]
    assert erros == [], "\n".join(a.formatar() for a in erros)


def test_ea_real_extrai_estrutura():
    _, an = L.lint_fonte(EA)
    assert len(an.funcs) > 40 and "Tratar" in an.funcs and "CmdOrdemAbrir" in an.funcs
    assert set(L.comandos_do_ea(an)) == {"ola", "conta", "simbolos", "simbolo", "selecionar", "tick", "ticks", "posicoes",
                                         "pendentes", "historico", "ordem_abrir", "ordem_fechar", "ordem_modificar",
                                         "ordem_apagar"}


def test_ea_real_marca_revisar_conhecidos():
    cod = codigos(EA, L.REVISAR, md=MD, py=PY, conector=CN)
    assert {"CONSTANTE-CONFERIR", "ZMQ-CONFERIR", "ORDEM-SEM-SELECT", "PP-INCLUDE-EXTERNO"} <= cod


# ------------------------------------------------------------------ tokenizador
def test_tokenizador_literais_e_comentarios():
    fonte = ('int x = 0xFF; // c "nao string"\n/* bloco\n com "aspas */ color c = C\'255,0,10\';\n'
             'datetime d = D\'2020.01.31 23:59:59\'; string s = "a\\"b//c"; double e = 1.5e-3 + .5; char q = \'\\n\';\n')
    toks, _, ach = L.tokenizar(fonte)
    assert ach == []
    tipos = [(t.kind, t.text) for t in toks]
    assert ("num", "0xFF") in tipos and ("color", "C'255,0,10'") in tipos
    assert ("dt", "D'2020.01.31 23:59:59'") in tipos and ("str", '"a\\"b//c"') in tipos
    assert ("num", "1.5e-3") in tipos and ("num", ".5") in tipos and ("chr", "'\\n'") in tipos
    assert [t.line for t in toks if t.text == "color"] == [3]


@pytest.mark.parametrize("fonte,cod", [
    ('string s = "aberta;\n', "LEX-STRING"),
    ("/* nunca fecha\nint x;", "LEX-COMENTARIO"),
    ("color c = C'300,0,0';", "LEX-COR"),
    ("datetime d = D'2020.13.01';", "LEX-DATETIME"),
    ("int x = 0x;", "LEX-NUMERO"),
    ("int x = 12abc;", "LEX-NUMERO"),
    ("int x = 1 @ 2;", "LEX-CARACTERE"),
])
def test_erros_lexicos(fonte, cod):
    assert cod in codigos(fonte, L.ERRO)


# ------------------------------------------------------------------ balanceamento e diretivas
def test_balanceamento():
    assert "BAL-ABERTO" in codigos("int f()\n{\n if(1 {\n return(1);\n}\n")
    assert "BAL-SOBRA" in codigos("int f()\n{\n return(1);\n}\n}\n")
    assert "BAL-CRUZADO" in codigos("int f()\n{\n int a[3);\n return(1);\n}\n")


def test_diretivas():
    assert "PP-IMPORT" in codigos('#import "a.dll"\nint F(int a);\n')
    assert "PP-IMPORT" in codigos("#import\n")
    assert "PP-INCLUDE" in codigos("#include stdlib.mqh\n")
    assert "PP-IF" in codigos("#ifdef X\nint a;\n")
    assert "MQL5-INCLUDE" in codigos("#include <Trade\\Trade.mqh>\n")
    assert "PP-VERSION" in codigos('#property version "1.2.3.4"\n')
    assert "PP-INCLUDE-EXTERNO" in codigos("#include <Zmq/Zmq.mqh>\n", L.REVISAR)


# ------------------------------------------------------------------ ';'
def test_ponto_e_virgula_faltando_no_ea():
    assert "PV-FALTA" in codigos(trocar(EA, "g_seq[slot]++;", "g_seq[slot]++"), L.ERRO)
    q2 = trocar(EA, "int idx = slot * CAP + (int)((g_seq[slot] - 1) % CAP);", "int idx = slot * CAP + (int)((g_seq[slot] - 1) % CAP)")
    assert "PV-FALTA" in codigos(q2, L.ERRO)
    assert "PV-FALTA" in codigos(trocar(EA, "long     g_epoca = 0;", "long     g_epoca = 0"), L.ERRO)


def test_pv_faltando_variados():
    assert "PV-FALTA" in codigos(corpo("int a = 1\n int b = 2;"))
    assert "PV-FALTA" in codigos(corpo("Print(\"x\")\n Print(\"y\");"))
    assert "PV-FALTA" in codigos(corpo("int a = 1\n if(a > 0) a = 2;"))
    assert "PV-FALTA" in codigos("input int X = 1\ninput int Y = 2;\n")
    assert "PV-FALTA" in codigos("int f()\n{\n return(1)\n}\n")
    assert "PV-FALTA" in codigos("struct S { int a; }\nint g;\n")
    assert "SINTAXE" in codigos(corpo("int a = 1 2;"))


def test_pv_continuacao_de_expressao_nao_e_erro():
    assert "PV-FALTA" not in codigos(corpo('string s = "a" +\n "b"\n + "c";\n int v = (int)\n 5;'))
    assert "PV-FALTA" not in codigos(corpo("int a[3] = {1, 2, 3};\n int b = a[0] ? 1 :\n 2;"))


def test_ponto_e_virgula_sobrando():
    assert "PV-SOBRANDO" in codigos(corpo("int a = 1;;"))
    assert "PV-CORPO-VAZIO" in codigos(corpo("int a = 1;\n if(a > 0);\n a = 2;"))
    assert "PV-CORPO-VAZIO" in codigos(corpo("int a = 1;\n for(int i = 0; i < 3; i++);"))
    assert "PV-SOBRANDO" in codigos("int f()\n{\n return(1);\n};\n")
    assert "PV-CORPO-VAZIO" not in codigos(corpo("int i = 0;\n do { i++; } while(i < 3);"))


# ------------------------------------------------------------------ return
def test_funcao_sem_return():
    assert "RET-AUSENTE" in codigos("int f()\n{\n int a = 1;\n}\n", L.ERRO)
    ea_sem = trocar(EA, '   return(OkJson(id, "{\\"simbolos\\":[" + lista + "]}"));', '   OkJson(id, "x");')
    assert "RET-AUSENTE" in codigos(ea_sem, L.ERRO)


def test_caminhos_de_return():
    assert "RET-CAMINHOS" in codigos("int f(int a)\n{\n if(a > 0)\n return(1);\n}\n", L.AVISO)
    assert not codigos("int f(int a)\n{\n if(a > 0)\n return(1);\n else\n return(2);\n}\n", L.AVISO)
    assert not codigos("int f(int a)\n{\n while(true)\n {\n if(a > 0)\n return(1);\n a++;\n }\n}\n", L.AVISO)
    assert "RET-AUSENTE" in codigos("int f(int a)\n{\n while(true)\n {\n if(a > 0)\n break;\n }\n}\n", L.ERRO)
    assert not codigos("int f(int a)\n{\n switch(a)\n {\n case 1: return(1);\n default: return(2);\n }\n}\n", L.AVISO)


def test_return_incompativel_com_tipo():
    assert "RET-VOID" in codigos("void f()\n{\n return(1);\n}\n")
    assert "RET-SEM-VALOR" in codigos("int f()\n{\n return;\n}\n")


# ------------------------------------------------------------------ DLL
DLL = '#import "x.dll"\nint Soma(int a, int b);\nvoid Msg(string &s, double d = 0.0);\n#import\n'


def test_dll_ok_e_aridade_errada():
    ok = DLL + corpo('int r = Soma(1, 2);\n string t = ""; Msg(t);')
    assert not codigos(ok, L.ERRO)
    assert "DLL-ARIDADE" in codigos(DLL + corpo("int r = Soma(1);"), L.ERRO)
    assert "DLL-ARIDADE" in codigos(DLL + corpo("int r = Soma(1, 2, 3);"), L.ERRO)
    assert "DLL-IMPORT" in codigos(ok, L.REVISAR)


def test_dll_tipos_errados():
    assert "DLL-TIPO" in codigos(DLL + corpo('int r = Soma("a", 2);'), L.ERRO)
    assert "DLL-TIPO" in codigos(DLL + corpo("Msg(5);"), L.ERRO)
    assert "REF-ARG" in codigos(DLL + corpo('Msg("lit");'), L.ERRO)


def test_dll_lista_importadas():
    _, an = L.lint_fonte(DLL)
    assert [(f.nome, f.ret, f.aridade) for f in an.imports] == [("Soma", "int", (2, 2)), ("Msg", "void", (1, 2))]
    assert an.imports[0].importada == "x.dll"


# ------------------------------------------------------------------ MQL5 por engano
@pytest.mark.parametrize("trecho,cod", [
    ("CTrade t;", "MQL5-TIPO"),
    ('PositionSelect("EURUSD");', "MQL5-FUNCAO"),
    ("MqlTradeRequest req;", "MQL5-TIPO"),
    ("int x = TRADE_ACTION_DEAL;", "MQL5-CONSTANTE"),
    ("int x = ORDER_TYPE_BUY;", "MQL5-CONSTANTE"),
    ("int ok = OrderSend(req, res);", "MQL5-ORDERSEND"),
])
def test_mql5_por_engano(trecho, cod):
    assert cod in codigos(corpo(trecho), L.ERRO)


def test_enum_e_long_viram_revisar():
    assert "ENUM" in codigos(corpo("ENUM_ORDER_TYPE t = 0;"), L.REVISAR)
    assert "TIPO-LONG" in codigos(corpo("ulong u = 1;"), L.REVISAR)
    assert "ENUM" not in codigos(corpo("ENUM_TIMEFRAMES t = PERIOD_M1;"))


# ------------------------------------------------------------------ ordens
def test_orders_checks():
    send = "int t = OrderSend(Symbol(), OP_BUY, 0.1, Ask, 3, 0, 0);\n"
    assert "ORDERSEND-SEM-CHECAGEM" in codigos(corpo(send + "Print(t);"), L.AVISO)
    assert "ORDERSEND-SEM-CHECAGEM" not in codigos(corpo(send + 'if(t < 0) Print("x");'))
    assert "ORDERSEND-SEM-CHECAGEM" not in codigos(corpo(send + 'if(t == -1)\n Print("x");'))
    assert "RETORNO-IGNORADO" in codigos(corpo("OrderSend(Symbol(), OP_BUY, 0.1, Ask, 3, 0, 0);"), L.AVISO)
    assert "RETORNO-IGNORADO" in codigos(corpo("OrderClose(1, 0.1, Bid, 3);"), L.AVISO)
    assert "RETORNO-IGNORADO" in codigos(corpo("OrderSelect(0, SELECT_BY_POS);"), L.AVISO)
    assert "ARIDADE" in codigos(corpo("int t = OrderSend(Symbol(), OP_BUY, 1.0);\n if(t < 0) return(1);"), L.ERRO)


def test_getter_sem_select():
    f = "int Magic()\n{\n return(OrderMagicNumber());\n}\n"
    assert "ORDEM-SEM-SELECT" in codigos(f, L.REVISAR)
    g = "int Magic()\n{\n if(!OrderSelect(0, SELECT_BY_POS))\n return(0);\n return(OrderMagicNumber());\n}\n"
    assert "ORDEM-SEM-SELECT" not in codigos(g)


def test_divisao_por_zero_literal():
    assert "DIV-ZERO" in codigos(corpo("double a = 1.0 / 0;"), L.ERRO)
    assert "DIV-ZERO" in codigos(corpo("int a = 5 % 0;"), L.ERRO)
    assert "DIV-ZERO" in codigos(corpo("double a = 1 / 0.0;"), L.ERRO)
    assert "DIV-ZERO" not in codigos(corpo("double a = 1.0 / 2;"))


def test_funcoes_e_constantes_desconhecidas():
    cod = codigos(corpo("int a = FuncaoInventada(1);\n int b = CONSTANTE_INVENTADA;"), L.REVISAR)
    assert {"FUNCAO-DESCONHECIDA", "IDENT-DESCONHECIDO"} <= cod
    assert not codigos(corpo("int a = OrdersTotal();\n int b = MODE_BID + clrRed + OP_BUY;"), L.REVISAR)
    assert "ARIDADE" in codigos(corpo('string s = StringSubstr("a");'), L.ERRO)
    assert "ARIDADE" in codigos("int f(int a)\n{\n return(a);\n}\nint OnInit()\n{\n f();\n return(0);\n}\n", L.ERRO)
    assert "SEM-INCLUDE" in codigos(corpo("string s = ErrorDescription(1);"), L.ERRO)


# ------------------------------------------------------------------ protocolo
def test_protocolo_ok():
    assert not [a for a in achar(EA, md=MD, py=PY, conector=CN) if a.codigo.startswith("PROTO") and a.severidade == L.ERRO]


def test_protocolo_chave_faltando_no_doc():
    md2 = trocar(MD, "`tick_value`, ", "")
    achados = achar(EA, md=md2, py=PY, conector=CN)
    assert any(a.codigo == "PROTO-DADOS-DOC" and "tick_value" in a.msg and a.severidade == L.ERRO for a in achados)


def test_protocolo_chave_faltando_no_ea():
    ea2 = trocar(EA, '",\\"lote_passo\\":" + D(MarketInfo(sym, MODE_LOTSTEP))', '""')
    achados = achar(ea2, md=MD, py=PY, conector=CN)
    assert any(a.codigo == "PROTO-DADOS-EA" and "lote_passo" in a.msg and a.severidade == L.ERRO for a in achados)


def test_protocolo_arg_lido_nao_documentado():
    ea2 = trocar(EA, 'JStr(req, "comentario")', 'JStr(req, "comentario_novo")')
    achados = achar(ea2, md=MD, py=PY, conector=CN)
    assert any(a.codigo == "PROTO-ARG-DOC" and "comentario_novo" in a.msg for a in achados)


def test_protocolo_comando_divergente():
    ea2 = trocar(EA, 'cmd == "historico"', 'cmd == "historia"')
    cods = {a.codigo for a in achar(ea2, md=MD, py=PY, conector=CN) if a.severidade == L.ERRO}
    assert {"PROTO-CMD-DOC", "PROTO-CMD-PY", "PROTO-CMD-EA"} <= cods


def test_protocolo_codigo_de_erro_divergente():
    ea2 = trocar(EA, "#define ERRO_MAGIC         1012", "#define ERRO_MAGIC         1013")
    cods = {a.codigo for a in achar(ea2, md=MD, py=PY, conector=CN) if a.severidade == L.ERRO}
    assert {"PROTO-ERRO-PY", "PROTO-ERRO-DOC", "PROTO-ERRO-EA"} <= cods
    ea3 = trocar(EA, "#define VERSAO_PROTOCOLO   1", "#define VERSAO_PROTOCOLO   2")
    assert "PROTO-VERSAO" in {a.codigo for a in achar(ea3, md=MD, py=PY, conector=CN)}


def test_protocolo_ordem_diferente():
    ea2 = trocar(EA, ' || cmd == "ordem_apagar"', "")
    assert "PROTO-ORDEM" in {a.codigo for a in achar(ea2, md=MD, py=PY, conector=CN)}


# ------------------------------------------------------------------ CLI
def test_cli_codigos_de_saida(tmp_path, capsys):
    assert L.main([str(RAIZ / "mql4" / "RobowinBridge.mq4")]) == 0
    saida = capsys.readouterr().out
    assert "REVISAR" in saida and "NAO e um compilador" in saida
    ruim = tmp_path / "x.mq4"
    ruim.write_text("int f()\n{\n int a = 1\n}\n", encoding="utf-8")
    assert L.main([str(ruim), "--sem-protocolo"]) == 1
    assert "PV-FALTA" in capsys.readouterr().out
    assert L.main([str(tmp_path / "nao_existe.mq4")]) == 2
