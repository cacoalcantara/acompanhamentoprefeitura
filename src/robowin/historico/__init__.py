"""Simulacao historica walk-forward CAUSAL sobre dados REAIS (``robowin historico``).

Para cada pregao D, em ordem: modelo quant treinado SO com pregoes < D, Maquina do Tempo SO com sessoes < D e
replay de D por instante de DISPONIBILIDADE no mesmo orquestrador do ``robowin simular`` (custos, latencia
injetada, risco com veto, controles aleatorios, IA pelo provedor configurado com dossie anonimizado).
"""
