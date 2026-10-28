"""Protocolo da ponte MT4 (versao 1): constantes e codificacao compartilhadas por `ConectorMT4` (cliente), pelo
`DubleMT4` (servidor falso) e documentadas em mql4/PROTOCOLO.md para o Expert Advisor mql4/RobowinBridge.mq4.

Transporte: ZeroMQ REQ (cliente Python) -> REP (EA), um JSON UTF-8 por mensagem, em 127.0.0.1.
Requisicao: {"v":1,"id":"<texto unico>","cmd":"<comando>","args":{...}}
Resposta ok: {"v":1,"id":"<mesmo id>","ok":true,"dados":{...}}
Resposta com erro: {"v":1,"id":"<mesmo id>","ok":false,"erro":{"codigo":<int>,"msg":"<texto>","origem":"mt4|ponte"}}

Os nomes dos comandos de ordem NAO usam o identificador do envio de ordens do MT5 de proposito: o teste
estatico do projeto so admite UM chamador desse nome (mt5/execucao_demo.py).
"""
from __future__ import annotations

import json
from typing import Any

VERSAO_PROTOCOLO = 1

# --- comandos --------------------------------------------------------------------------------------
CMD_OLA = "ola"
CMD_CONTA = "conta"
CMD_SIMBOLOS = "simbolos"
CMD_SIMBOLO = "simbolo"
CMD_SELECIONAR = "selecionar"
CMD_TICK = "tick"
CMD_TICKS = "ticks"
CMD_POSICOES = "posicoes"
CMD_PENDENTES = "pendentes"
CMD_HISTORICO = "historico"
CMD_ORDEM_ABRIR = "ordem_abrir"
CMD_ORDEM_FECHAR = "ordem_fechar"
CMD_ORDEM_MODIFICAR = "ordem_modificar"
CMD_ORDEM_APAGAR = "ordem_apagar"

COMANDOS_LEITURA = frozenset({CMD_OLA, CMD_CONTA, CMD_SIMBOLOS, CMD_SIMBOLO, CMD_SELECIONAR, CMD_TICK, CMD_TICKS,
                              CMD_POSICOES, CMD_PENDENTES, CMD_HISTORICO})
COMANDOS_ORDEM = frozenset({CMD_ORDEM_ABRIR, CMD_ORDEM_FECHAR, CMD_ORDEM_MODIFICAR, CMD_ORDEM_APAGAR})
COMANDOS = COMANDOS_LEITURA | COMANDOS_ORDEM

# --- erros da PONTE (>= 1000; os demais sao GetLastError() do MQL4) -----------------------------------
ERRO_JSON_INVALIDO = 1001
ERRO_ARGUMENTO = 1002
ERRO_VERSAO = 1003
ERRO_SIMBOLO = 1004
ERRO_TICKET = 1005
ERRO_CONTA_NAO_DEMO = 1006
ERRO_SOMENTE_LEITURA = 1007
ERRO_COMANDO_DESCONHECIDO = 1008
ERRO_SLTP_FALHOU_POSICAO_FECHADA = 1009  # SL/TP nao foi aceito apos abrir; a posicao foi fechada por seguranca
ERRO_TOKEN = 1010  # token ausente/errado (EA com input Token preenchido)
ERRO_LIMITE_POSICOES = 1011  # posicoes + pendentes do simbolo/magic ja no maximo (input MaxPosicoes do EA)
ERRO_MAGIC = 1012  # ticket (ou ordem) de magic fora dos permitidos (input Magics do EA)
ERRO_INTERNO = 1099

# --- MQL4 GetLastError() relevantes --------------------------------------------------------------------
M4_SEM_RESULTADO = 1
M4_ERRO_COMUM = 2
M4_PARAMETROS_INVALIDOS = 3
M4_SERVIDOR_OCUPADO = 4
M4_SEM_CONEXAO = 6
M4_MUITO_FREQUENTE = 8
M4_CONTA_DESABILITADA = 64
M4_CONTA_INVALIDA = 65
M4_TIMEOUT = 128
M4_PRECO_INVALIDO = 129
M4_STOPS_INVALIDOS = 130
M4_VOLUME_INVALIDO = 131
M4_MERCADO_FECHADO = 132
M4_TRADE_DESABILITADO = 133
M4_SEM_DINHEIRO = 134
M4_PRECO_MUDOU = 135
M4_SEM_COTACAO = 136
M4_CORRETORA_OCUPADA = 137
M4_REQUOTE = 138
M4_ORDEM_TRAVADA = 139
M4_MUITAS_REQUISICOES = 141
M4_MODIFICACAO_NEGADA = 145
M4_CONTEXTO_OCUPADO = 146
M4_EXPIRACAO_NEGADA = 147
M4_MUITAS_ORDENS = 148
M4_HEDGE_PROIBIDO = 149
M4_FIFO = 150
M4_SIMBOLO_DESCONHECIDO = 4106
M4_TICKET_INVALIDO = 4108
M4_TRADE_NAO_PERMITIDO = 4109
M4_LOTE_INVALIDO = 4051

# codigos cujo desfecho e DESCONHECIDO (a ordem pode ter sido executada): viram retcode de ordem INCERTA
M4_INCERTOS = frozenset({M4_SEM_RESULTADO, M4_SEM_CONEXAO, M4_TIMEOUT})


def retcode_de_erro(codigo: int) -> int:
    """Converte erro MT4/ponte para o `retcode` equivalente do MT5 (constantes.RET_*)."""
    from . import constantes as C
    tabela = {
        M4_SEM_RESULTADO: C.RET_TIMEOUT, M4_TIMEOUT: C.RET_TIMEOUT, M4_SEM_CONEXAO: C.RET_SEM_CONEXAO,
        M4_ERRO_COMUM: C.RET_ERRO, M4_PARAMETROS_INVALIDOS: C.RET_INVALIDO, M4_SERVIDOR_OCUPADO: C.RET_REJEITADO,
        M4_MUITO_FREQUENTE: C.RET_MUITAS_REQUISICOES, M4_MUITAS_REQUISICOES: C.RET_MUITAS_REQUISICOES,
        M4_CONTEXTO_OCUPADO: C.RET_MUITAS_REQUISICOES, M4_CONTA_DESABILITADA: C.RET_TRADE_DESABILITADO,
        M4_CONTA_INVALIDA: C.RET_TRADE_DESABILITADO, M4_PRECO_INVALIDO: C.RET_PRECO_INVALIDO,
        M4_STOPS_INVALIDOS: C.RET_STOPS_INVALIDOS, M4_MODIFICACAO_NEGADA: C.RET_STOPS_INVALIDOS,
        M4_VOLUME_INVALIDO: C.RET_VOLUME_INVALIDO, M4_LOTE_INVALIDO: C.RET_VOLUME_INVALIDO,
        M4_MERCADO_FECHADO: C.RET_MERCADO_FECHADO, M4_TRADE_DESABILITADO: C.RET_TRADE_DESABILITADO,
        M4_SEM_DINHEIRO: C.RET_SEM_DINHEIRO, M4_PRECO_MUDOU: C.RET_REQUOTE, M4_REQUOTE: C.RET_REQUOTE,
        M4_SEM_COTACAO: C.RET_REQUOTE, M4_CORRETORA_OCUPADA: C.RET_REQUOTE, M4_ORDEM_TRAVADA: C.RET_REJEITADO,
        M4_EXPIRACAO_NEGADA: C.RET_INVALIDO, M4_MUITAS_ORDENS: C.RET_REJEITADO, M4_HEDGE_PROIBIDO: C.RET_REJEITADO,
        M4_FIFO: C.RET_REJEITADO, M4_SIMBOLO_DESCONHECIDO: C.RET_INVALIDO, M4_TICKET_INVALIDO: C.RET_POSICAO_FECHADA,
        M4_TRADE_NAO_PERMITIDO: C.RET_ALGO_DESLIGADO_CLIENTE,
        ERRO_TICKET: C.RET_POSICAO_FECHADA, ERRO_SIMBOLO: C.RET_INVALIDO, ERRO_ARGUMENTO: C.RET_INVALIDO,
        ERRO_CONTA_NAO_DEMO: C.RET_TRADE_DESABILITADO, ERRO_SOMENTE_LEITURA: C.RET_TRADE_DESABILITADO,
        ERRO_SLTP_FALHOU_POSICAO_FECHADA: C.RET_STOPS_INVALIDOS,
        ERRO_TOKEN: C.RET_TRADE_DESABILITADO, ERRO_LIMITE_POSICOES: C.RET_REJEITADO, ERRO_MAGIC: C.RET_REJEITADO,
    }
    return tabela.get(int(codigo), C.RET_ERRO)


class ErroProtocolo(Exception):
    """Resposta malformada, versao incompativel ou mensagem fora do contrato."""


def codificar(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def decodificar(dados: bytes) -> dict[str, Any]:
    try:
        msg = json.loads(dados.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ErroProtocolo(f"JSON invalido: {e}") from e
    if not isinstance(msg, dict):
        raise ErroProtocolo("a mensagem nao e um objeto JSON")
    return msg


def requisicao(id_: str, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"v": VERSAO_PROTOCOLO, "id": id_, "cmd": cmd, "args": args or {}}


def resposta_ok(id_: str, dados: dict[str, Any]) -> dict[str, Any]:
    return {"v": VERSAO_PROTOCOLO, "id": id_, "ok": True, "dados": dados}


def resposta_erro(id_: str, codigo: int, msg: str, origem: str = "ponte") -> dict[str, Any]:
    return {"v": VERSAO_PROTOCOLO, "id": id_, "ok": False, "erro": {"codigo": int(codigo), "msg": msg, "origem": origem}}
