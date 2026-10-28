"""Confere as constantes numericas de robowin.mt5.constantes contra o pacote OFICIAL MetaTrader5.

Roda onde o pacote existe (Windows, CI Windows, Wine). Sai com codigo 1 se algum valor divergir.
Constantes sem equivalente no pacote Python viram AVISO (nao reprovam).
"""
from __future__ import annotations

import sys

from robowin.mt5 import constantes as C

MAPA = {
    "ACAO_DEAL": "TRADE_ACTION_DEAL", "ACAO_PENDING": "TRADE_ACTION_PENDING", "ACAO_SLTP": "TRADE_ACTION_SLTP",
    "ACAO_MODIFY": "TRADE_ACTION_MODIFY", "ACAO_REMOVE": "TRADE_ACTION_REMOVE",
    "ORDEM_BUY": "ORDER_TYPE_BUY", "ORDEM_SELL": "ORDER_TYPE_SELL", "ORDEM_BUY_LIMIT": "ORDER_TYPE_BUY_LIMIT",
    "ORDEM_SELL_LIMIT": "ORDER_TYPE_SELL_LIMIT",
    "PREENCHE_FOK": "ORDER_FILLING_FOK", "PREENCHE_IOC": "ORDER_FILLING_IOC", "PREENCHE_RETURN": "ORDER_FILLING_RETURN",
    "SIMB_FILL_FOK": "SYMBOL_FILLING_FOK", "SIMB_FILL_IOC": "SYMBOL_FILLING_IOC",
    "VALIDADE_GTC": "ORDER_TIME_GTC", "VALIDADE_DIA": "ORDER_TIME_DAY",
    "POS_COMPRA": "POSITION_TYPE_BUY", "POS_VENDA": "POSITION_TYPE_SELL",
    "DEAL_COMPRA": "DEAL_TYPE_BUY", "DEAL_VENDA": "DEAL_TYPE_SELL",
    "ENTRADA_IN": "DEAL_ENTRY_IN", "ENTRADA_OUT": "DEAL_ENTRY_OUT", "ENTRADA_INOUT": "DEAL_ENTRY_INOUT",
    "ENTRADA_OUT_BY": "DEAL_ENTRY_OUT_BY",
    "RAZAO_CLIENT": "DEAL_REASON_CLIENT", "RAZAO_MOBILE": "DEAL_REASON_MOBILE", "RAZAO_WEB": "DEAL_REASON_WEB",
    "RAZAO_EXPERT": "DEAL_REASON_EXPERT", "RAZAO_SL": "DEAL_REASON_SL", "RAZAO_TP": "DEAL_REASON_TP",
    "RAZAO_SO": "DEAL_REASON_SO",
    "CONTA_DEMO": "ACCOUNT_TRADE_MODE_DEMO", "CONTA_CONTEST": "ACCOUNT_TRADE_MODE_CONTEST",
    "CONTA_REAL": "ACCOUNT_TRADE_MODE_REAL",
    "MARGEM_NETTING": "ACCOUNT_MARGIN_MODE_RETAIL_NETTING", "MARGEM_EXCHANGE": "ACCOUNT_MARGIN_MODE_EXCHANGE",
    "MARGEM_HEDGING": "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING",
    "EXEC_REQUEST": "SYMBOL_TRADE_EXECUTION_REQUEST", "EXEC_INSTANT": "SYMBOL_TRADE_EXECUTION_INSTANT",
    "EXEC_MARKET": "SYMBOL_TRADE_EXECUTION_MARKET", "EXEC_EXCHANGE": "SYMBOL_TRADE_EXECUTION_EXCHANGE",
    "COPY_TICKS_ALL": "COPY_TICKS_ALL", "COPY_TICKS_INFO": "COPY_TICKS_INFO", "COPY_TICKS_TRADE": "COPY_TICKS_TRADE",
    "FLAG_BID": "TICK_FLAG_BID", "FLAG_ASK": "TICK_FLAG_ASK", "FLAG_LAST": "TICK_FLAG_LAST",
    "FLAG_VOLUME": "TICK_FLAG_VOLUME", "FLAG_BUY": "TICK_FLAG_BUY", "FLAG_SELL": "TICK_FLAG_SELL",
    "BOOK_VENDA": "BOOK_TYPE_SELL", "BOOK_COMPRA": "BOOK_TYPE_BUY",
    "BOOK_VENDA_MERCADO": "BOOK_TYPE_SELL_MARKET", "BOOK_COMPRA_MERCADO": "BOOK_TYPE_BUY_MARKET",
    "RET_REQUOTE": "TRADE_RETCODE_REQUOTE", "RET_REJEITADO": "TRADE_RETCODE_REJECT", "RET_COLOCADA": "TRADE_RETCODE_PLACED",
    "RET_EXECUTADA": "TRADE_RETCODE_DONE", "RET_PARCIAL": "TRADE_RETCODE_DONE_PARTIAL", "RET_ERRO": "TRADE_RETCODE_ERROR",
    "RET_TIMEOUT": "TRADE_RETCODE_TIMEOUT", "RET_INVALIDO": "TRADE_RETCODE_INVALID",
    "RET_VOLUME_INVALIDO": "TRADE_RETCODE_INVALID_VOLUME", "RET_PRECO_INVALIDO": "TRADE_RETCODE_INVALID_PRICE",
    "RET_STOPS_INVALIDOS": "TRADE_RETCODE_INVALID_STOPS", "RET_TRADE_DESABILITADO": "TRADE_RETCODE_TRADE_DISABLED",
    "RET_MERCADO_FECHADO": "TRADE_RETCODE_MARKET_CLOSED", "RET_SEM_DINHEIRO": "TRADE_RETCODE_NO_MONEY",
    "RET_MUITAS_REQUISICOES": "TRADE_RETCODE_TOO_MANY_REQUESTS", "RET_SEM_MUDANCAS": "TRADE_RETCODE_NO_CHANGES",
    "RET_ALGO_DESLIGADO_CLIENTE": "TRADE_RETCODE_CLIENT_DISABLES_AT",
    "RET_PREENCHIMENTO_INVALIDO": "TRADE_RETCODE_INVALID_FILL", "RET_SEM_CONEXAO": "TRADE_RETCODE_CONNECTION",
    "RET_POSICAO_FECHADA": "TRADE_RETCODE_POSITION_CLOSED",
}


def main() -> int:
    try:
        import MetaTrader5 as mt
    except ImportError:
        print("pacote MetaTrader5 ausente (so existe no Windows): NAO VERIFICAVEL aqui")
        return 2
    ok = diverge = ausente = 0
    for local, oficial in MAPA.items():
        meu = getattr(C, local, None)
        if meu is None:
            print(f"ERRO   {local}: nao existe em robowin.mt5.constantes")
            diverge += 1
            continue
        real = getattr(mt, oficial, None)
        if real is None:
            print(f"AVISO  {local} ({oficial}): sem equivalente no pacote Python (valor local {meu})")
            ausente += 1
        elif int(real) != int(meu):
            print(f"DIVERGE {local}={meu} mas {oficial}={real}")
            diverge += 1
        else:
            ok += 1
    print(f"MetaTrader5 {getattr(mt, '__version__', '?')}: {ok} conferem, {diverge} divergem, {ausente} sem equivalente")
    return 1 if diverge else 0


if __name__ == "__main__":
    sys.exit(main())
