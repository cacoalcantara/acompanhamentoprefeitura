"""Decisao (M8): bracos experimentais, gatilhos, plano condicional, validade e o orquestrador da sessao.

API publica: ``Orquestrador``/``ConfigOrquestrador`` (orquestrador), ``bracos`` (A-quant, B, C, controles e
extras de ``[[bracos]]``), ``GerenciadorGatilhos`` (gatilhos), ``GerenciadorPlano`` (plano_condicional),
``BloqueioDados``/``classificar_chamada``/``Prazos`` (validade), ``montar_sessao``/``executar_sessao``
(simulacao) e ``Nucleo`` (ao vivo). Tudo e SIMULACAO: nenhuma ordem real.
"""
