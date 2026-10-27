# robowin — agente de IA para day trade de WIN (B3) via MetaTrader 5

> **SIMULAÇÃO. Nenhuma ordem real é enviada.** O envio real não está implementado e levanta
> `ExecucaoRealNaoAutorizada`. Nada neste repositório é garantia de rentabilidade. Poucas pessoas físicas
> lucram com day trade, e não há evidência de que robôs melhorem esse resultado (alerta da CVM).

Versão 1: conector MT5, contrato vigente, coleta de dados, motor de IA (Claude propõe, GPT revisa, outros
modelos plugáveis), modelos de microestrutura, StatArb, RL de execução, market making (simulado), backtest
cronológico sem vazamento, simulação, risco independente, painel web em português, testes e auditoria com
cadeia de hash. Plano completo e fontes: `docs/`.

## Arquitetura (resumo)
`gateway` (Windows, único processo que fala com o MT5) ⇄ ZeroMQ ⇄ `nucleo` (decisão, risco, execução simulada)
→ SQLite/Parquet → `painel` (FastAPI, pt-BR). Stop, alvo e zeragem **não dependem de IA ou de rede**.

## Início rápido
Linux/qualquer (sem MT5, com dublê e dados sintéticos rotulados):
```
uv sync --extra dev
uv run pytest
uv run robowin backtest --dados sinteticos
uv run robowin painel --banco dados_locais/robowin.sqlite
```
Windows (MT5 real, conta **DEMO**): `scripts/windows/instalar.ps1`, depois `uv run robowin diagnostico` e
`docs/07-risco-e-operacao.md`.

## Variáveis de ambiente
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (e de outros provedores), `ROBOWIN_MT5_SENHA`, `ROBOWIN_TOKEN`.
Chaves nunca são gravadas em logs.
