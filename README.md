# robowin — agente de IA para day trade na B3 via MetaTrader 5 (ativo configurável; padrão WIN)

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

## Trocar o ativo ou a bolsa (só configuração)
- **Ativo:** edite `[geral].simbolo_base` em `config/robowin.toml` (ex.: `"WDO"`, `"IND"`, `"DOL"`, `"PETR4"`,
  `"BOVA11"`) ou use `--ativo WDO` em `simular`, `backtest`, `treinar`, `mm demo`, `rl demo`, `avaliar`,
  `diagnostico`, `gateway`, `coletar`, `nucleo`. Tick, valor do ponto, lote, tarifas, vencimento, stop/folga
  (em **ticks**) e sessão saem de `config/instrumentos.toml`; nenhum número de ativo vive no código (um teste
  estático falha se aparecer).
- **`a_confirmar = true`** marca valor sem fonte confirmada (suposição ou placeholder). Falha segura: o perfil
  **bloqueia** gateway, núcleo ao vivo, conexão ao MT5 e ordens; só roda **simulação sintética**, rotulada
  `ativo_a_confirmar`. Confira cada item de `a_confirmar_itens` com a B3/corretora e mude para `false`.
  Hoje só o **WIN** está confirmado; IND, WDO, DOL, PETR4 (modelo de ação) e BOVA11 (modelo de ETF) estão a confirmar.
- **Bolsa:** cada ativo aponta para uma bolsa de `config/bolsas.toml` (`bolsa = "B3"`; fuso, calendário do
  exchange-calendars, sessões versionadas, moeda, modelo de tarifas, limite de oscilação, regime tributário).
  Só a B3 está preenchida; NYSE, NASDAQ, LSE e CME são modelos `a_confirmar` que bloqueiam a operação.
- **Moeda:** P&L, custos e limites de risco estão na moeda do instrumento (os campos `*_brl` valem "na moeda do
  instrumento"); conversão só com taxa fornecida (`core.bolsas.converter`); o orçamento de IA segue em USD
  (faturamento da API). Imposto só no regime brasileiro; nas outras bolsas o relatório diz "tributo nao calculado".
- **Troca segura:** exige posição zerada; modelos treinados, registro e orçamento guardam o `ativo` e são
  recusados em outro ativo; o manifesto do experimento inclui o ativo e o hash do perfil. Ativo diferente do
  aprovado parte de limites mínimos (`risco_nao_aprovado`) até existir `config/risco-<ativo>.toml` com
  aprovação por hash em `config/aprovacoes/risco-<ativo>.toml`; ao vivo, sem isso, recusa.
