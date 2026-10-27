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
