"""Rotulo ``correcoes:v1.1`` (docs/16 secao 2): correcoes que MUDAM o comportamento de B, C, quant ou controles.

Fica num modulo proprio, sem dependencias pesadas, porque entra em dois lugares que precisam concordar: o manifesto
da simulacao (``decisao.simulacao.hashes_planejador``) e o pre-registro (``avaliacao.preregistro``). O hash do
rotulo cobre a lista E a fonte dos modulos que implementam as correcoes: editar qualquer um deles depois do registro
muda o hash (variante nova), em vez de passar despercebido sob o mesmo texto.
"""
from __future__ import annotations

# C2, C4, C5, C6, C16 e C18 (marcados [v1.1] na tabela) e as que tambem mudam B, C, quant ou controles:
# * C7: stop_colado_no_preco no MANTER legado (C); C8: stop/alvo abaixo de um tick; C9: despacho fail-closed;
# * C19: a IA (B, C) e o quant nao sao chamados sem entrada possivel (menos decisoes, menos custo);
# * veto_teto_tempo_posicao: o motor veta toda entrada com tempo_max_s acima de tempo_max_posicao_s ou que passe
#   da zeragem (B, C, quant e [[bracos]] extras perdem as entradas do fim do pregao);
# * controles_backtest_300s: controles do backtest com tempo_max_s=300 (antes 900/600 s).
CORRECOES_V11: tuple[str, ...] = ("C2", "C4", "C5", "C6", "C7", "C8", "C9", "C16", "C18", "C19",
                                  "veto_teto_tempo_posicao", "controles_backtest_300s")

# modulos cuja fonte entra no hash do rotulo (onde as correcoes acima moram)
MODULOS_CORRECOES_V11: tuple[str, ...] = (
    "robowin.risco.motor", "robowin.decisao.dossie_conta", "robowin.decisao.validade", "robowin.ia.validadores",
    "robowin.backtest.bracos_referencia", "robowin.decisao.bracos.llm",
)
