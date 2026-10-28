#!/usr/bin/env python3
"""Linter ESTATICO para o Expert Advisor mql4/RobowinBridge.mq4 (somente biblioteca padrao).

*** NAO E UM COMPILADOR. *** Este projeto nao tem MetaEditor nem Windows; este script apenas pega erros
EVIDENTES (parenteses desbalanceados, ';' faltando, funcao sem return, chamada com aridade errada, uso de
API do MQL5 por engano, divisao por zero literal, protocolo fora de sincronia com PROTOCOLO.md/Python...).
Um arquivo que passa aqui AINDA PODE FALHAR no MetaEditor (tipos, sobrecargas, assinaturas da biblioteca
mql-zmq, escopo, conversoes implicitas, semantica...). Compile (F7) e teste em conta DEMO antes de usar.

Uso:
    python scripts/lint_mql4.py mql4/RobowinBridge.mq4 [--estrito] [--sem-revisar] [--sem-protocolo]

Saida: uma linha por achado `arquivo:linha: SEVERIDADE [codigo] mensagem`, com SEVERIDADE em
ERRO (quase certamente quebra a compilacao/o contrato), AVISO (suspeito) ou REVISAR (o linter nao consegue
decidir; um humano precisa conferir). Codigo de saida 1 se houver ERRO (ou AVISO com --estrito).
"""
from __future__ import annotations

import argparse
import bisect
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ERRO, AVISO, REVISAR = "ERRO", "AVISO", "REVISAR"
_ORDEM_SEV = {ERRO: 0, AVISO: 1, REVISAR: 2}


@dataclass(frozen=True)
class Achado:
    linha: int
    severidade: str
    codigo: str
    msg: str

    def formatar(self, arquivo: str = "") -> str:
        return f"{arquivo}:{self.linha}: {self.severidade} [{self.codigo}] {self.msg}"


# =====================================================================================================
# Tabelas MQL4 (derivadas do que o EA usa + o que se espera ver; o que nao estiver aqui vira REVISAR)
# =====================================================================================================
TIPOS = frozenset({"void", "bool", "char", "uchar", "short", "ushort", "int", "uint", "long", "ulong", "float",
                   "double", "string", "datetime", "color"})
MODIFICADORES = frozenset({"input", "extern", "static", "const", "virtual", "unsigned"})
PALAVRAS_CHAVE = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "default", "break", "continue", "return", "true", "false",
    "NULL", "new", "delete", "sizeof", "class", "struct", "enum", "union", "template", "operator", "this", "private",
    "public", "protected", "typename", "namespace", "typedef", "export", "import", "virtual", "static", "const",
    "extern", "input", "sinput", "interface", "implements"}) | TIPOS
# palavras que iniciam um novo comando: se aparecem no meio de um comando (profundidade 0), falta ';' antes
INICIO_COMANDO = frozenset({"if", "for", "while", "do", "switch", "return", "break", "continue", "else", "case",
                            "default"})

# funcoes: nome -> (aridade minima, maxima). 64 = variadica. Tudo que o EA chama e mais algumas comuns.
_FUNCS_TXT = """
AccountBalance 0 0;AccountCredit 0 0;AccountCompany 0 0;AccountCurrency 0 0;AccountEquity 0 0;
AccountFreeMargin 0 0;AccountFreeMarginCheck 3 3;AccountLeverage 0 0;AccountMargin 0 0;AccountName 0 0;
AccountNumber 0 0;AccountProfit 0 0;AccountServer 0 0;AccountStopoutLevel 0 0;AccountInfoDouble 1 1;
AccountInfoInteger 1 1;AccountInfoString 1 1;
GetLastError 0 0;ResetLastError 0 0;IsStopped 0 0;IsTesting 0 0;IsDemo 0 0;IsOptimization 0 0;
IsTradeAllowed 0 2;IsTradeContextBusy 0 0;IsExpertEnabled 0 0;IsConnected 0 0;IsDllsAllowed 0 0;
IsLibrariesAllowed 0 0;IsVisualMode 0 0;UninitializeReason 0 0;TerminalInfoInteger 1 1;TerminalInfoString 1 1;
TerminalInfoDouble 1 1;TerminalCompany 0 0;TerminalName 0 0;TerminalPath 0 0;MQLInfoInteger 1 1;MQLInfoString 1 1;
Print 1 64;Comment 1 64;Alert 1 64;PrintFormat 1 64;SendMail 2 2;SendNotification 1 1;Sleep 1 1;GetTickCount 0 0;
MathAbs 1 1;MathMax 2 2;MathMin 2 2;MathFloor 1 1;MathCeil 1 1;MathRound 1 1;MathSqrt 1 1;MathPow 2 2;
MathLog 1 1;MathExp 1 1;MathMod 2 2;MathRand 0 0;MathSrand 1 1;MathIsValidNumber 1 1;
ArrayResize 2 3;ArraySize 1 1;ArrayInitialize 2 2;ArrayFill 4 4;ArrayCopy 2 5;ArraySort 1 4;ArrayFree 1 1;
ArraySetAsSeries 2 2;ArrayGetAsSeries 1 1;ArrayMaximum 1 4;ArrayMinimum 1 4;ArrayRange 2 2;ArrayBsearch 2 5;
ArrayIsSeries 1 1;ArrayIsDynamic 1 1;
CharToString 1 1;CharToStr 1 1;DoubleToString 1 2;DoubleToStr 2 2;IntegerToString 1 3;StringToDouble 1 1;
StringToInteger 1 1;StringToTime 1 1;StringToUpper 1 1;StringToLower 1 1;StringToCharArray 2 4;
CharArrayToString 1 3;StringFormat 1 64;TimeToString 1 2;TimeToStr 1 2;NormalizeDouble 2 2;StringGetCharacter 2 2;
StringSetCharacter 3 3;StringFind 2 3;StringSubstr 2 3;StringLen 1 1;StringReplace 3 3;StringSplit 3 3;
StringTrimLeft 1 1;StringTrimRight 1 1;StringConcatenate 2 64;StringAdd 2 2;StringCompare 2 3;StringInit 2 3;
ShortToString 1 1;ColorToString 1 2;StringGetChar 2 2;StringSetChar 3 3;
TimeCurrent 0 0;TimeLocal 0 0;TimeGMT 0 0;TimeDaylightSavings 0 0;TimeGMTOffset 0 0;Day 0 0;Month 0 0;Year 0 0;
Hour 0 0;Minute 0 0;Seconds 0 0;DayOfWeek 0 0;DayOfYear 0 0;TimeDay 1 1;TimeMonth 1 1;TimeYear 1 1;TimeHour 1 1;
TimeMinute 1 1;TimeSeconds 1 1;TimeDayOfWeek 1 1;TimeToStruct 2 2;StructToTime 1 1;
MarketInfo 2 2;OrderSend 7 11;OrderClose 4 5;OrderCloseBy 3 4;OrderDelete 1 2;OrderModify 5 7;OrderSelect 2 3;
OrdersTotal 0 0;OrdersHistoryTotal 0 0;OrderType 0 0;OrderTicket 0 0;OrderLots 0 0;OrderSymbol 0 0;
OrderOpenPrice 0 0;OrderOpenTime 0 0;OrderClosePrice 0 0;OrderCloseTime 0 0;OrderStopLoss 0 0;
OrderTakeProfit 0 0;OrderComment 0 0;OrderCommission 0 0;OrderSwap 0 0;OrderProfit 0 0;OrderMagicNumber 0 0;
OrderExpiration 0 0;OrderPrint 0 0;RefreshRates 0 0;
SymbolsTotal 1 1;SymbolName 2 2;SymbolSelect 2 2;SymbolInfoInteger 2 2;SymbolInfoDouble 2 2;SymbolInfoString 2 2;
SymbolInfoTick 2 2;Symbol 0 0;Period 0 0;
EventSetTimer 1 1;EventSetMillisecondTimer 1 1;EventKillTimer 0 0;ChartSymbol 0 1;ChartID 0 0;ChartPeriod 0 1;
ChartRedraw 0 1;GlobalVariableSet 2 3;GlobalVariableGet 1 1;GlobalVariableCheck 1 1;GlobalVariableDel 1 1;
iClose 3 3;iOpen 3 3;iHigh 3 3;iLow 3 3;iTime 3 3;iVolume 3 3;iBars 2 2;iMA 7 7;iRSI 5 5;iATR 4 4;
ExpertRemove 0 0;
ErrorDescription 1 1;
"""
FUNCS: dict[str, tuple[int, int]] = {}
for _item in _FUNCS_TXT.replace("\n", "").split(";"):
    if _item.strip():
        _n, _a, _b = _item.split()
        FUNCS[_n] = (int(_a), int(_b))

FUNCS_INCLUDE = {"ErrorDescription": "stdlib.mqh"}  # funcao -> include que a declara

# manipuladores de evento aceitos como definicoes de usuario
EVENTOS = frozenset({"OnInit", "OnDeinit", "OnTick", "OnTimer", "OnChartEvent", "OnStart", "OnCalculate",
                     "OnTester", "OnTesterInit", "OnTesterDeinit", "OnTesterPass"})

# funcoes cuja semantica no MQL5 difere (modificam o argumento); o EA nao usa; REVISAR se aparecerem
FUNCS_SEMANTICA_MQL5 = frozenset({"StringToUpper", "StringToLower", "StringTrimLeft", "StringTrimRight",
                                  "StringReplace", "StringAdd", "StringInit"})

# --- recursos do MQL5 que NAO existem no MQL4 (ERRO se usados) ---------------------------------------
MQL5_FUNCS = frozenset({
    "PositionSelect", "PositionSelectByTicket", "PositionsTotal", "PositionGetInteger", "PositionGetDouble",
    "PositionGetString", "PositionGetSymbol", "PositionGetTicket", "OrderGetTicket", "OrderGetInteger",
    "OrderGetDouble", "OrderGetString", "OrderCheck", "OrderSendAsync", "OrderCalcMargin", "OrderCalcProfit",
    "HistorySelect", "HistorySelectByPosition", "HistoryDealsTotal", "HistoryDealGetTicket", "HistoryDealGetInteger",
    "HistoryDealGetDouble", "HistoryDealGetString", "HistoryOrderSelect", "HistoryOrdersTotal",
    "HistoryOrderGetTicket", "HistoryOrderGetInteger", "HistoryOrderGetDouble", "HistoryOrderGetString",
    "CopyTicks", "CopyTicksRange", "MarketBookAdd", "MarketBookGet", "TesterStatistics"})
MQL5_TIPOS = frozenset({"CTrade", "CPositionInfo", "COrderInfo", "CHistoryOrderInfo", "CDealInfo", "CSymbolInfo",
                        "CAccountInfo", "CTerminalInfo", "MqlTradeRequest", "MqlTradeResult",
                        "MqlTradeCheckResult", "MqlTradeTransaction", "MqlBookInfo"})
MQL5_CONST_RE = re.compile(r"^(TRADE_ACTION_|ORDER_TYPE_|ORDER_FILLING_|ORDER_TIME_|ORDER_STATE_|POSITION_|DEAL_|"
                           r"TRADE_RETCODE_|ACCOUNT_TRADE_|ORDER_(MAGIC|TICKET|SYMBOL|VOLUME_|PRICE_|SL$|TP$|COMMENT|"
                           r"POSITION_)|HISTORY_)")
MQL5_EVENTOS = frozenset({"OnTradeTransaction", "OnBookEvent", "OnTrade"})
ENUMS_MQL4 = frozenset({"ENUM_TIMEFRAMES", "ENUM_MA_METHOD", "ENUM_APPLIED_PRICE", "ENUM_BASE_CORNER",
                        "ENUM_OBJECT", "ENUM_LINE_STYLE", "ENUM_INIT_RETCODE", "ENUM_SYMBOL_INFO_INTEGER",
                        "ENUM_SYMBOL_INFO_DOUBLE", "ENUM_SYMBOL_INFO_STRING", "ENUM_TERMINAL_INFO_INTEGER",
                        "ENUM_TERMINAL_INFO_STRING", "ENUM_ACCOUNT_INFO_DOUBLE", "ENUM_ACCOUNT_INFO_INTEGER",
                        "ENUM_ACCOUNT_INFO_STRING"})

# --- constantes MQL4 conhecidas. Valor = None (confiavel) ou texto de nota (REVISAR: conferir no MetaEditor) --
_CONF = "nome/disponibilidade escritos de memoria; confirmar no MetaEditor"
CONSTANTES: dict[str, str | None] = {}


def _k(nomes: str, nota: str | None = None) -> None:
    for n in nomes.split():
        CONSTANTES[n] = nota


_k("OP_BUY OP_SELL OP_BUYLIMIT OP_SELLLIMIT OP_BUYSTOP OP_SELLSTOP")
_k("SELECT_BY_POS SELECT_BY_TICKET MODE_TRADES MODE_HISTORY")
_k("MODE_LOW MODE_HIGH MODE_TIME MODE_BID MODE_ASK MODE_POINT MODE_DIGITS MODE_SPREAD MODE_STOPLEVEL MODE_LOTSIZE "
   "MODE_TICKVALUE MODE_TICKSIZE MODE_SWAPLONG MODE_SWAPSHORT MODE_STARTING MODE_EXPIRATION MODE_TRADEALLOWED "
   "MODE_MINLOT MODE_LOTSTEP MODE_MAXLOT MODE_SWAPTYPE MODE_PROFITCALCMODE MODE_MARGINCALCMODE MODE_MARGININIT "
   "MODE_MARGINMAINTENANCE MODE_MARGINHEDGED MODE_MARGINREQUIRED MODE_FREEZELEVEL MODE_CLOSEBY_ALLOWED "
   "MODE_MAIN MODE_SIGNAL MODE_SMA MODE_EMA MODE_SMMA MODE_LWMA")
_k("INIT_SUCCEEDED INIT_FAILED INIT_PARAMETERS_INCORRECT INIT_AGENT_NOT_SUITABLE")
_k("REASON_PROGRAM REASON_REMOVE REASON_RECOMPILE REASON_CHARTCHANGE REASON_CHARTCLOSE REASON_PARAMETERS "
   "REASON_ACCOUNT REASON_TEMPLATE REASON_INITFAILED REASON_CLOSE")
_k("TERMINAL_BUILD TERMINAL_PATH TERMINAL_DATA_PATH TERMINAL_COMMONDATA_PATH TERMINAL_COMPANY TERMINAL_NAME "
   "TERMINAL_CONNECTED TERMINAL_DLLS_ALLOWED TERMINAL_TRADE_ALLOWED TERMINAL_LANGUAGE TERMINAL_MEMORY_PHYSICAL")
_k("SYMBOL_SELECT SYMBOL_VISIBLE SYMBOL_DIGITS SYMBOL_SPREAD SYMBOL_TRADE_MODE SYMBOL_POINT SYMBOL_BID SYMBOL_ASK "
   "SYMBOL_VOLUME_MIN SYMBOL_VOLUME_MAX SYMBOL_VOLUME_STEP SYMBOL_TRADE_CONTRACT_SIZE SYMBOL_TRADE_TICK_SIZE "
   "SYMBOL_TRADE_TICK_VALUE")
_k("SYMBOL_TRADE_EXEMODE", "ENUM_SYMBOL_TRADE_EXECUTION so documentada no MQL5; no MQL4 confirmar (usado em CmdSimbolo)")
_k("SYMBOL_PATH SYMBOL_DESCRIPTION SYMBOL_CURRENCY_BASE SYMBOL_CURRENCY_PROFIT SYMBOL_CURRENCY_MARGIN", _CONF)
_k("PERIOD_CURRENT PERIOD_M1 PERIOD_M5 PERIOD_M15 PERIOD_M30 PERIOD_H1 PERIOD_H4 PERIOD_D1 PERIOD_W1 PERIOD_MN1")
_k("PRICE_CLOSE PRICE_OPEN PRICE_HIGH PRICE_LOW PRICE_MEDIAN PRICE_TYPICAL PRICE_WEIGHTED")
_k("EMPTY_VALUE EMPTY WHOLE_ARRAY CLR_NONE clrNONE")
_k("ERR_NO_ERROR ERR_NO_RESULT ERR_COMMON_ERROR ERR_INVALID_TRADE_PARAMETERS ERR_SERVER_BUSY ERR_TRADE_NOT_ALLOWED "
   "ERR_INVALID_TICKET ERR_UNKNOWN_SYMBOL ERR_INVALID_PRICE ERR_INVALID_STOPS ERR_INVALID_TRADE_VOLUME "
   "ERR_MARKET_CLOSED ERR_TRADE_DISABLED ERR_NOT_ENOUGH_MONEY ERR_REQUOTE ERR_TRADE_CONTEXT_BUSY")
PREDEFINIDAS = frozenset({"Ask", "Bid", "Point", "Digits", "Bars", "Time", "Open", "High", "Low", "Close", "Volume",
                          "_Symbol", "_Point", "_Digits", "_Period", "_LastError", "_StopFlag", "_UninitReason"})
COR_RE = re.compile(r"^clr[A-Z][A-Za-z0-9]*$")  # cores web (clrRed, clrNONE...)
ZMQ_CONST_RE = re.compile(r"^ZMQ_[A-Z0-9_]+$")  # constantes da biblioteca mql-zmq (include externo)

# --- biblioteca mql-zmq (dingmaotu): tipos e metodos que o EA usa; assinatura escrita de memoria -------------
CLASSES_ZMQ = frozenset({"Context", "Socket", "ZmqMsg", "Poller", "PollItem"})
METODOS_ZMQ: dict[str, tuple[int, int]] = {
    "bind": (1, 1), "connect": (1, 1), "recv": (1, 2), "send": (1, 2), "setLinger": (1, 1), "getData": (0, 1),
    "size": (0, 0), "setReceiveTimeout": (1, 1), "setSendTimeout": (1, 1)}
INCLUDES_PADRAO = frozenset({"stdlib.mqh", "stderror.mqh", "winuser32.mqh", "wininet.mqh", "hanoi.mqh"})
PROPRIEDADES = frozenset({"copyright", "link", "version", "description", "icon", "strict", "stacksize",
                          "show_inputs", "library", "script_show_inputs", "script_show_confirm", "indicator_chart_window",
                          "indicator_separate_window", "indicator_buffers", "indicator_color1", "indicator_width1"})

GETTERS_ORDEM = frozenset({"OrderMagicNumber", "OrderTicket", "OrderLots", "OrderOpenPrice", "OrderClosePrice",
                           "OrderStopLoss", "OrderTakeProfit", "OrderComment", "OrderCloseTime", "OrderOpenTime",
                           "OrderSymbol", "OrderType", "OrderProfit", "OrderCommission", "OrderSwap",
                           "OrderExpiration"})
OPS_CMP = frozenset({"<", "<=", ">", ">=", "==", "!="})


# =====================================================================================================
# Tokenizador
# =====================================================================================================
@dataclass
class Tok:
    kind: str  # id num str chr color dt op pp
    text: str
    line: int


_OPS3 = ("<<=", ">>=", "...")
_OPS2 = ("==", "!=", "<=", ">=", "&&", "||", "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<", ">>",
         "::")
_OPS1 = set("+-*/%=<>!&|^~?:;,.()[]{}")
_NUM_RE = re.compile(r"0[xX][0-9a-fA-F]*|(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def tokenizar(fonte: str) -> tuple[list[Tok], list[Tok], list[Achado]]:
    """Devolve (tokens sem comentarios/diretivas, diretivas '#...' como tokens 'pp', achados lexicos)."""
    toks: list[Tok] = []
    pps: list[Tok] = []
    ach: list[Achado] = []
    i, n, linha = 0, len(fonte), 1
    ini_linha = True  # so espacos desde o inicio da linha (para detectar '#')
    while i < n:
        c = fonte[i]
        if c == "\n":
            linha += 1
            i += 1
            ini_linha = True
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if fonte.startswith("//", i):
            j = fonte.find("\n", i)
            i = n if j < 0 else j
            continue
        if fonte.startswith("/*", i):
            j = fonte.find("*/", i + 2)
            if j < 0:
                ach.append(Achado(linha, ERRO, "LEX-COMENTARIO", "comentario /* sem fechamento */"))
                return toks, pps, ach
            linha += fonte.count("\n", i, j + 2)
            i = j + 2
            continue
        if c == "#" and ini_linha:
            j, txt, l0 = i, [], linha
            while j < n:  # diretiva ate o fim da linha (continuacao com '\'), ignorando comentarios e respeitando "..."
                ch = fonte[j]
                if ch == "\n":
                    if txt and txt[-1] == "\\":
                        txt.pop()
                        linha += 1
                        j += 1
                        continue
                    break
                if fonte.startswith("//", j):
                    k = fonte.find("\n", j)
                    j = n if k < 0 else k
                    break
                if fonte.startswith("/*", j):
                    k = fonte.find("*/", j + 2)
                    if k < 0:
                        break
                    j = k + 2
                    continue
                if ch == '"':
                    k = j + 1
                    while k < n and fonte[k] not in '"\n':
                        k += 2 if fonte[k] == "\\" else 1
                    txt.append(fonte[j:k + 1])
                    j = k + 1
                    continue
                txt.append(ch)
                j += 1
            pps.append(Tok("pp", "".join(txt).strip(), l0))
            toks_marcador = len(toks)
            pps[-1].__dict__["pos"] = toks_marcador  # posicao no fluxo de tokens (para regioes #import)
            i = j
            continue
        ini_linha = False
        if c == '"':
            j = i + 1
            while j < n and fonte[j] != '"':
                if fonte[j] == "\n":
                    break
                j += 2 if fonte[j] == "\\" else 1
            if j >= n or fonte[j] != '"':
                ach.append(Achado(linha, ERRO, "LEX-STRING", "string sem aspas de fechamento"))
                i = j
                continue
            toks.append(Tok("str", fonte[i:j + 1], linha))
            i = j + 1
            continue
        if c.isalpha() or c == "_":
            m = _ID_RE.match(fonte, i)
            assert m
            txt = m.group()
            j = m.end()
            if txt in ("C", "D") and j < n and fonte[j] == "'":
                k = fonte.find("'", j + 1)
                nl = fonte.find("\n", j + 1)
                if k < 0 or (0 <= nl < k):
                    ach.append(Achado(linha, ERRO, "LEX-LITERAL", f"literal {txt}'...' sem aspas simples de fechamento"))
                    i = j + 1
                    continue
                corpo = fonte[j + 1:k]
                if txt == "C":
                    _validar_cor(corpo, linha, ach)
                    toks.append(Tok("color", fonte[i:k + 1], linha))
                else:
                    _validar_datetime(corpo, linha, ach)
                    toks.append(Tok("dt", fonte[i:k + 1], linha))
                i = k + 1
                continue
            toks.append(Tok("id", txt, linha))
            i = j
            continue
        if c == "'":
            j = i + 1
            while j < n and fonte[j] != "'" and fonte[j] != "\n":
                j += 2 if fonte[j] == "\\" else 1
            if j >= n or fonte[j] != "'":
                ach.append(Achado(linha, ERRO, "LEX-CHAR", "literal de caractere sem aspas simples de fechamento"))
                i = j
                continue
            toks.append(Tok("chr", fonte[i:j + 1], linha))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and fonte[i + 1].isdigit()):
            m = _NUM_RE.match(fonte, i)
            assert m
            txt = m.group()
            if txt.lower() == "0x":
                ach.append(Achado(linha, ERRO, "LEX-NUMERO", "numero hexadecimal sem digitos ('0x')"))
            j = m.end()
            if j < n and (fonte[j].isalnum() or fonte[j] == "_"):
                ach.append(Achado(linha, ERRO, "LEX-NUMERO", f"numero malformado: '{txt}{fonte[j]}...'"))
                while j < n and (fonte[j].isalnum() or fonte[j] == "_"):
                    j += 1
            toks.append(Tok("num", txt, linha))
            i = j
            continue
        for grupo in (_OPS3, _OPS2):
            op = fonte[i:i + len(grupo[0])]
            if op in grupo:
                toks.append(Tok("op", op, linha))
                i += len(op)
                break
        else:
            if c in _OPS1:
                toks.append(Tok("op", c, linha))
                i += 1
            else:
                ach.append(Achado(linha, ERRO, "LEX-CARACTERE", f"caractere invalido {c!r}"))
                i += 1
    return toks, pps, ach


def _validar_cor(corpo: str, linha: int, ach: list[Achado]) -> None:
    s = corpo.strip()
    if re.fullmatch(r"0[xX][0-9a-fA-F]{1,6}", s):
        return
    partes = [p.strip() for p in s.split(",")]
    if len(partes) == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in partes):
        return
    ach.append(Achado(linha, ERRO, "LEX-COR", f"literal de cor invalido C'{corpo}' (use C'r,g,b' 0..255 ou C'0xBBGGRR')"))


def _validar_datetime(corpo: str, linha: int, ach: list[Achado]) -> None:
    s = corpo.strip()
    m = re.fullmatch(r"(?:(\d{4})\.(\d{1,2})\.(\d{1,2}))?\s*(?:(\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if not s or not m:
        ach.append(Achado(linha, ERRO, "LEX-DATETIME", f"literal de data/hora invalido D'{corpo}'"))
        return
    ano, mes, dia, hh, mm, ss = m.groups()
    ok = True
    if mes is not None and not (1 <= int(mes) <= 12 and 1 <= int(dia) <= 31):
        ok = False
    if hh is not None and not (int(hh) <= 23 and int(mm) <= 59 and (ss is None or int(ss) <= 59)):
        ok = False
    if not ok:
        ach.append(Achado(linha, ERRO, "LEX-DATETIME", f"data/hora fora de faixa em D'{corpo}'"))


# =====================================================================================================
# Estruturas
# =====================================================================================================
@dataclass
class Param:
    tipo: str
    nome: str
    ref: bool = False
    array: bool = False
    padrao: bool = False


@dataclass
class Funcao:
    nome: str
    ret: str
    params: list[Param]
    linha: int
    ini: int = -1  # indice do '{' no fluxo de tokens
    fim: int = -1  # indice do '}'
    importada: str | None = None  # nome da DLL, se vier de #import

    @property
    def aridade(self) -> tuple[int, int]:
        return sum(1 for p in self.params if not p.padrao), len(self.params)


def _fim_args(toks: list[Tok], ab: int) -> int:
    """Indice do ')' que fecha o '(' em `ab` (ou -1)."""
    d = 0
    for k in range(ab, len(toks)):
        t = toks[k].text
        if toks[k].kind == "op":
            if t in "([{":
                d += 1
            elif t in ")]}":
                d -= 1
                if d == 0:
                    return k
    return -1


def _dividir_args(toks: list[Tok], ab: int, fe: int) -> list[list[Tok]]:
    """Argumentos entre o '(' (ab) e o ')' (fe), separados por ',' no nivel 1."""
    args: list[list[Tok]] = []
    atual: list[Tok] = []
    d = 0
    for k in range(ab + 1, fe):
        t = toks[k]
        if t.kind == "op" and t.text in "([{":
            d += 1
        elif t.kind == "op" and t.text in ")]}":
            d -= 1
        if t.kind == "op" and t.text == "," and d == 0:
            args.append(atual)
            atual = []
        else:
            atual.append(t)
    if atual or args:
        args.append(atual)
    return args


def _parse_params(toks: list[Tok]) -> tuple[list[Param], bool]:
    """Lista de parametros a partir dos tokens entre parenteses. Devolve (params, ok)."""
    if not toks or (len(toks) == 1 and toks[0].text == "void"):
        return [], True
    fake = [Tok("op", "(", 0), *toks, Tok("op", ")", 0)]
    out: list[Param] = []
    ok = True
    for a in _dividir_args(fake, 0, len(fake) - 1):
        padrao = any(t.text == "=" for t in a)
        if padrao:
            a = a[:[t.text for t in a].index("=")]
        a = [t for t in a if t.text not in MODIFICADORES]
        ref = any(t.text == "&" for t in a)
        arr = any(t.text == "[" for t in a)
        ids = [t for t in a if t.kind == "id"]
        if len(ids) < 1 or (len(ids) == 1 and ids[0].text not in TIPOS and not ref and not arr and False):
            ok = False
            continue
        if len(ids) == 1:  # parametro sem nome (prototipo/import): so o tipo
            out.append(Param(ids[0].text, "", ref, arr, padrao))
        else:
            out.append(Param(ids[0].text, ids[-1].text, ref, arr, padrao))
    return out, ok


# =====================================================================================================
# Analisador
# =====================================================================================================
class Analise:
    def __init__(self, fonte: str) -> None:
        self.fonte = fonte
        self.toks, self.pps, self.achados = tokenizar(fonte)
        self.par: dict[int, int] = {}
        self.defines: dict[str, str] = {}
        self.includes: list[tuple[str, int]] = []
        self.funcs: dict[str, Funcao] = {}
        self.imports: list[Funcao] = []
        self.globais: dict[str, tuple[str, bool]] = {}
        self.tipos_usuario: set[str] = set()  # struct/class/enum declarados
        self.enum_membros: set[str] = set()
        self.arvores: dict[str, list] = {}
        self._import_eventos: list[tuple[int, str | None]] = []  # (pos_no_fluxo, dll|None p/ fechar)
        self.classes_zmq_ok = False

    def add(self, linha: int, sev: str, cod: str, msg: str) -> None:
        self.achados.append(Achado(linha, sev, cod, msg))

    # ---------------------------------------------------------------- balanceamento e diretivas
    def balanco(self) -> None:
        pilha: list[int] = []
        par_de = {")": "(", "]": "[", "}": "{"}
        for k, t in enumerate(self.toks):
            if t.kind != "op":
                continue
            if t.text in "([{":
                pilha.append(k)
            elif t.text in ")]}":
                if not pilha:
                    self.add(t.line, ERRO, "BAL-SOBRA", f"'{t.text}' sem abertura correspondente")
                    continue
                a = pilha[-1]
                if self.toks[a].text != par_de[t.text]:
                    self.add(t.line, ERRO, "BAL-CRUZADO",
                             f"'{t.text}' fecha '{self.toks[a].text}' aberto na linha {self.toks[a].line}")
                    # recuperacao: tenta casar com algum abaixo
                    for m in range(len(pilha) - 1, -1, -1):
                        if self.toks[pilha[m]].text == par_de[t.text]:
                            self.par[pilha[m]] = k
                            del pilha[m:]
                            break
                    continue
                pilha.pop()
                self.par[a] = k
        for a in pilha:
            self.add(self.toks[a].line, ERRO, "BAL-ABERTO", f"'{self.toks[a].text}' aberto aqui e nunca fechado")

    def diretivas(self) -> None:
        aberto: tuple[str, int] | None = None
        pilha_if: list[tuple[str, int]] = []
        for pp in self.pps:
            pos = pp.__dict__.get("pos", 0)
            m = re.match(r"#\s*(\w+)\s*(.*)$", pp.text, re.S)
            if not m:
                self.add(pp.line, ERRO, "PP-INVALIDA", f"diretiva invalida: {pp.text!r}")
                continue
            d, resto = m.group(1), m.group(2).strip()
            if d == "property":
                nome = resto.split()[0] if resto.split() else ""
                if not nome:
                    self.add(pp.line, ERRO, "PP-PROPERTY", "#property sem nome")
                elif nome not in PROPRIEDADES:
                    self.add(pp.line, REVISAR, "PP-PROPERTY", f"#property '{nome}' fora da lista conhecida do linter")
                if nome == "version":
                    v = re.search(r'"([^"]*)"', resto)
                    if not v or not re.fullmatch(r"\d{1,3}\.\d{1,3}", v.group(1)):
                        self.add(pp.line, AVISO, "PP-VERSION",
                                 "#property version deve ser \"NNN.NNN\" (ate 3 digitos por parte)")
            elif d == "include":
                mi = re.fullmatch(r'<([^<>]+)>|"([^"]+)"', resto)
                if not mi:
                    self.add(pp.line, ERRO, "PP-INCLUDE", f"#include malformado: {resto!r} (use <arq> ou \"arq\")")
                    continue
                caminho = (mi.group(1) or mi.group(2)).replace("\\", "/")
                self.includes.append((caminho, pp.line))
                if re.search(r"(^|/)Trade\.mqh$|Trade/", caminho, re.I):
                    self.add(pp.line, ERRO, "MQL5-INCLUDE", f"#include <{caminho}> e da biblioteca do MQL5 (CTrade); nao existe no MQL4")
                elif caminho.lower() not in INCLUDES_PADRAO:
                    self.add(pp.line, REVISAR, "PP-INCLUDE-EXTERNO",
                             f"#include <{caminho}> nao e do MetaTrader: precisa estar instalado em MQL4/Include")
                if caminho.lower().startswith("zmq/"):
                    self.classes_zmq_ok = True
            elif d == "define":
                md = re.match(r"(\w+)(\(.*?\))?\s*(.*)$", resto, re.S)
                if not md:
                    self.add(pp.line, ERRO, "PP-DEFINE", "#define sem nome")
                else:
                    self.defines[md.group(1)] = md.group(3)
                    if md.group(2):
                        self.defines[md.group(1) + "()"] = ""
            elif d == "import":
                if resto:
                    mi = re.fullmatch(r'"([^"]+)"', resto)
                    if not mi:
                        self.add(pp.line, ERRO, "PP-IMPORT", f"#import malformado: {resto!r}")
                    if aberto:
                        # um #import "x" dentro de outro bloco abre um novo (MQL permite encadear varios blocos);
                        # o anterior e fechado implicitamente
                        pass
                    aberto = (mi.group(1) if mi else resto, pp.line)
                    self._import_eventos.append((pos, aberto[0]))
                else:
                    if not aberto:
                        self.add(pp.line, ERRO, "PP-IMPORT", "#import de fechamento sem #import \"dll\" aberto")
                    aberto = None
                    self._import_eventos.append((pos, None))
            elif d in ("ifdef", "ifndef", "if"):
                pilha_if.append((d, pp.line))
            elif d == "endif":
                if not pilha_if:
                    self.add(pp.line, ERRO, "PP-ENDIF", "#endif sem #if/#ifdef/#ifndef")
                else:
                    pilha_if.pop()
            elif d in ("else", "elif"):
                if not pilha_if:
                    self.add(pp.line, ERRO, "PP-ELSE", f"#{d} sem #if")
            elif d in ("undef", "pragma", "error", "warning"):
                pass
            else:
                self.add(pp.line, ERRO, "PP-DESCONHECIDA", f"diretiva desconhecida #{d}")
        if aberto:
            self.add(aberto[1], ERRO, "PP-IMPORT", f"#import \"{aberto[0]}\" nunca fechado com #import")
        for d, ln in pilha_if:
            self.add(ln, ERRO, "PP-IF", f"#{d} sem #endif")

    def _import_em(self, idx: int) -> str | None:
        """DLL do bloco #import em que o token `idx` esta (ou None)."""
        pos = [p for p, _ in self._import_eventos]
        k = bisect.bisect_right(pos, idx) - 1
        return self._import_eventos[k][1] if k >= 0 else None

    # ---------------------------------------------------------------- nivel superior
    def estrutura(self) -> None:
        toks, n = self.toks, len(self.toks)
        i = 0
        while i < n:
            t = toks[i]
            if t.text == ";" and t.kind == "op":
                self.add(t.line, AVISO, "PV-SOBRANDO", "';' solto no nivel superior (declaracao vazia)")
                i += 1
                continue
            j, d = i, 0
            fim_tipo = ""  # ';' | '{' | 'falta' | '}'
            while j < n:
                tk = toks[j]
                if tk.kind == "op":
                    if tk.text in "([":
                        d += 1
                    elif tk.text in ")]":
                        d -= 1
                    elif d == 0 and tk.text == ";":
                        fim_tipo = ";"
                        break
                    elif d == 0 and tk.text == "{":
                        fim_tipo = "{"
                        break
                    elif d == 0 and tk.text == "}":
                        fim_tipo = "}"
                        break
                if d == 0 and j > i and self._falta_pv(toks, i, j):
                    fim_tipo = "falta"
                    break
                j += 1
            head = toks[i:j]
            if fim_tipo == "}":
                self.add(toks[j].line, ERRO, "BAL-SOBRA", "'}' inesperado no nivel superior")
                i = j + 1
                continue
            if fim_tipo == "falta":
                self.add(toks[j - 1].line, ERRO, "PV-FALTA", f"';' ausente apos '{toks[j - 1].text}' (declaracao seguinte comeca na linha {toks[j].line})")
                self._registrar_decl(head, i)
                i = j
                continue
            if fim_tipo == "":
                if head:
                    self.add(head[-1].line, ERRO, "PV-FALTA", f"';' ausente no fim do arquivo apos '{head[-1].text}'")
                    self._registrar_decl(head, i)
                break
            if fim_tipo == ";":
                self._declaracao_global(head, i)
                i = j + 1
                continue
            # '{'
            first = head[0].text if head else ""
            if first in ("struct", "class", "enum", "union", "namespace") or (len(head) > 1 and head[1].text in ("struct", "class", "enum")):
                fe = self.par.get(j, -1)
                if fe < 0:
                    break
                nome = next((h.text for h in head[1:] if h.kind == "id"), "")
                if nome:
                    self.tipos_usuario.add(nome)
                if first == "enum":
                    self._membros_enum(j, fe)
                i = fe + 1
                if i >= n or toks[i].text != ";":
                    ln = toks[fe].line
                    self.add(ln, ERRO, "PV-FALTA", f"';' ausente apos '}}' de {first} {nome}")
                else:
                    i += 1
                continue
            self._definicao_funcao(head, i, j)
            fe = self.par.get(j, -1)
            if fe < 0:
                break
            i = fe + 1
        return

    @staticmethod
    def _falta_pv(toks: list[Tok], i: int, j: int) -> bool:
        """No nivel superior, o token j (>i, profundidade 0) comeca uma nova declaracao sem ';' antes?"""
        t, p = toks[j], toks[j - 1]
        if t.kind != "id":
            return False
        if p.text in MODIFICADORES or p.text in ("operator",) or (p.kind == "op" and p.text not in (")", "]")):
            return False
        if t.text in MODIFICADORES or (t.text in TIPOS and j + 1 < len(toks) and toks[j + 1].kind == "id"):
            if p.text in TIPOS or p.text in ("struct", "class", "enum"):
                return False
            return p.kind in ("num", "str", "chr", "id", "color", "dt") or p.text in (")", "]")
        return False

    def _membros_enum(self, ab: int, fe: int) -> None:
        esperando = True
        for k in range(ab + 1, fe):
            t = self.toks[k]
            if esperando and t.kind == "id":
                self.enum_membros.add(t.text)
                esperando = False
            elif t.text == ",":
                esperando = True

    def _declaracao_global(self, head: list[Tok], idx0: int) -> None:
        dll = self._import_em(idx0)
        if dll is not None:
            self._import_funcao(head, dll)
            return
        self._registrar_decl(head, idx0)

    def _registrar_decl(self, head: list[Tok], idx0: int) -> None:
        for nome, info in coletar_decls(head, self).items():
            self.globais[nome] = info

    def _import_funcao(self, head: list[Tok], dll: str) -> None:
        if not any(t.text == "(" for t in head):
            return
        k = next(i for i, t in enumerate(head) if t.text == "(")
        if k < 2 or head[k - 1].kind != "id":
            self.add(head[0].line, ERRO, "IMPORT-INVALIDO", "declaracao de funcao importada malformada")
            return
        fe = _fim_args(head, k)
        if fe < 0:
            return
        params, ok = _parse_params(head[k + 1:fe])
        if not ok:
            self.add(head[0].line, ERRO, "IMPORT-PARAM", "parametro malformado em funcao importada")
        f = Funcao(head[k - 1].text, " ".join(t.text for t in head[:k - 1]), params, head[0].line, importada=dll)
        self.imports.append(f)

    def _definicao_funcao(self, head: list[Tok], i0: int, ab: int) -> None:
        if not head or head[-1].text not in (")", "const"):
            ln = head[-1].line if head else self.toks[ab].line
            self.add(ln, ERRO, "FUNC-INVALIDA", "'{' no nivel superior que nao abre funcao/struct/enum")
            return
        k = next((x for x, t in enumerate(head) if t.text == "("), -1)
        if k < 2 or head[k - 1].kind != "id":
            self.add(head[0].line, ERRO, "FUNC-INVALIDA", "definicao de funcao sem 'tipo nome(...)'")
            return
        fe = _fim_args(head, k)
        params, ok = _parse_params(head[k + 1:fe])
        if not ok:
            self.add(head[0].line, ERRO, "FUNC-PARAM", f"parametro malformado em {head[k - 1].text}")
        ret = " ".join(t.text for t in head[:k - 1] if t.text not in MODIFICADORES)
        f = Funcao(head[k - 1].text, ret, params, head[k - 1].line, ini=ab, fim=self.par.get(ab, -1))
        if f.nome in self.funcs:
            self.add(f.linha, ERRO, "FUNC-DUPLICADA", f"funcao '{f.nome}' ja definida na linha {self.funcs[f.nome].linha}")
        self.funcs[f.nome] = f

    # ---------------------------------------------------------------- corpos (statements)
    def corpos(self) -> None:
        for f in self.funcs.values():
            if f.fim < 0:
                continue
            c = _Corpo(self, f)
            nodes = c.bloco(f.ini + 1, f.fim)
            self.arvores[f.nome] = nodes
            self._retornos(f, nodes, c)

    def _retornos(self, f: Funcao, nodes: list, c: _Corpo) -> None:
        void = f.ret.strip() == "void"
        for ln, tem in c.returns:
            if void and tem:
                self.add(ln, ERRO, "RET-VOID", f"'{f.nome}' e void mas retorna valor")
            if not void and not tem:
                self.add(ln, ERRO, "RET-SEM-VALOR", f"'{f.nome}' retorna {f.ret} mas este return nao tem valor")
        if void:
            return
        if not _retorna(("bloco", nodes)):
            fecha = self.toks[f.fim].line
            if not c.returns:
                self.add(f.linha, ERRO, "RET-AUSENTE", f"'{f.nome}' ({f.ret}) nao tem nenhum return")
            else:
                self.add(fecha, AVISO, "RET-CAMINHOS",
                         f"'{f.nome}' ({f.ret}): nem todos os caminhos terminam em return (heuristica; MetaEditor avisa)")


def _retorna(no: tuple) -> bool:
    k = no[0]
    if k == "ret":
        return True
    if k == "bloco":
        return any(_retorna(x) for x in no[1])
    if k == "if":
        return no[2] is not None and _retorna(no[1]) and _retorna(no[2])
    if k == "loop":
        return bool(no[1]) and not _tem_break(no[2])
    if k == "switch":
        return bool(no[1]) and not any(_tem_break(x) for x in no[2]) and bool(no[2]) and _retorna(no[2][-1])
    return False


def _tem_break(no: tuple) -> bool:
    """break que pertence ao laco/switch cujo corpo e `no` (nao conta break de lacos/switch aninhados)."""
    k = no[0]
    if k == "brk":
        return True
    if k == "bloco":
        return any(_tem_break(x) for x in no[1])
    if k == "if":
        return _tem_break(no[1]) or (no[2] is not None and _tem_break(no[2]))
    return False


class _Corpo:
    """Parser de comandos de um corpo de funcao: acha ';' faltando/sobrando e monta arvore p/ analise de return."""

    def __init__(self, an: Analise, f: Funcao) -> None:
        self.an, self.toks, self.f = an, an.toks, f
        self.p = 0
        self.fim = 0
        self.returns: list[tuple[int, bool]] = []

    def bloco(self, ini: int, fim: int) -> list:
        sp, sf = self.p, self.fim
        self.p, self.fim = ini, fim
        nodes = []
        while self.p < self.fim:
            antes = self.p
            nodes.append(self.stmt())
            if self.p == antes:
                self.p += 1
        self.p, self.fim = sp, sf
        return nodes

    def _tok(self, k: int) -> Tok:
        return self.toks[k]

    def _paren(self, kw: str) -> tuple[int, int] | None:
        """No token '(' (apos if/for/while/switch); devolve (abre, fecha) e posiciona depois do ')'."""
        if self.p >= self.fim or self.toks[self.p].text != "(":
            ln = self.toks[min(self.p, len(self.toks) - 1)].line
            self.an.add(ln, ERRO, "SINTAXE", f"'(' esperado apos '{kw}'")
            return None
        ab = self.p
        fe = self.an.par.get(ab, -1)
        if fe < 0 or fe >= self.fim:
            return None
        self.p = fe + 1
        return ab, fe

    def _corpo_cmd(self, kw: str, linha: int) -> tuple:
        if self.p < self.fim and self.toks[self.p].text == ";" and self.toks[self.p].kind == "op":
            self.an.add(self.toks[self.p].line, AVISO, "PV-CORPO-VAZIO", f"';' logo apos '{kw}(...)': corpo vazio (provavel engano)")
            self.p += 1
            return ("vazio",)
        if self.p >= self.fim:
            return ("vazio",)
        return self.stmt()

    def stmt(self) -> tuple:
        t = self.toks[self.p]
        txt = t.text
        if t.kind == "op" and txt == "{":
            fe = self.an.par.get(self.p, -1)
            if fe < 0 or fe > self.fim:
                self.p += 1
                return ("vazio",)
            nodes = self.bloco(self.p + 1, fe)
            self.p = fe + 1
            return ("bloco", nodes)
        if t.kind == "op" and txt == ";":
            self.an.add(t.line, AVISO, "PV-SOBRANDO", "';' sobrando (comando vazio)")
            self.p += 1
            return ("vazio",)
        if t.kind == "id":
            if txt == "if":
                self.p += 1
                if not self._paren("if"):
                    return ("vazio",)
                entao = self._corpo_cmd("if", t.line)
                senao = None
                if self.p < self.fim and self.toks[self.p].text == "else":
                    self.p += 1
                    senao = self._corpo_cmd("else", t.line)
                return ("if", entao, senao)
            if txt in ("while", "for"):
                self.p += 1
                pr = self._paren(txt)
                if not pr:
                    return ("vazio",)
                infinito = self._infinito(txt, pr)
                corpo = self._corpo_cmd(txt, t.line)
                return ("loop", infinito, corpo)
            if txt == "do":
                self.p += 1
                corpo = self.stmt()
                if self.p < self.fim and self.toks[self.p].text == "while":
                    self.p += 1
                    pr = self._paren("while")
                    inf = bool(pr) and self._infinito("while", pr)
                    if self.p < self.fim and self.toks[self.p].text == ";":
                        self.p += 1
                    else:
                        self.an.add(self.toks[max(self.p - 1, 0)].line, ERRO, "PV-FALTA", "';' ausente apos do { } while(...)")
                    return ("loop", inf, corpo)
                self.an.add(t.line, ERRO, "SINTAXE", "'do' sem 'while' correspondente")
                return ("loop", False, corpo)
            if txt == "switch":
                self.p += 1
                if not self._paren("switch"):
                    return ("vazio",)
                if self.p >= self.fim or self.toks[self.p].text != "{":
                    self.an.add(t.line, ERRO, "SINTAXE", "'{' esperado apos switch(...)")
                    return ("vazio",)
                fe = self.an.par.get(self.p, -1)
                if fe < 0:
                    return ("vazio",)
                filhos, tem_default = self._switch(self.p + 1, fe)
                self.p = fe + 1
                return ("switch", tem_default, filhos)
            if txt == "return":
                self.p += 1
                ini = self.p
                self._simples(ini, "return")
                self.returns.append((t.line, self.p - 1 > ini))
                return ("ret",)
            if txt in ("break", "continue"):
                self.p += 1
                if self.p < self.fim and self.toks[self.p].text == ";":
                    self.p += 1
                else:
                    self.an.add(t.line, ERRO, "PV-FALTA", f"';' ausente apos '{txt}'")
                return ("brk",) if txt == "break" else ("cont",)
            if txt == "else":
                self.an.add(t.line, ERRO, "SINTAXE", "'else' sem 'if' correspondente")
                self.p += 1
                return ("vazio",)
            if txt in ("case", "default"):
                self.an.add(t.line, ERRO, "SINTAXE", f"'{txt}' fora de switch")
                self.p += 1
                return ("vazio",)
        ini = self.p
        self._simples(ini, "")
        return ("simples", ini, self.p)

    def _infinito(self, kw: str, pr: tuple[int, int]) -> bool:
        ab, fe = pr
        interior = self.toks[ab + 1:fe]
        if kw == "while":
            return len(interior) == 1 and interior[0].text in ("true", "1")
        partes = " ".join(t.text for t in interior).split(";")
        return len(partes) == 3 and partes[1].strip() == ""

    def _switch(self, ini: int, fim: int) -> tuple[list, bool]:
        sp, sf = self.p, self.fim
        self.p, self.fim = ini, fim
        filhos, tem_default = [], False
        while self.p < self.fim:
            t = self.toks[self.p]
            if t.kind == "id" and t.text in ("case", "default"):
                if t.text == "default":
                    tem_default = True
                d = 0
                self.p += 1
                while self.p < self.fim:
                    x = self.toks[self.p]
                    if x.kind == "op" and x.text in "([":
                        d += 1
                    elif x.kind == "op" and x.text in ")]":
                        d -= 1
                    elif d == 0 and x.text == ":":
                        break
                    self.p += 1
                self.p += 1
                continue
            antes = self.p
            filhos.append(self.stmt())
            if self.p == antes:
                self.p += 1
        self.p, self.fim = sp, sf
        return filhos, tem_default

    def _simples(self, i0: int, kw: str) -> None:
        """Consome ate o ';' do comando (nivel 0). Registra ';' ausente. Deixa self.p depois do ';'."""
        d = 0
        toks = self.toks
        fechou_chamada = False
        pilha_ab: list[int] = []
        k = i0
        while k < self.fim:
            t = toks[k]
            ant = toks[k - 1] if k > i0 else None
            if t.kind == "op":
                if t.text in "([":
                    d += 1
                    pilha_ab.append(k)
                elif t.text in ")]":
                    d -= 1
                    ab = pilha_ab.pop() if pilha_ab else -1
                    eh_cast = ab >= 0 and k - ab == 2 and toks[ab + 1].text in TIPOS
                    fechou_chamada = bool(d == 0 and t.text == ")" and ab >= 0 and not eh_cast)
                    k += 1
                    continue
                elif d == 0 and t.text == ";":
                    self.p = k + 1
                    return
                elif d == 0 and t.text == "{":
                    if ant is not None and ant.text == "=":  # inicializador de array: {1,2,3}
                        fe = self.an.par.get(k, -1)
                        k = (fe if fe > 0 else k) + 1
                        continue
                    self._sem_pv(ant, i0, k)
                    return
                elif d == 0 and t.text == "}":
                    self._sem_pv(ant, i0, k)
                    return
            elif d == 0 and ant is not None:
                if t.kind == "id" and t.text in INICIO_COMANDO:
                    self._sem_pv(ant, i0, k)
                    return
                if t.kind == "id" and t.text in TIPOS and k + 1 < self.fim and toks[k + 1].kind == "id" \
                        and ant.text not in MODIFICADORES and ant.text not in TIPOS:
                    self._sem_pv(ant, i0, k)
                    return
                if t.line > ant.line and t.kind in ("id", "num", "str", "chr", "color", "dt") \
                        and ant.text not in MODIFICADORES and ant.text not in TIPOS \
                        and (ant.kind in ("num", "str", "chr", "id", "color", "dt") or ant.text in ("]", "++", "--")
                             or (ant.text == ")" and fechou_chamada)) \
                        and ant.text not in ("return", "else", "new", "delete", "case", "do") \
                        and not (ant.kind == "id" and ant.text in PALAVRAS_CHAVE - TIPOS - {"true", "false", "NULL"}):
                    self._sem_pv(ant, i0, k)
                    return
            if ant is not None and ant.kind in ("num", "str") and t.kind in ("id", "num") and ant.line == t.line:
                self.an.add(t.line, ERRO, "SINTAXE", f"operador ou ';' esperado entre '{ant.text}' e '{t.text}'")
            if t.kind != "op":
                fechou_chamada = False if t.line else fechou_chamada
            k += 1
        # fim do corpo sem ';'
        self._sem_pv(toks[k - 1] if k > i0 else None, i0, k)

    def _sem_pv(self, ant: Tok | None, i0: int, k: int) -> None:
        ln = (ant or self.toks[min(k, len(self.toks) - 1)]).line
        pal = ant.text if ant else "?"
        self.an.add(ln, ERRO, "PV-FALTA", f"';' ausente apos '{pal}'")
        self.p = k


# =====================================================================================================
# Declaracoes e tipos
# =====================================================================================================
def coletar_decls(toks: list[Tok], an: Analise) -> dict[str, tuple[str, bool]]:
    """Nomes declarados num trecho de tokens: {nome: (tipo, e_array)}. Reconhece `tipo nome` e `, nome` seguintes."""
    out: dict[str, tuple[str, bool]] = {}
    n = len(toks)
    k = 0
    while k < n:
        t = toks[k]
        if (t.kind == "id" and t.text not in PALAVRAS_CHAVE - TIPOS and t.text not in MODIFICADORES
                and k + 2 < n + 1 and k + 1 < n and toks[k + 1].kind == "id"
                and toks[k + 1].text not in PALAVRAS_CHAVE
                and (k + 2 >= n or toks[k + 2].text in ("=", ";", ",", "[", "(", ")"))
                and (k == 0 or toks[k - 1].text not in (".", "return", "new", "case", "else"))):
            tipo, nome = t.text, toks[k + 1].text
            arr = k + 2 < n and toks[k + 2].text == "["
            out[nome] = (tipo, arr)
            # declaradores adicionais: tipo a = 1, b[3], c;  (mesmo nivel de parenteses)
            d, m = 0, k + 2
            while m < n:
                x = toks[m]
                if x.kind == "op" and x.text in "([{":
                    d += 1
                elif x.kind == "op" and x.text in ")]}":
                    d -= 1
                    if d < 0:
                        break
                elif x.text == ";" and d == 0:
                    break
                elif x.text == "," and d == 0 and m + 1 < n and toks[m + 1].kind == "id" \
                        and (m + 2 >= n or toks[m + 2].text in ("=", ",", ";", "[", ")")):
                    out[toks[m + 1].text] = (tipo, m + 2 < n and toks[m + 2].text == "[")
                m += 1
            k += 2
            continue
        k += 1
    return out


def inferir_tipo(arg: list[Tok], locais: dict[str, tuple[str, bool]]) -> str | None:
    """Tipo grosseiro de um argumento: string, int, double, bool, array:<tipo> ou o tipo declarado; None = nao sei."""
    if not arg:
        return None
    if len(arg) == 1:
        t = arg[0]
        if t.kind == "str":
            return "string"
        if t.kind == "num":
            return "double" if re.search(r"[.eE]", t.text) and not t.text.lower().startswith("0x") else "int"
        if t.kind == "chr":
            return "int"
        if t.kind == "color":
            return "color"
        if t.kind == "dt":
            return "datetime"
        if t.text in ("true", "false"):
            return "bool"
        if t.kind == "id" and t.text in locais:
            tp, arr = locais[t.text]
            return f"array:{tp}" if arr else tp
        return None
    if len(arg) >= 3 and arg[0].text == "(" and arg[1].text in TIPOS and arg[2].text == ")":
        return arg[1].text
    if any(t.kind == "str" for t in arg) and all(t.kind == "str" or t.text == "+" for t in arg):
        return "string"
    return None


_NUMERICOS = {"bool", "char", "uchar", "short", "ushort", "int", "uint", "long", "ulong", "float", "double", "datetime",
              "color"}


def _tipos_compat(param: Param, arg: str | None) -> str | None:
    """None se compativel/indeterminado; senao texto do problema. ERRO so para string<->numero e array."""
    if arg is None:
        return None
    p = param.tipo
    if param.array:
        return None if arg.startswith("array:") else f"parametro '{param.nome or p}' e array, argumento e {arg}"
    if arg.startswith("array:"):
        return f"argumento e array ({arg}) mas o parametro '{param.nome or p}' e escalar {p}"
    if p == "string" and arg in _NUMERICOS:
        return f"parametro string recebe valor numerico ({arg})"
    if p in _NUMERICOS and arg == "string":
        return f"parametro {p} recebe string"
    return None


# =====================================================================================================
# Verificacoes semanticas nas funcoes
# =====================================================================================================
def verificar_funcoes(an: Analise) -> None:
    toks = an.toks
    mql5_ids_vistos: set[str] = set()
    long_linhas: list[int] = []
    constantes_conferir: dict[str, int] = {}
    desconhecidos: dict[str, int] = {}
    funcs_desc: dict[str, int] = {}
    metodos_zmq: dict[str, int] = {}
    for f in an.funcs.values():
        if f.fim < 0:
            continue
        corpo = toks[f.ini + 1:f.fim]
        base = f.ini + 1
        locais: dict[str, tuple[str, bool]] = {p.nome: (p.tipo, p.array) for p in f.params if p.nome}
        locais.update(coletar_decls(corpo, an))
        visiveis = {**an.globais, **locais}
        primeiro_select = None
        primeiro_getter: Tok | None = None
        nomes_decl = {nome for nome in locais}
        k = 0
        while k < len(corpo):
            t = corpo[k]
            gi = base + k
            prev = corpo[k - 1] if k > 0 else None
            nxt = corpo[k + 1] if k + 1 < len(corpo) else None
            if t.kind == "op" and t.text in ("/", "/=", "%", "%=") and nxt is not None and nxt.kind == "num":
                if _valor_zero(nxt.text):
                    an.add(t.line, ERRO, "DIV-ZERO", f"divisao por zero literal ('{t.text} {nxt.text}')")
            if t.kind != "id":
                k += 1
                continue
            nome = t.text
            if nome in ("long", "ulong") and t.line not in long_linhas:
                long_linhas.append(t.line)
            if nome.startswith("ENUM_") and nome not in ENUMS_MQL4:
                an.add(t.line, REVISAR, "ENUM", f"'{nome}' parece enum do MQL5; confirmar que existe no MQL4")
            if nome in MQL5_TIPOS:
                an.add(t.line, ERRO, "MQL5-TIPO", f"'{nome}' e do MQL5 (nao existe no MQL4)")
            if prev is not None and prev.text in (".", "::"):
                # membro de objeto
                if nxt is not None and nxt.text == "(" and prev.text == ".":
                    fe = _fim_args(corpo, k + 1)
                    na = len(_dividir_args(corpo, k + 1, fe)) if fe > 0 else 0
                    if nome in METODOS_ZMQ:
                        lo, hi = METODOS_ZMQ[nome]
                        metodos_zmq.setdefault(nome, t.line)
                        if not lo <= na <= hi:
                            an.add(t.line, ERRO, "ARIDADE-ZMQ", f"metodo .{nome}() com {na} argumento(s); esperado {lo}..{hi}")
                    else:
                        metodos_zmq.setdefault(nome, t.line)
                        an.add(t.line, REVISAR, "METODO-DESCONHECIDO", f"metodo .{nome}() de objeto: linter nao conhece a assinatura")
                k += 1
                continue
            if nxt is not None and nxt.text == "(" and nome in PALAVRAS_CHAVE:
                k += 1  # if( while( for( return( ... nao sao chamadas
                continue
            if nxt is not None and nxt.text == "(":
                # declaracao com construtor (`ZmqMsg resposta(resp)`): o nome ja e local; nao e chamada
                if prev is not None and prev.kind == "id" and prev.text not in PALAVRAS_CHAVE - TIPOS and nome in locais:
                    k += 1
                    continue
                if prev is not None and prev.kind == "id" and prev.text in TIPOS and nome in locais:
                    k += 1
                    continue
                _checar_chamada(an, f, corpo, k, base, locais, visiveis)
                if nome == "OrderSelect" and primeiro_select is None:
                    primeiro_select = gi
                if nome in GETTERS_ORDEM and primeiro_select is None and primeiro_getter is None:
                    primeiro_getter = t
                if nome in MQL5_FUNCS:
                    an.add(t.line, ERRO, "MQL5-FUNCAO", f"'{nome}()' e do MQL5; no MQL4 use OrderSelect/OrdersTotal/OrderSend...")
                elif nome in MQL5_EVENTOS:
                    an.add(t.line, AVISO, "MQL5-EVENTO", f"'{nome}' e evento do MQL5; nunca sera chamado no MQL4")
                elif nome in FUNCS_SEMANTICA_MQL5:
                    an.add(t.line, REVISAR, "STRING-SEMANTICA", f"'{nome}()' modifica o argumento/retorna bool no MQL5; conferir semantica no MQL4")
                elif nome not in FUNCS and nome not in an.funcs and not any(i.nome == nome for i in an.imports) \
                        and nome not in an.defines and nome not in an.defines.keys() and (nome + "()") not in an.defines \
                        and nome not in TIPOS and nome not in an.tipos_usuario and nome not in EVENTOS:
                    funcs_desc.setdefault(nome, t.line)
                k += 1
                continue
            # identificador que nao e chamada
            if nome in PALAVRAS_CHAVE or nome in visiveis or nome in an.defines or nome in an.enum_membros \
                    or nome in nomes_decl or nome in PREDEFINIDAS:
                k += 1
                continue
            if nome in an.funcs or any(i.nome == nome for i in an.imports) or nome in FUNCS or nome in EVENTOS:
                k += 1
                continue
            if nxt is not None and nxt.kind == "id" and nxt.text not in PALAVRAS_CHAVE:
                # `Tipo variavel`: tipo de classe
                if nome in CLASSES_ZMQ:
                    if not an.classes_zmq_ok:
                        an.add(t.line, ERRO, "ZMQ-SEM-INCLUDE", f"tipo '{nome}' da biblioteca mql-zmq sem #include <Zmq/Zmq.mqh>")
                elif nome not in an.tipos_usuario:
                    desconhecidos.setdefault(nome, t.line)
                k += 1
                continue
            if nome in CLASSES_ZMQ or nome in an.tipos_usuario:
                k += 1
                continue
            if nome in CONSTANTES:
                if CONSTANTES[nome]:
                    constantes_conferir.setdefault(nome, t.line)
            elif COR_RE.match(nome) or ZMQ_CONST_RE.match(nome):
                pass
            elif MQL5_CONST_RE.match(nome):
                an.add(t.line, ERRO, "MQL5-CONSTANTE", f"constante '{nome}' e do MQL5 (nao existe no MQL4)")
                mql5_ids_vistos.add(nome)
            else:
                desconhecidos.setdefault(nome, t.line)
            k += 1
        if primeiro_getter is not None:
            an.add(primeiro_getter.line, REVISAR, "ORDEM-SEM-SELECT",
                   f"'{f.nome}' chama {primeiro_getter.text}() sem OrderSelect nesta funcao: "
                   "o chamador precisa ter selecionado o ticket")
        _checar_ordens(an, f, corpo, locais)
    if long_linhas:
        an.add(long_linhas[0], REVISAR, "TIPO-LONG",
               f"'long'/'ulong' usados em {len(long_linhas)} linha(s) (1a aqui): exigem MT4 build >= 600; "
               "nao existem no MQL4 antigo")
    for nome, ln in sorted(constantes_conferir.items(), key=lambda x: x[1]):
        an.add(ln, REVISAR, "CONSTANTE-CONFERIR", f"{nome}: {CONSTANTES[nome]}")
    for nome, ln in sorted(desconhecidos.items(), key=lambda x: x[1]):
        an.add(ln, REVISAR, "IDENT-DESCONHECIDO", f"identificador/tipo '{nome}' nao esta na tabela MQL4 do linter (nem declarado no arquivo)")
    for nome, ln in sorted(funcs_desc.items(), key=lambda x: x[1]):
        an.add(ln, REVISAR, "FUNCAO-DESCONHECIDA", f"funcao '{nome}()' nao esta na tabela MQL4 do linter (nem definida/importada)")
    if metodos_zmq:
        lista = ", ".join(f".{m}() (l.{ln})" for m, ln in metodos_zmq.items())
        an.add(min(metodos_zmq.values()), REVISAR, "ZMQ-CONFERIR",
               f"metodos da biblioteca mql-zmq usados: {lista}; assinaturas NAO verificaveis aqui (conferir no .mqh instalado)")


def _valor_zero(txt: str) -> bool:
    try:
        return float(int(txt, 16) if txt.lower().startswith("0x") else txt) == 0.0
    except ValueError:
        return False


def _checar_chamada(an: Analise, f: Funcao, corpo: list[Tok], k: int, base: int,
                    locais: dict[str, tuple[str, bool]], visiveis: dict[str, tuple[str, bool]]) -> None:
    t = corpo[k]
    nome = t.text
    fe = _fim_args(corpo, k + 1)
    if fe < 0:
        return
    args = _dividir_args(corpo, k + 1, fe)
    na = len(args)
    vazio = na == 1 and not args[0]
    if vazio:
        na = 0
    alvo: Funcao | None = an.funcs.get(nome) or next((i for i in an.imports if i.nome == nome), None)
    if alvo is not None:
        lo, hi = alvo.aridade
        origem = f"importada de {alvo.importada}" if alvo.importada else "definida no arquivo"
        if not lo <= na <= hi:
            sig = f"{lo}" if lo == hi else f"{lo}..{hi}"
            cod = "DLL-ARIDADE" if alvo.importada else "ARIDADE"
            an.add(t.line, ERRO, cod, f"{nome}() ({origem}) chamada com {na} argumento(s); a assinatura declarada tem {sig}")
            return
        todos = {**an.globais, **locais}
        for i, (p, a) in enumerate(zip(alvo.params, args, strict=False)):
            ta = inferir_tipo(a, todos)
            prob = _tipos_compat(p, ta)
            if prob:
                cod = "DLL-TIPO" if alvo.importada else "TIPO-ARG"
                an.add(t.line, ERRO, cod, f"{nome}() argumento {i + 1}: {prob}")
            elif p.ref and not p.array and (len(a) != 1 or a[0].kind != "id"):
                an.add(t.line, ERRO, "REF-ARG", f"{nome}() argumento {i + 1}: parametro por referencia (&) exige variavel")
            elif ta == "double" and p.tipo in ("int", "uint", "long", "ulong", "short", "char") and not p.ref:
                an.add(t.line, AVISO, "TIPO-NARROW", f"{nome}() argumento {i + 1}: double passado para {p.tipo} (perde casas)")
        return
    if nome in FUNCS:
        lo, hi = FUNCS[nome]
        if not lo <= na <= hi:
            if nome == "OrderSend" and na == 2:
                an.add(t.line, ERRO, "MQL5-ORDERSEND",
                       "OrderSend(request, result) e a forma do MQL5 (struct); no MQL4 sao 7 a 11 argumentos escalares")
            else:
                sig = f"{lo}" if lo == hi else f"{lo}..{hi if hi < 64 else 'N'}"
                an.add(t.line, ERRO, "ARIDADE", f"{nome}() chamada com {na} argumento(s); a API MQL4 aceita {sig}")
        req = FUNCS_INCLUDE.get(nome)
        if req and not any(c.lower().endswith(req) for c, _ in an.includes):
            an.add(t.line, ERRO, "SEM-INCLUDE", f"{nome}() vem de <{req}>, que nao foi incluido")
        if nome == "OrderSend" and lo <= na <= hi:
            todos = {**an.globais, **locais}
            if inferir_tipo(args[1], todos) == "string" or inferir_tipo(args[0], todos) in _NUMERICOS - {"string"}:
                an.add(t.line, ERRO, "TIPO-ARG", "OrderSend: ordem dos argumentos suspeita (simbolo=string, cmd=int)")
        if nome == "OrderSelect" and lo <= na <= hi:
            pass


def _checar_ordens(an: Analise, f: Funcao, corpo: list[Tok], locais: dict[str, tuple[str, bool]]) -> None:
    """OrderSend sem checagem de -1, retornos de OrderClose/Modify/Delete/Select ignorados."""
    n = len(corpo)
    for k, t in enumerate(corpo):
        if t.kind != "id" or t.text not in ("OrderSend", "OrderClose", "OrderModify", "OrderDelete", "OrderSelect") \
                or k + 1 >= n or corpo[k + 1].text != "(":
            continue
        if k > 0 and corpo[k - 1].text == ".":
            continue
        fe = _fim_args(corpo, k + 1)
        prev = corpo[k - 1] if k > 0 else None
        nxt = corpo[fe + 1] if 0 < fe + 1 < n else None
        nome = t.text
        inicio = prev is None or prev.text in (";", "{", "}", "else") or (prev.text == ")" and k >= 2)
        if inicio and prev is not None and prev.text == ")":
            # `if(x) OrderClose(...)` tambem descarta o retorno
            pass
        if inicio:
            sev = AVISO
            what = {"OrderSend": "o ticket (-1 = falha) nao e lido", "OrderSelect": "falha de OrderSelect nao e tratada"}.get(
                nome, "o bool de sucesso nao e lido")
            an.add(t.line, sev, "RETORNO-IGNORADO", f"retorno de {nome}() descartado: {what}")
            continue
        # atribuicao: [tipo] var = Order...(...)
        if prev is not None and prev.text == "=" and k >= 2 and corpo[k - 2].kind == "id":
            var = corpo[k - 2].text
            if nome == "OrderSend":
                tp = locais.get(var, ("", False))[0]
                if tp == "bool":
                    an.add(t.line, AVISO, "TIPO-TICKET", f"OrderSend devolve int (ticket), atribuido a bool '{var}'")
            if not _var_checada(corpo, var, fe + 1):
                if nome == "OrderSend":
                    an.add(t.line, AVISO, "ORDERSEND-SEM-CHECAGEM",
                           f"ticket '{var}' de OrderSend nao e comparado (ex.: < 0 / == -1) nas linhas seguintes")
                else:
                    an.add(t.line, AVISO, "RETORNO-IGNORADO", f"resultado '{var}' de {nome}() nunca e testado depois")
            continue
        if nome == "OrderSend" and nxt is not None and nxt.text not in OPS_CMP and nxt.text not in (")", "&&", "||", ","):
            an.add(t.line, REVISAR, "ORDERSEND-USO", "OrderSend usado dentro de expressao: conferir checagem de -1")


def _var_checada(corpo: list[Tok], var: str, a_partir: int) -> bool:
    """`var` aparece depois comparado, negado ou como condicao isolada (if(var))?"""
    for m in range(a_partir, min(len(corpo), a_partir + 400)):
        if corpo[m].text != var or corpo[m].kind != "id":
            continue
        pr = corpo[m - 1].text if m > 0 else ""
        pn = corpo[m + 1].text if m + 1 < len(corpo) else ""
        if pn in OPS_CMP or pr in OPS_CMP or pr == "!":
            return True
        if pr == "(" and pn in (")", "&&", "||") and m >= 2 and corpo[m - 2].text in ("if", "while", "&&", "||", "!", "("):
            return True
        if pr in ("&&", "||") and pn in (")", "&&", "||"):
            return True
    return False


# =====================================================================================================
# DLL: resumo
# =====================================================================================================
def verificar_dll(an: Analise) -> None:
    vistos: dict[str, Funcao] = {}
    for im in an.imports:
        if im.nome in vistos:
            a = vistos[im.nome]
            if [(p.tipo, p.array, p.ref) for p in a.params] != [(p.tipo, p.array, p.ref) for p in im.params] \
                    or a.ret != im.ret:
                an.add(im.linha, AVISO, "DLL-DUPLICADA",
                       f"'{im.nome}' importada com assinaturas diferentes (l.{a.linha} e l.{im.linha})")
        vistos[im.nome] = im
    dlls = sorted({i.importada for i in an.imports if i.importada})
    for d in dlls:
        ln = next(i.linha for i in an.imports if i.importada == d)
        an.add(ln, REVISAR, "DLL-IMPORT",
               f"importa DLL '{d}': exige 'Permitir importacao de DLL' e DLL com a mesma arquitetura do MT4 (x86)")


# =====================================================================================================
# Protocolo: EA x PROTOCOLO.md x protocolo_mt4.py x conector_mt4.py
# =====================================================================================================
ENVELOPE = {"v", "id", "cmd", "args", "tk", "ok", "dados", "erro", "codigo", "msg", "origem"}
FUNCOES_ENVELOPE = {"OkJson", "ErrJson", "ErrMt4"}
_CHAVE_ESC_RE = re.compile(r'\\"([A-Za-z_][A-Za-z0-9_]*)\\":')


def _chaves_escritas(an: Analise, f: Funcao) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in an.toks[f.ini:f.fim + 1]:
        if t.kind == "str":
            for m in _CHAVE_ESC_RE.finditer(t.text):
                out.setdefault(m.group(1), t.line)
    return out


def _chaves_lidas(an: Analise, f: Funcao) -> dict[str, int]:
    out: dict[str, int] = {}
    toks = an.toks[f.ini:f.fim + 1]
    for k, t in enumerate(toks):
        if t.kind == "id" and t.text in ("JStr", "JNum", "JFind") and k + 1 < len(toks) and toks[k + 1].text == "(":
            fe = _fim_args(toks, k + 1)
            args = _dividir_args(toks, k + 1, fe) if fe > 0 else []
            if len(args) >= 2 and len(args[1]) == 1 and args[1][0].kind == "str":
                out.setdefault(args[1][0].text.strip('"'), t.line)
    return out


def _chamados(an: Analise, f: Funcao) -> set[str]:
    nomes = set()
    toks = an.toks[f.ini:f.fim + 1]
    for k, t in enumerate(toks[:-1]):
        if t.kind == "id" and toks[k + 1].text == "(" and t.text in an.funcs and t.text != f.nome:
            nomes.add(t.text)
    return nomes


def _fecho_escrita(an: Analise, nome: str) -> dict[str, int]:
    """Chaves escritas por `nome` e por tudo que ele chama (menos as funcoes do envelope)."""
    visto: set[str] = set()
    out: dict[str, int] = {}
    pilha = [nome]
    while pilha:
        x = pilha.pop()
        if x in visto or x in FUNCOES_ENVELOPE or x not in an.funcs:
            continue
        visto.add(x)
        f = an.funcs[x]
        if f.fim < 0:
            continue
        for kk, ln in _chaves_escritas(an, f).items():
            out.setdefault(kk, ln)
        pilha.extend(_chamados(an, f))
    return out


def comandos_do_ea(an: Analise) -> dict[str, tuple[str, int]]:
    """{comando: (funcao Cmd*, linha)} a partir de `cmd == "x"` em Tratar()."""
    out: dict[str, tuple[str, int]] = {}
    f = an.funcs.get("Tratar")
    if not f or f.fim < 0:
        return out
    toks = an.toks[f.ini:f.fim + 1]
    for k, t in enumerate(toks):
        if t.kind == "str" and k >= 2 and toks[k - 1].text == "==" and toks[k - 2].text == "cmd":
            cmd = t.text.strip('"')
            alvo = ""
            for x in toks[k + 1:k + 40]:
                if x.kind == "id" and x.text.startswith("Cmd"):
                    alvo = x.text
                    break
                if x.kind == "str" and x is not t and "==" in "".join(y.text for y in toks[k + 1:k + 40][:3]):
                    pass
            out.setdefault(cmd, (alvo, t.line))
    # comando atendido pelo `else` final (sem literal em Tratar): atribui a unica funcao Cmd* ainda sem comando
    usadas = {a for a, _ in out.values()}
    sobra = sorted({x.text for x in toks if x.kind == "id" and x.text.startswith("Cmd") and x.text in an.funcs} - usadas)
    faltam = sorted(comandos_ordem_do_ea(an) - set(out))
    if len(sobra) == 1 and len(faltam) == 1:
        out[faltam[0]] = (sobra[0], an.funcs[sobra[0]].linha)
    return out


def comandos_ordem_do_ea(an: Analise) -> set[str]:
    f = an.funcs.get("ComandoDeOrdem")
    if not f or f.fim < 0:
        return set()
    return {t.text.strip('"') for t in an.toks[f.ini:f.fim + 1] if t.kind == "str"}


@dataclass
class DocProto:
    args: dict[str, set[str]] = field(default_factory=dict)
    dados: dict[str, set[str]] = field(default_factory=dict)
    item: set[str] = field(default_factory=set)
    codigos: set[int] = field(default_factory=set)
    texto: str = ""


_SPAN_RE = re.compile(r"`([^`]*)`")
_IDENT_MIN_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _spans_chaves(celula: str) -> set[str]:
    celula = re.sub(r"\([^)]*\)", " ", celula)
    out = set()
    for s in _SPAN_RE.findall(celula):
        s = s.strip()
        if _IDENT_MIN_RE.match(s) and s not in ("true", "false", "null"):
            out.add(s)
    return out


def ler_doc(md: str) -> DocProto:
    d = DocProto(texto=md)
    for linha in md.splitlines():
        if not linha.startswith("| `"):
            continue
        cel = [c.strip() for c in linha.replace("\\|", "\x00").strip().strip("|").split("|")]
        if len(cel) < 3:
            continue
        m = re.match(r"`(\w+)`", cel[0])
        if not m:
            continue
        cmd = m.group(1)
        d.args[cmd] = _spans_chaves(cel[1].replace("\x00", "/"))
        d.dados[cmd] = _spans_chaves(cel[2].replace("\x00", "/"))
    m = re.search(r"^Item de posi.*?(?:\n\s*\n|\Z)", md, re.S | re.M)
    if m:
        d.item = _spans_chaves(m.group(0))
    for c in ("posicoes", "pendentes"):
        if c in d.dados:
            d.dados[c] |= d.item
    d.codigos = {int(x) for x in re.findall(r"\b(1\d{3})\b", md)}
    return d


def ler_py_protocolo(py: str) -> dict:
    cmds = dict(re.findall(r'^CMD_(\w+)\s*=\s*"(\w+)"', py, re.M))
    erros = {k: int(v) for k, v in re.findall(r"^(ERRO_\w+)\s*=\s*(\d+)", py, re.M)}
    m4 = {k: int(v) for k, v in re.findall(r"^(M4_\w+)\s*=\s*(\d+)", py, re.M)}
    mv = re.search(r"^VERSAO_PROTOCOLO\s*=\s*(\d+)", py, re.M)
    ordem: set[str] = set()
    mo = re.search(r"COMANDOS_ORDEM\s*=\s*frozenset\(\{(.*?)\}\)", py, re.S)
    if mo:
        ordem = {cmds[n] for n in re.findall(r"CMD_(\w+)", mo.group(1)) if n in cmds}
    return {"cmds": {v for v in cmds.values()}, "cmd_por_nome": cmds, "erros": erros, "m4": m4,
            "versao": int(mv.group(1)) if mv else None, "ordem": ordem}


def checar_protocolo(an: Analise, md: str | None, py: str | None, conector: str | None) -> list[Achado]:
    out: list[Achado] = []

    def add(ln: int, sev: str, cod: str, msg: str) -> None:
        out.append(Achado(ln, sev, cod, msg))

    cmds_ea = comandos_do_ea(an)
    if not cmds_ea:
        add(1, AVISO, "PROTO-SEM-TRATAR", "nao achei Tratar() com `cmd == \"...\"`: checagem de protocolo ignorada")
        return out
    doc = ler_doc(md) if md is not None else None
    pyp = ler_py_protocolo(py) if py is not None else None
    # --- comandos
    for cmd, (alvo, ln) in cmds_ea.items():
        if doc is not None and cmd not in doc.args:
            add(ln, ERRO, "PROTO-CMD-DOC", f"comando '{cmd}' do EA nao esta na tabela de PROTOCOLO.md")
        if pyp is not None and cmd not in pyp["cmds"]:
            add(ln, ERRO, "PROTO-CMD-PY", f"comando '{cmd}' do EA nao existe em protocolo_mt4.py (CMD_*)")
        if not alvo:
            add(ln, AVISO, "PROTO-CMD-SEM-FUNCAO", f"comando '{cmd}': nao achei a funcao Cmd* que o atende")
    if doc is not None:
        for cmd in doc.args:
            if cmd not in cmds_ea:
                add(1, ERRO, "PROTO-CMD-EA", f"comando '{cmd}' documentado em PROTOCOLO.md mas o EA nao o atende")
    if pyp is not None:
        for cmd in sorted(pyp["cmds"] - set(cmds_ea)):
            add(1, ERRO, "PROTO-CMD-EA", f"comando '{cmd}' (protocolo_mt4.py) nao e atendido pelo EA")
        ordem_ea = comandos_ordem_do_ea(an)
        if ordem_ea and pyp["ordem"] and ordem_ea != pyp["ordem"]:
            ln = an.funcs["ComandoDeOrdem"].linha
            add(ln, ERRO, "PROTO-ORDEM", f"comandos de ordem diferem: EA {sorted(ordem_ea)} x Python {sorted(pyp['ordem'])}")
    # --- chaves por funcao (comandos que compartilham funcao: uniao)
    grupos: dict[str, list[str]] = {}
    for cmd, (alvo, _ln) in cmds_ea.items():
        if alvo:
            grupos.setdefault(alvo, []).append(cmd)
    chaves_args_ea: dict[str, int] = {}
    chaves_dados_ea: dict[str, int] = {}
    for alvo, cmds in grupos.items():
        f = an.funcs.get(alvo)
        if f is None or f.fim < 0:
            continue
        lidas = _chaves_lidas(an, f)
        escritas = _fecho_escrita(an, alvo)
        chaves_args_ea.update(lidas)
        chaves_dados_ea.update(escritas)
        if doc is None:
            continue
        doc_args = set().union(*(doc.args.get(c, set()) for c in cmds))
        doc_dados = set().union(*(doc.dados.get(c, set()) for c in cmds))
        rotulo = "/".join(cmds)
        for kk, ln in lidas.items():
            if kk not in doc_args:
                add(ln, ERRO, "PROTO-ARG-DOC", f"[{rotulo}] o EA le a chave '{kk}' que PROTOCOLO.md nao lista em args")
        for kk in sorted(doc_args - set(lidas)):
            add(f.linha, AVISO, "PROTO-ARG-EA", f"[{rotulo}] PROTOCOLO.md lista o arg '{kk}' que o EA nunca le")
        for kk, ln in escritas.items():
            if kk not in doc_dados:
                add(ln, ERRO, "PROTO-DADOS-DOC", f"[{rotulo}] o EA escreve a chave '{kk}' que PROTOCOLO.md nao lista em dados")
        for kk in sorted(doc_dados - set(escritas)):
            add(f.linha, ERRO, "PROTO-DADOS-EA", f"[{rotulo}] PROTOCOLO.md promete a chave '{kk}' que o EA nao escreve")
    # --- envelope
    tratar = an.funcs.get("Tratar")
    if tratar is not None:
        for kk, ln in _chaves_lidas(an, tratar).items():
            chaves_args_ea.setdefault(kk, ln)
            if doc is not None and kk not in ENVELOPE:
                add(ln, ERRO, "PROTO-ENVELOPE", f"Tratar() le a chave de envelope '{kk}' desconhecida")
            if doc is not None and f'"{kk}"' not in doc.texto and f"`{kk}`" not in doc.texto:
                add(ln, ERRO, "PROTO-ENVELOPE-DOC", f"chave de envelope '{kk}' lida pelo EA nao aparece em PROTOCOLO.md")
    for nm in FUNCOES_ENVELOPE:
        f = an.funcs.get(nm)
        if f is None or f.fim < 0 or doc is None:
            continue
        for kk, ln in _chaves_escritas(an, f).items():
            if f'"{kk}"' not in doc.texto:
                add(ln, ERRO, "PROTO-ENVELOPE-DOC", f"{nm} escreve '{kk}', ausente do envelope em PROTOCOLO.md")
    # --- codigos de erro e versao
    defs = {m.group(1): int(m.group(2)) for m in re.finditer(r"^\s*#\s*define\s+(ERRO_\w+)\s+(\d+)", an.fonte, re.M)}
    for nome, val in defs.items():
        ln = next((p.line for p in an.pps if re.match(rf"#\s*define\s+{nome}\b", p.text)), 1)
        if pyp is not None and val not in pyp["erros"].values():
            add(ln, ERRO, "PROTO-ERRO-PY", f"{nome}={val} do EA nao existe em protocolo_mt4.py")
        if doc is not None and val not in doc.codigos:
            add(ln, ERRO, "PROTO-ERRO-DOC", f"{nome}={val} do EA nao aparece em PROTOCOLO.md")
    if pyp is not None:
        for nome, val in pyp["erros"].items():
            if nome != "ERRO_INTERNO" and val not in defs.values():
                add(1, ERRO, "PROTO-ERRO-EA", f"{nome}={val} (Python) nao tem #define no EA")
        for m in re.finditer(r"ErrMt4\s*\(\s*\w+\s*,\s*(\d+)\s*\)|ErrJson\s*\(\s*\w+\s*,\s*(\d{3,4})\s*,[^;]*?\"mt4\"", an.fonte):
            val = int(m.group(1) or m.group(2))
            if val not in pyp["m4"].values():
                ln = an.fonte.count("\n", 0, m.start()) + 1
                add(ln, AVISO, "PROTO-M4-PY", f"codigo MT4 {val} usado no EA nao tem constante M4_* em protocolo_mt4.py")
        mv = re.search(r"^\s*#\s*define\s+VERSAO_PROTOCOLO\s+(\d+)", an.fonte, re.M)
        if mv and pyp["versao"] is not None and int(mv.group(1)) != pyp["versao"]:
            add(1, ERRO, "PROTO-VERSAO", f"VERSAO_PROTOCOLO difere: EA {mv.group(1)} x Python {pyp['versao']}")
    # --- conector Python
    if conector is not None:
        lits = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', conector))
        for kk, ln in sorted(chaves_args_ea.items(), key=lambda x: x[1]):
            if kk not in ENVELOPE and kk not in lits:
                add(ln, AVISO, "PROTO-CONECTOR-ARG", f"o EA le o arg '{kk}' que conector_mt4.py nunca envia")
        nao_lidas = sorted(k for k in chaves_dados_ea if k not in ENVELOPE and k not in lits)
        if nao_lidas:
            add(1, REVISAR, "PROTO-CONECTOR-DADOS",
                "chaves que o EA escreve e conector_mt4.py nunca referencia (ignoradas pelo cliente): " + ", ".join(nao_lidas))
        usados = set(re.findall(r"\bP\.CMD_(\w+)", conector))
        if pyp is not None:
            for u in sorted(usados - set(pyp["cmd_por_nome"])):
                add(1, ERRO, "PROTO-CONECTOR-CMD", f"conector_mt4.py usa P.CMD_{u}, inexistente em protocolo_mt4.py")
    return out


# =====================================================================================================
# Orquestracao e CLI
# =====================================================================================================
def lint_fonte(fonte: str, md: str | None = None, py: str | None = None, conector: str | None = None,
               protocolo: bool = True) -> tuple[list[Achado], Analise]:
    an = Analise(fonte)
    an.balanco()
    an.diretivas()
    an.estrutura()
    an.corpos()
    verificar_funcoes(an)
    verificar_dll(an)
    if protocolo and (md is not None or py is not None or conector is not None):
        an.achados.extend(checar_protocolo(an, md, py, conector))
    vistos: set[Achado] = set()
    unicos = []
    for a in an.achados:
        if a not in vistos:
            vistos.add(a)
            unicos.append(a)
    unicos.sort(key=lambda a: (a.linha, _ORDEM_SEV[a.severidade], a.codigo, a.msg))
    return unicos, an


def _ler(caminho: Path) -> str | None:
    try:
        return caminho.read_text(encoding="utf-8")
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    raiz = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="Linter estatico (NAO e compilador) para Expert Advisors MQL4.")
    ap.add_argument("arquivo", help="arquivo .mq4")
    ap.add_argument("--md", default=str(raiz / "mql4" / "PROTOCOLO.md"))
    ap.add_argument("--py", default=str(raiz / "src" / "robowin" / "mt5" / "protocolo_mt4.py"))
    ap.add_argument("--conector", default=str(raiz / "src" / "robowin" / "mt5" / "conector_mt4.py"))
    ap.add_argument("--sem-protocolo", action="store_true", help="nao confere contra PROTOCOLO.md/Python")
    ap.add_argument("--sem-revisar", action="store_true", help="oculta achados REVISAR")
    ap.add_argument("--estrito", action="store_true", help="AVISO tambem reprova (codigo de saida 1)")
    a = ap.parse_args(argv)
    caminho = Path(a.arquivo)
    fonte = _ler(caminho)
    if fonte is None:
        print(f"erro: nao consegui ler {caminho}", file=sys.stderr)
        return 2
    md = py = cn = None
    if not a.sem_protocolo:
        md, py, cn = _ler(Path(a.md)), _ler(Path(a.py)), _ler(Path(a.conector))
    achados, an = lint_fonte(fonte, md, py, cn, protocolo=not a.sem_protocolo)
    if not a.sem_protocolo:
        for nome, txt in (("PROTOCOLO.md", md), ("protocolo_mt4.py", py), ("conector_mt4.py", cn)):
            if txt is None:
                achados.append(Achado(1, REVISAR, "PROTO-ARQUIVO", f"{nome} nao encontrado: parte da consistencia nao foi checada"))
    cont = {ERRO: 0, AVISO: 0, REVISAR: 0}
    for x in achados:
        cont[x.severidade] += 1
        if a.sem_revisar and x.severidade == REVISAR:
            continue
        print(x.formatar(str(caminho)))
    print(f"\n# {caminho}: {len(an.funcs)} funcoes, {len(an.imports)} funcoes importadas de DLL, "
          f"{len(an.toks)} tokens")
    for im in an.imports:
        print(f"#   import {im.importada}: {im.ret} {im.nome}({', '.join(p.tipo + ' ' + p.nome for p in im.params)})")
    print(f"# {cont[ERRO]} ERRO, {cont[AVISO]} AVISO, {cont[REVISAR]} REVISAR")
    print("# LEMBRETE: isto NAO e um compilador. Compile no MetaEditor (F7) e teste em DEMO.")
    if cont[ERRO] or (a.estrito and cont[AVISO]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
