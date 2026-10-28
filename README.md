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
Autoverificação rápida (< 60 s, sem rede, sem ordens): `uv run robowin verificar` imprime uma matriz
**APROVADO / REPROVADO / NÃO VERIFICÁVEL AQUI** (com o motivo) e sai com código != 0 se houver REPROVADO;
`--json` para máquina. O que é verificável aqui, o que depende de você e por que lucratividade nunca é provada:
**[docs/ENTREGA.md](docs/ENTREGA.md)** (item do pedido original → onde está → evidência → status honesto).

**Máquina do Tempo** (simulação de bootstrap das últimas sessões, **NÃO é previsão**): chances de um plano de stop/alvo
antes de arriscar qualquer coisa, com custos do perfil e rótulo de dados sintéticos:
`uv run robowin maquina-do-tempo --ativo WIN --sintetico --lado compra --stop-pontos 100 --alvo-pontos 200`
(`--dados DIR` para ticks gravados; `--entrada HH:MM --zeragem HH:MM --seed N --replays N --json`). As mesmas
probabilidades vão, anonimizadas, ao revisor de IA; o veto por odds é opcional e vem desligado
(`[decisao].veto_por_odds`). Ideia e método inspirados no Time Machine do day-trader-skill (Jake Schincariol, MIT),
reimplementação independente: ver **[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)** e `docs/13-inspiracoes.md`.

**Simulação histórica com dados REAIS da B3** ("como se não tivesse havido acontecimentos": o robô e a IA só veem o que
estaria disponível em cada instante). No PowerShell do seu Windows, na pasta do projeto:
```
uv run robowin dados baixar-b3 --ativo WIN --ultimos 10 --saida dados_b3
uv run robowin dados importar-b3 --ativo WIN --arquivo dados_b3 --armazem dados_locais\ticks_b3 --relatorio relatorios\qualidade.md
uv run robowin historico --ativo WIN --armazem dados_locais\ticks_b3 --dias 10 --saida relatorios\historico
```
(`--de 2026-10-01 --ate 2026-10-27` no lugar de `--ultimos`; `--bracos quant,claude_gpt`; `--provedor falso` para nunca
chamar API). Sem `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` a IA é o provedor **FALSO** (rotulado). Relatório em
`relatorios\historico\consolidado.md` e um por pregão em `relatorios\historico\dias\<data>\`. Os negócios públicos da B3 não
têm lado agressor nem livro: o lado é **inferido** pela regra do tick e tudo sai rotulado; poucos pregões = amostra
insuficiente; **não prova lucratividade**. Na CI: workflow `historico-b3` (Actions ▸ historico-b3 ▸ Run workflow).
Detalhes: `docs/03-dados-integracoes-infra.md` e `docs/04-testes-e-criterios.md`.

Windows (MT5 real, conta **DEMO**): `scripts/windows/instalar.ps1`, depois `uv run robowin diagnostico` e
`docs/07-risco-e-operacao.md`.

**Aceitação no seu Windows (é o que fecha o "100%").** O que só existe na sua máquina (terminal MT5/MT4, conta demo,
compilação do EA, latência real de ordens e de IA) é provado por `uv run robowin aceitacao --confirmo-conta-demo`
(ou `scripts\windows\validar_tudo.ps1 -ConfirmoContaDemo`), que roda as etapas A1–A11 **somente em conta DEMO**, grava
`aceitacao-<data>.md/.json` e sai com código != 0 se houver REPROVADO. Sem a flag, nenhuma ordem é enviada. Aqui, "100%"
significa: **aceitação rodada no Windows do usuário, com relatório sem REPROVADO e com as etapas aplicáveis APROVADAS**.
Passo a passo e o que nunca é enviado: [docs/12-aceitacao-no-windows.md](docs/12-aceitacao-no-windows.md).

## Variáveis de ambiente
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` (e de outros provedores), `ROBOWIN_MT5_SENHA`, `ROBOWIN_TOKEN`.
Chaves nunca são gravadas em logs.

## Qualquer IA (não só Claude e GPT)
Gemini, DeepSeek, Grok, Mistral, Groq, OpenRouter, Qwen, Kimi, Cohere, Perplexity, Azure, Bedrock, Vertex e modelos
locais (Ollama, LM Studio, vLLM, llama.cpp) já estão em `config/provedores.toml`; provedor novo é uma entrada lá.
`setx DEEPSEEK_API_KEY "..."` → `uv run robowin ia adicionar-modelo deepseek:<modelo> --preco-entrada X --preco-saida Y
--corte AAAA-MM-DD` → `uv run robowin ia testar --modelo deepseek:<modelo>` → braço em `[[bracos]]`. `uv run robowin ia
listar` mostra chave presente/ausente (nunca o valor). Sem preço ou corte, só com declaração (teto de orçamento e
contaminação). Os testes rodam sem chave e sem rede. Passo a passo: [docs/09-modelos-de-ia.md](docs/09-modelos-de-ia.md).

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

**Limites de risco por usuário e por conta.** Os valores de `config/risco.toml` (R$ 60/operação, R$ 200/dia,
R$ 500/semana, drawdown R$ 1.500, 1 contrato) são a aprovação **provisória do titular, somente DEMO**, e não são padrão
para ninguém. Cada usuário define, altera e aprova os próprios limites pelo painel (página **Risco**: formulário,
pré-visualização, "Aprovar" com `APROVO` + token de ações, histórico) ou pela CLI (`robowin usuario criar`,
`robowin usuario conta`, `robowin risco definir/aprovar/status/historico --usuario U --conta PLAT:SERVIDOR:LOGIN`),
isolados por usuário e por conta. Usuário novo começa sem limites (bloqueado para operar); alterar invalida a
aprovação; tudo vai para a auditoria; núcleo/gateway/espelho/MM/aceitação usam só os limites aprovados do par ativo e
bloqueiam conta conectada diferente; a IA não lê nem altera limites; conta REAL fica sempre bloqueada
(`autorizacao_real`). Sem usuário configurado, vale o modo de arquivo único. Ver `docs/07-risco-e-operacao.md`.
