# 13b — Estudo de Aurum, TradingAgents, claude-trader e trader-skill: matriz de decisão

> **SOMENTE SIMULAÇÃO / conta DEMO. Nada aqui envia ordem real.** Este documento registra o que foi lido em
> quatro repositórios open source (clonados em modo **somente leitura**, nada foi executado nem instalado, nenhuma
> chave copiada) e **o que foi efetivamente adaptado** ao robowin. Tudo foi **reimplementado de forma independente**
> (nomes, tipos e testes próprios, em pt-BR); nenhum trecho foi copiado em bloco. O fragmento de atribuição
> está em `NOTICES_AURUM_TRADINGAGENTS.md`. O quinto projeto (day-trader-skill) está com outro agente.

## Regras que filtram tudo

1. Sem indicadores técnicos tradicionais (RSI, MACD, médias, Bollinger, ATR, ADX, cruzamentos...): teste estático.
2. Sem ordens reais; `order_send` só em `mt5/execucao_demo.py` (teste AST); 1 contrato; DEMO.
3. IA nunca fala com o mercado: propõe, validação determinística e **veto final do motor de risco**.
4. Dossiê anonimizado (sem datas, níveis absolutos, texto de notícia, nome de ativo).
5. Nenhum literal de ativo fora de `config/` e dos testes.

## Honestidade sobre as fontes

* **Aurum** (XAUUSD/MT5): o próprio autor declara que, sob protocolo pré-registrado em 14 anos de dados, **nenhuma
  estratégia tem vantagem estatística demonstrada** (melhor DSR em walk-forward 0,05 contra a barra de 0,95). Foi
  lido como **fonte de engenharia e de governança**, não como evidência de que algo funciona.
* **TradingAgents** (Apache-2.0): pesquisa acadêmica; os analistas dependem de indicadores técnicos (MACD, RSI,
  Bollinger, ATR, médias) e de dados da internet. **Esses não podem ser usados.** A arquitetura de debate e a memória
  reflexiva são o aproveitável, e **não há evidência aqui de que debater melhore o resultado**: vira hipótese H4.
* **claude-trader** (MIT): **não tem testes**, executa `order_send` direto, multi-par FX e Telegram. Quase tudo rejeitado.
* **trader-skill** (MIT): ações dos EUA via SEC/Congresso, longo prazo. Aproveitável só o princípio do portão de
  risco e a impressão da ordem aprovada.

Legenda de decisão: **adotar** (ideia aplicada quase como está), **adaptar** (reescrita para as nossas regras),
**rejeitar** (com motivo), **já existe** (equivalente no robowin, nada a fazer).

## Matriz

### Aurum (`zero-was-here/tradingbot`, MIT)

| ID | Origem | O que faz | Já existe no robowin? | Adaptável a WIN/B3 sem indicadores? | Decisão | Motivo e onde ficou |
|---|---|---|---|---|---|---|
| A1 | `aurum/agents/policy.py` (`DecisionPolicy`: overlay/advisory/discretionary, `on_failure`, `min_confidence`) | Converte a decisão do LLM em previsão limitada: no modo overlay a IA só reduz ou veta o sinal quant, nunca inverte nem aumenta | Parcial: veto final do risco, falha da IA bloqueia entrada (`decisao/validade.py`, `ia/validadores.py`); não existe overlay sobre o braço A | Sim, conceitualmente | **rejeitar** | A hipótese pré-registrada (H1/H2) é que a IA **propõe** entradas próprias e é medida contra o controle; o overlay a transformaria em filtro do quant e mudaria a pergunta. Pode virar um braço futuro com id próprio. |
| A2 | `aurum/agents/usage.py` (`UsageLedger`, custo por agente) | Soma tokens e custo por agente/chamada, com preço de cache e orçamento por ciclo | Parcial: `ia/orcamento.py` reserva e confirma custo; `Registro.chamada` grava cada chamada; faltava o agregado por braço × papel | Sim | **adaptar** | `avaliacao/uso_ia.py` (`resumir_uso`, `carregar_chamadas`, tabela em Markdown) anexado ao `robowin avaliar`; essencial para o debate (6+ chamadas por decisão). Teste: `tests/unit/test_aval_robustez_uso.py`. |
| A3 | `aurum/agents/tools.py`, `chief.py` (allow-list de ferramentas por agente, `max_specialists_per_cycle`, `create_specialist`) | Governança do que cada agente pode chamar; o chefe cria agentes em tempo de execução | Não: no robowin o LLM não tem ferramentas | Parcial | **adaptar** (envelope) / **rejeitar** (criação em runtime) | `EnvelopeAgente` + `verificar_envelope` em `decisao/bracos/debate.py`: cada etapa do debate declara **zero ferramentas, zero internet, entrada = dossiê anonimizado**, conferido em teste. Agentes criados pelo LLM em runtime ampliam superfície não auditável: rejeitado. |
| A4 | `aurum/agents/journal.py` (JSONL por ciclo, truncamento, degrada para memória) | Auditoria de cada evento do ciclo do desk | **Já existe** e é mais forte: `auditoria/cadeia.py` (cadeia de hash), `chamadas_llm.resposta_bruta`, `decisoes` | Sim | já existe | Acrescentado: resumo do debate no `proposta_json` da decisão e evento `reflexao_pos_trade` na auditoria. |
| A5 | `aurum/agents/client.py`, `loop.py`, `providers.py` (laço de tool use, retries, cache de prompt) | Cliente Anthropic com ferramentas | Já existe: `ia/provedores/*`, `ia/orcamento.py`, `ia/cache.py` | n/a | já existe | Sem laço de ferramentas por desenho. |
| A6 | `aurum/research/splits.py` (purga, embargo, walk-forward, K-fold purgado, CPCV) | Validação cruzada temporal sem vazamento | Já existe: `modelos/walkforward.py` (`mascara_treino` com purga e `embargo_ns`, `dividir_walkforward`, `verificar_sem_vazamento`) e `backtest/vazamento.py` (perturbação do futuro) | Sim | já existe; **CPCV rejeitado** | CPCV não foi implementado: as dobras walk-forward com purga/embargo cobrem o uso atual (um ativo, poucos modelos); o custo de manter CPCV supera o ganho. Reavaliar se houver muitos modelos candidatos. |
| A7 | `aurum/research/stats.py` (PSR, DSR, Holm, bootstrap estacionário, MinTRL, PBO/CSCV, haircut) | Estatística anti-sorte de seleção | Já existe: `avaliacao/estatistica.py` (DSR, PSR, bootstrap por pregão, SPRT, CUSUM), `comparacao.py` (Holm). **Faltavam MinTRL e PBO** | Sim | **adotar** (MinTRL, PBO) / já existe (resto) / rejeitar `haircut_sharpe` | `avaliacao/robustez.py`: `pbo_cscv`, `pbo_entre_bracos`, `min_track_record_length`; PBO entra no `robowin avaliar`. `haircut_sharpe` é redundante com DSR + bootstrap. Testes em `test_aval_robustez_uso.py`. |
| A8 | `aurum/risk/var.py` (VaR/ES histórico, gaussiano, Cornish-Fisher, Kupiec, `stress_test`) | Medidas de cauda | Parcial: `risco/estresse.py` (gap e circuit breaker). **Não havia VaR/ES nem Kupiec** | Sim (P&L diário em R$) | **adaptar** (histórico + Kupiec) / rejeitar o resto | `risco/cauda.py`: `var_es_historico`, `kupiec_pof`, `backtest_var_deslizante`, `relatorio_cauda`. **Só monitoramento, nunca veto**: teste estático garante que nenhum módulo de risco, execução, decisão ou mt5 importa `cauda`. Gaussiano e Cornish-Fisher rejeitados (subestimam cauda com amostras curtas). |
| A9 | `aurum/risk/manager.py` (halt persistente, blackout de eventos, `no_new_risk`, `reset_halt("RESET")`) | Risco que só reduz | Já existe: `risco/kill_switch.py` (rearme só manual), `risco/motor.py`, `risco/invariantes.py` | Sim | já existe | — |
| A10 | `aurum/live/oms.py`, `broker.py` (client id idempotente, reconciliação) | OMS idempotente | Já existe: `execucao/oms.py`, `reconciliacao.py` | Sim | já existe | — |
| A11 | `aurum/live/monitor.py` (PSI, `SlippageTracker`, `PnLBand`, sinks de alerta/webhook) | Monitoramento ao vivo | Parcial: CUSUM em `avaliacao/estatistica.py`; **sem PSI nem rastreio de custo real × modelo** | Sim | **adaptar** (PSI, slippage) / já existe (PnLBand ≈ CUSUM) / rejeitar (webhook) | `avaliacao/monitoramento.py`: `psi`, `psi_amostras`, `deriva_de_features`, `RastreadorSlippage` (em ticks). **Ainda não ligado ao fluxo ao vivo**: biblioteca para relatórios e aceitação. Webhooks externos ampliam superfície; o painel e a auditoria cobrem. |
| A12 | `aurum/live/state.py` (escrita atômica, heartbeat) | Estado durável | Já existe (heartbeat no kill switch, auditoria append-only, SQLite) | Sim | já existe | — |
| A13 | `aurum/strategies/*`, `features/technical.py`, `volatility.py` (TSMOM, EWMAC, Donchian, RSI(2), Bollinger, ATR, médias) | Estratégias e indicadores | n/a | **Não** | **rejeitar** | Indicadores técnicos tradicionais proibidos. |
| A14 | `aurum/data/dukascopy.py`, `macro.py`, `calendar.py` | Dados de ouro e macro | n/a | Não (XAUUSD) | **rejeitar** | Dados e calendário do robowin vêm do MT5/B3. |
| A15 | `aurum/rl/*`, `models/regime.py`, `models/volatility.py` | RL, HMM, GARCH | Parcial: `rl/`, `modelos/har.py` | Parcial | **rejeitar** | Fora do escopo desta rodada; já há RL de execução e HAR. |
| A16 | `SECURITY.md` (segredos só em env, dry-run padrão, magic number, kill switch persistente, LLM nunca contorna risco) | Postura de segurança | **Já existe**: `ia/segredos.py`, DEMO-only, magic por braço, `ExecucaoRealNaoAutorizada` | Sim | já existe | Registrado como confirmação da arquitetura. |
| A17 | `SPEC.md` §0 (point-in-time ou nada; um simulador; alfa ≠ dimensionamento ≠ risco) | Princípios | **Já existe** (`t_disponivel`, `backtest/vazamento.py`, orquestrador único) | Sim | já existe | Reforçou o desenho do teste de vazamento da reflexão. |

### TradingAgents (`TauricResearch/TradingAgents`, Apache-2.0)

| ID | Origem | O que faz | Já existe no robowin? | Adaptável sem indicadores? | Decisão | Motivo e onde ficou |
|---|---|---|---|---|---|---|
| T1 | `agents/researchers/bull_researcher.py`, `bear_researcher.py`, `managers/research_manager.py`, `graph/conditional_logic.py` | Debate altista × baixista em rodadas e gestor que decide | Não | Sim, **se** os pesquisadores virem só o dossiê | **adaptar** | `decisao/bracos/debate.py`: dois pesquisadores (esquema `ArgumentoDebate`, evidências que citam campos do dossiê), 1 ou 2 rodadas. |
| T2 | `agents/risk_mgmt/aggressive|neutral|conservative_debator.py`, `managers/portfolio_manager.py` | Debate de risco em três perfis e decisão final | Não | Sim | **adaptar** | Três pareceres (`ParecerRisco`) + sintetizador que cumpre o papel **proponente** e devolve uma `PropostaIA` comum (mesmos validadores, revisor opcional, **veto do risco**). Veto unânime opcional dos três pareceres. |
| T3 | `agents/trader/trader.py` | Plano do trader a partir da tese | Já existe: proponente (`ia/prompts`, `bracos/llm.py`) | Sim | já existe | O sintetizador faz esse papel. |
| T4 | `agents/analysts/market_analyst.py`, `sentiment_analyst.py`, `news_analyst.py`, `fundamentals_analyst.py`, `tools.py`, `dataflows/*` | Analistas com ferramentas: indicadores (SMA/EMA/MACD/RSI/Bollinger/ATR/VWMA), Reddit/StockTwits, notícias, SEC, yfinance | n/a | **Não** | **rejeitar** | Indicadores proibidos; internet e texto de notícia violam a anonimização do dossiê (as notícias entram só como categoria, direção e intensidade). |
| T5 | `memory/log.py`, `reflection.py`, `settlement.py` | Registra a decisão como pendente, liquida depois que a janela de preço fechou e escreve uma reflexão | Parcial: `memoria` de 5 operações no dossiê; sem reflexão | Sim | **adaptar** | `ia/reflexao.py`: `MemoriaReflexiva` (pendente → em liquidação → liquidada/descartada), atraso de liquidação, `t_disponivel_ns = t + latência da IA`, `reflexoes_ate(t)`. Sem benchmark/alpha (1 contrato, sem benchmark): o resultado é em pontos. |
| T6 | `memory/log.py::get_past_context(as_of=...)` (#1251) | Só injeta lições cujo resultado já era conhecido na data | Não | Sim | **adotar** | É exatamente a regra anti-vazamento de `MemoriaReflexiva.enriquecer/reflexoes_ate`; teste de perturbação do futuro em `tests/unit/test_ia_reflexao.py`. |
| T7 | `agents/structured.py` (`bind_structured`, queda para texto livre) | Saída estruturada com fallback | Já existe a saída estruturada (Pydantic, `ia/esquemas.py`) | Sim | **rejeitar o fallback** | Cair para texto livre contraria a falha segura: resposta inválida **bloqueia a entrada**. Mantida a estrutura; adotada a instrução `NO_EXTERNAL_TOOLS` nos prompts novos. |
| T8 | `graph/*` (LangGraph, checkpointer) | Orquestração | Já existe: orquestrador próprio | n/a | **rejeitar** | Dependência pesada (langchain/langgraph) sem ganho; o orquestrador já tem prazos, orçamento e cache. |
| T9 | `llm_clients/capabilities.py`, `model_catalog.py` | Tabela de capacidades e catálogo de modelos | Já existe: `ia/catalogo.py` (`saida_estruturada`, preço, corte, `fixavel`) | Sim | já existe | Modelos listados lá (GLM, Qwen...) fora do escopo. |
| T10 | `agents/rating.py`, `portfolio.py`, `backtest.py`, `reporting.py` | Rating de 5 níveis (Buy..Sell), carteira, relatórios | Já existe equivalente próprio | Não (ações de carteira) | **rejeitar** | Nossas ações são COMPRAR/VENDER/MANTER/ENCERRAR/NAO_OPERAR com 1 contrato. |

### claude-trader (`el-bach/claude-trader`, MIT, sem testes)

| ID | Origem | O que faz | Já existe? | Adaptável? | Decisão | Motivo |
|---|---|---|---|---|---|---|
| C1 | `agents/tracker.py` | Desempenho por agente a partir dos *deals* do MT5 (comentário `APEX_<agente>`) | Já existe: magic por braço no OMS, tabelas `trades`/`decisoes`/`equity`, relatório por braço, painel | Sim | já existe | Ler o MT5 direto para atribuir resultado é frágil; aqui o resultado nasce no OMS. |
| C2 | `agents/monitor.py` (+ `*_watch.py`) | Vigia posição a cada 10 s; duas IAs decidem HOLD/MOVE_SL/CLOSE e **executam no MT5** | Já existe o efeito legítimo: `MANTER` com `stop_pontos` e `ENCERRAR` passam pelo roteador e pelo risco | Parcial | **rejeitar** | IA executando direto, sem veto de risco, viola a regra 3. |
| C3 | `agents/manager.py` (constantes de risco: perda diária, `MIN_CONFIDENCE`, `MIN_RR`, perdas seguidas) | Limites por variável de ambiente | Parcial: `ConfigRisco` (perda dia, perdas seguidas...) travada por hash | Sim | **rejeitar** | `MIN_CONFIDENCE` e `MIN_RR` não têm evidência aqui; mexer em `config/risco.toml` exige nova aprovação por hash. O acerto de equilíbrio já é calculado em `avaliacao/estatistica.py`. |
| C4 | `mt5_executor.py` | `order_send` direto em vários pontos | Não deve existir | Não | **rejeitar** | Único `order_send` do projeto: `mt5/execucao_demo.py` (teste AST). |
| C5 | `agents/strategist.py`, `*_watch.py`, `backtest.py`, `dashboard_*`, Telegram, pares FX | Estratégia/relatórios para EURUSD, GBPUSD, USDJPY, ouro | n/a | Não | **rejeitar** | Outros ativos, chaves de terceiros (DeepSeek), sem testes. |

### trader-skill (`Jakeschincariol/trader-skill`, MIT)

| ID | Origem | O que faz | Já existe? | Adaptável? | Decisão | Motivo e onde ficou |
|---|---|---|---|---|---|---|
| S1 | `trader-risk/check.py` | Portão PASS/BLOCK por regra (formato, ativo, tamanho, posição, caixa, duplicata, limite diário), com motivos; checa o lote em ordem como se cada PASS tivesse sido executado | **Já existe**: `risco/motor.py` devolve `DecisaoRisco` com motivos; `reservar_pendente`; duplicidade no OMS | Sim | já existe | — |
| S2 | `check.py::fingerprint` + `trader-broker/ticket.py` | O broker só mostra como pronta a ordem cujo hash passou no risco | **Não** | Sim | **adaptar** | `execucao/aprovacao.py`: `impressao_ordem` (hash dos campos da ordem), `RegistroAprovacoes`; `Roteador(exigir_aprovacao=True)` recusa **entrada** cuja ordem difere da aprovada pelo risco (saídas nunca esperam). Ligado no orquestrador, no executor do backtest, no espelho DEMO e na ponte do market making. |
| S3 | `trader-broker` ("nenhuma ordem sem o seu sim"; paper por padrão) | Confirmação humana | **Já existe**: DEMO apenas, `--confirmo-conta-demo`, `[geral].espelhar` vazio, `ExecucaoRealNaoAutorizada` | Sim | já existe | Nenhuma ordem real existe nesta versão. |
| S4 | `trader-news/news.py` (RSS Yahoo + 8-K da SEC por ticker, dedup) | Monitor de notícias de ações americanas | Já existe `noticias/` (calendário, dedup, classificador por categoria) | Não (EUA/SEC) | **rejeitar** | Específico de EUA; e o texto da notícia nunca vai ao LLM. |
| S5 | `trader-alerts/alerts.py` (preço, stop, sino de abertura, calendário NYSE) | Alertas | Stop e zeragem são do guardião e do motor; calendário B3 em `core/calendario_b3.py` | Parcial | **rejeitar** | Alerta de preço manual não agrega a um robô com stop determinístico; calendário é da B3. |
| S6 | `trader-budget/size.py` (orçamento, colchão de caixa, teto por posição) | Dimensionamento | `contratos_max = 1` fixo | Não | **rejeitar** | Com 1 contrato não há o que dimensionar; margem de day trade segue como `a_confirmar` em `config/instrumentos.toml`. |
| S7 | `trader-tax/lots.py` (lotes FIFO/HIFO, *wash sale*, colheita de prejuízo) | Controle de lotes | **Já existe** o que importa: `avaliacao/impostos.py` (compensação de prejuízo de day trade, IRRF 1%, DARF 6015, mínimo) | Não (lotes/wash sale são regras dos EUA) | **rejeitar** | Day trade em contrato futuro não tem lotes nem *wash sale*. |
| S8 | `trader-copycat`, `trader-pelosi`, `trader-analyst`, `trader-screener`, `trader-rebalance` | 13F, Congresso, fundamentos, triagem, rebalanceamento | n/a | Não | **rejeitar** | Ações dos EUA de longo prazo via SEC/House Clerk. |

## Linha por repositório: o que foi efetivamente adaptado no código

| Repositório | Licença | Adaptado no código (arquivos novos ou alterados) | Só inspiração / rejeitado |
|---|---|---|---|
| **Aurum** | MIT | `avaliacao/uso_ia.py` (contabilidade de uso de IA); `avaliacao/robustez.py` (PBO por CSCV, MinTRL); `risco/cauda.py` (VaR/ES histórico e Kupiec, só monitoramento); `avaliacao/monitoramento.py` (PSI e custo de execução real × modelo, ainda não ligado ao vivo); `EnvelopeAgente` em `decisao/bracos/debate.py` (governança dos agentes) | Estratégias, indicadores, dados de ouro, CPCV, `DecisionPolicy` overlay, agentes criados em runtime, webhooks |
| **TradingAgents** | Apache-2.0 | `decisao/bracos/debate.py` (+ `fabrica.py`, `llm.py`, `bracos/__init__.py`, `simulacao.py`): braço de debate; `ia/reflexao.py` (+ `orquestrador.py`, `dossie.py`, `simulacao.py`): memória reflexiva com liquidação tardia; esquemas e 7 prompts novos (`config/prompts/*_v1.md`) | Analistas com indicadores/internet/sentimento, LangGraph, rating de 5 níveis, fallback para texto livre |
| **claude-trader** | MIT | **Nada em código.** | Tracker (já existe pelo OMS), monitor com IA executando direto, `order_send` direto, FX, Telegram, sem testes |
| **trader-skill** | MIT | `execucao/aprovacao.py` + `execucao/roteador.py`: impressão da ordem aprovada pelo risco, exigida nas entradas | News/8-K, alertas, orçamento, lotes e IR dos EUA, copycat, Congresso |
| **day-trader-skill** | — | (a cargo de outro agente; fora desta matriz) | — |

## O que ficou pronto para ligar, e o que ainda não está ligado

* **Ligado ao fluxo**: debate (via `[[bracos]]`, desligado por padrão), reflexão (via `[reflexao]`, desligada por
  padrão), uso de IA, cauda de perdas e PBO no `robowin avaliar`, impressão da ordem aprovada em todos os roteadores.
* **Biblioteca sem ligação ao vivo**: `avaliacao/monitoramento.py` (PSI e slippage) e `min_track_record_length`.
* **`MotorBacktest` (`backtest/motor.py`)**: segue com a memória só de resultados numéricos; a reflexão está ligada ao
  `Orquestrador` (o fluxo de `robowin simular`/`nucleo`).

## Como ligar e medir

```toml
# config/robowin.toml
[[bracos]]
id = "debate_cond"
tipo = "debate"
proponente = "sintetizador"       # papéis [ia.papeis.*]: pesquisador, debatedor_risco, sintetizador
modo = "condicional"              # o debate só existe neste modo (latência)

[reflexao]
ativo = true
atraso_liquidacao_s = 300
```

O braço entra no manifesto (hipótese **H4** e um modelo por etapa), nos relatórios de latência e custo, na comparação
com o controle e na correção de Holm, como qualquer outro braço. **Debater e refletir são hipóteses a medir; nenhum
resultado aqui sugere melhora.** Custos reais dependem de chaves e do comportamento dos modelos (itens **DU**).
