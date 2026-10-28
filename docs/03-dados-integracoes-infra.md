# 03 — Dados, integrações e infraestrutura

- **MT5:** pacote oficial `MetaTrader5` (só Windows x64, polling, `order_send` síncrono, 1 terminal por
  processo). Conta DEMO da XP (servidor informado pela corretora); login dedicado ao robô.
- **Coleta:** ticks (`copy_ticks_from`, dedup por `time_msc`), livro L2 agregado (`market_book_*`),
  offset do servidor calibrado, backfill na reconexão. Parquet + DuckDB; decisões e execuções em SQLite.
- **IA:** Anthropic (`claude-opus-5-5`, `claude-haiku-5-5`), OpenAI (`gpt-6.1-sol`), Google, Ollama,
  OpenAI-compatível. Sem região no Brasil; latência via internet.
- **Notícias:** Broadcast (Agência Estado) só após a especificação do contrato; fontes oficiais (BCB,
  IBGE, Fed, BLS) em `config/fontes.toml` com `verificado=false` até validação.
- **Infra sugerida:** Windows com MT5 (PC ou VPS em São Paulo); relógio sincronizado (NTP.br).

## Perfis de instrumento e de bolsa (ativo configurável)
`config/instrumentos.toml` é a única fonte dos números do ativo: `classe`, `bolsa`, `vencimento`
(`quarta_dia15_meses_pares` para WIN/IND; `primeiro_dia_util_mes` para WDO/DOL; `nenhum` para ação/ETF),
`ponto_valor_brl` (por lote, na moeda da bolsa), `tick_pontos`, `lote`, tarifas por vigência, `stop_max_ticks`,
`slippage_folga_ticks`, `stop_padrao_ticks`/`alvo_padrao_ticks`, `aceita_book`, `rlp`. `config/bolsas.toml` traz
fuso, calendário, sessões, moeda, tarifas-modelo, limite de oscilação e regime tributário.
`mt5/contrato.py` valida o símbolo da corretora contra o perfil (tick, valor do ponto por lote, lote, vencimento) e
**recusa** se divergir. **`a_confirmar`** = valor sem fonte confirmada: bloqueia gateway/núcleo/MT5 até o usuário
confirmar; a simulação sintética roda rotulada. Limite conhecido: o relógio interno da simulação ainda usa o fuso
`America/Sao_Paulo`; bolsas de outros fusos só têm o modelo e a operação bloqueada.
