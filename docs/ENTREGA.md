# ENTREGA — matriz de rastreabilidade do pedido original

> **SOMENTE SIMULAÇÃO / conta DEMO. O envio de ordens reais não existe nesta versão.** Este documento liga cada
> item do pedido original a (a) onde está no repositório, (b) a evidência que o sustenta e (c) um **status
> honesto**. Todo caminho citado existe (um teste confere: `tests/unit/test_verificacao_entrega.py`).

## Resumo honesto (leia primeiro)

**100% verificável em software, aqui (Linux, sem terminal):** código importável (181 módulos), estilo (ruff), linter
estático do `.mq4`, cadeia de auditoria com detecção de adulteração, catálogo de modelos, perfis de ativo/bolsa
(só o WIN confirmado; os demais **bloqueiam** a operação), política de risco travada por hash, `order_send` único
(AST), ausência de números de ativo no código, `diagnostico` e gateway/cliente contra o **dublê** MT5 e MT4, painel
(6 rotas, mais captura em Chromium real), backtest cronológico sem vazamento, avaliação estatística, simulação
completa com provedor de IA **falso**. Reproduza com `uv run robowin verificar` (< 60 s) e `uv run pytest`.

**Depende do usuário (nunca poderá ser provado aqui):** terminal MT5/MT4 reais e conta demo (`robowin diagnostico`
sem `--duble`); **compilar** `mql4/RobowinBridge.mq4` no MetaEditor (o linter não é compilador); chaves
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` e comportamento real dos modelos; tarifas, margem, horários e vencimentos
(perfis `a_confirmar`, `docs/10-pendencias.md`); **aprovação de risco** por pessoa
(`config/aprovacoes/risco-v1.toml` está com `responsavel = "pendente"`); regras da corretora; conectividade com os
hosts críticos (o `verificar` não usa rede).

**O que passa a significar "100%".** Como o que falta provar só existe no Windows do usuário, o "100%" **não** é mais
"tudo verde aqui": é **aceitação rodada no Windows do usuário** (`robowin aceitacao --confirmo-conta-demo`, passo a passo
em `docs/12-aceitacao-no-windows.md`), com **relatório sem nenhuma etapa REPROVADA e com as etapas aplicáveis
APROVADAS**. Etapa sem pré-requisito (pregão fechado, sem chave de API, perfil `a_confirmar`, plataforma errada) é
**NÃO VERIFICÁVEL** com o motivo e nunca conta como aprovada. Contra o dublê (`--duble`) o comando só prova que o
roteiro funciona e **não vale como aceitação**. Enquanto o relatório real não existir, os itens **DU** abaixo seguem **DU**.

**Lucratividade nunca é provada.** Dados sintéticos e dublês não são mercado; resultado de simulação não prova ganho
futuro; poucas pessoas físicas lucram com day trade (alerta da CVM). O pedido de "meta de acerto" é tratado como
**hipótese a testar** com amostra e incerteza declaradas (item 1), jamais como promessa.

**Legenda de status:** **VA** = Validado aqui · **VD** = Validado só contra dublê · **NV** = Não verificável aqui ·
**DU** = Depende do usuário.

## Como reproduzir

| O quê | Comando |
|---|---|
| Autoverificação rápida (matriz APROVADO / REPROVADO / NÃO VERIFICÁVEL AQUI) | `uv run robowin verificar` (`--json` para máquina) |
| Suíte completa (~10 min) | `uv run --extra dev pytest -q` |
| Aceitação no terminal DEMO (Windows do usuário) | `uv run robowin aceitacao --confirmo-conta-demo` (ou `scripts/windows/validar_tudo.ps1 -ConfirmoContaDemo`); ensaio sem terminal: `uv run robowin aceitacao --duble --confirmo-conta-demo` |
| Lint | `uv run ruff check .` e `uv run python scripts/lint_mql4.py mql4/RobowinBridge.mq4` |
| Sessão completa sintética | `uv run robowin simular --n-ticks 3000` e `uv run robowin auditoria --dir <saida>/auditoria` |
| Painel | `uv run robowin painel-demo` e `uv run robowin painel` (capturas: `docs/capturas/README.md`) |

## Pedido 1 — Meta de acerto, amostra, incerteza e métricas

| Item do pedido | Onde está | Evidência | Status |
|---|---|---|---|
| Meta de acerto tratada como hipótese, com IC e SPRT | `src/robowin/avaliacao/estatistica.py` (`ic_wilson`, `ic_clopper_pearson`, `teste_proporcao_unilateral`, `sprt_bernoulli`) | `tests/unit/test_aval_estatistica.py` | VA |
| Amostra mínima e período (n pelo poder / MDE) | `src/robowin/avaliacao/estatistica.py` (`tamanho_amostra_proporcao`, `n_proporcao_poder`, `n_expectativa`); `docs/04-testes-e-criterios.md` | `tests/unit/test_aval_estatistica.py` | VA |
| Incerteza (bootstrap por pregão, DSR, Holm) | `src/robowin/avaliacao/estatistica.py` (`bootstrap_por_pregao`, `deflated_sharpe`), `src/robowin/avaliacao/comparacao.py` (`holm`) | `tests/unit/test_aval_estatistica.py`, `tests/unit/test_aval_metricas_comparacao.py` | VA |
| Lucro líquido (custos, tarifas) e impostos | `src/robowin/execucao/custos.py`, `src/robowin/avaliacao/impostos.py` | `tests/unit/test_aval_impostos_relatorio.py`, `tests/unit/test_exec_simulador.py` | VA (tarifas/IR: DU) |
| Ganho/perda médios, expectativa, fator de lucro, drawdown | `src/robowin/avaliacao/metricas.py` (`resumo`, `fator_de_lucro`, `drawdown`) | `tests/unit/test_aval_metricas_comparacao.py` | VA |
| Relatório (Markdown/JSON) | `src/robowin/avaliacao/relatorio.py`; `uv run robowin avaliar` | `tests/unit/test_aval_impostos_relatorio.py` | VA |
| Lucratividade real / acerto futuro | — | — | **NV** (nunca provado; só dados reais por etapas, `docs/04-testes-e-criterios.md`) |

## Pedido 2 — Claude e GPT, versões fixadas, braços A/B/C

| Item do pedido | Onde está | Evidência | Status |
|---|---|---|---|
| Versões de modelo fixadas por experimento (preço, corte, `fixavel`) | `config/modelos.toml`, `src/robowin/ia/catalogo.py`, manifesto em `src/robowin/decisao/simulacao.py` (`montar_manifesto`) | `tests/unit/test_ia_catalogo.py`; `robowin verificar` (catálogo) | VA |
| GPT revisa coerência, qualidade de dados e regras (só entradas) | `src/robowin/decisao/bracos/claude_gpt.py`, `src/robowin/ia/papeis.py`, `config/prompts/revisor_v1.md` | `tests/unit/test_decisao_bracos.py`, `tests/unit/test_ia_validadores.py` | VD (provedor falso) |
| Concordância não é prova (sem tratar acordo Claude/GPT como validação) | `docs/09-modelos-de-ia.md`; ganho medido por `src/robowin/avaliacao/comparacao.py` (`valor_do_veto`) | `tests/unit/test_aval_metricas_comparacao.py` | VA |
| Braços A (sem LLM) / B (Claude) / C (Claude+GPT) e controles | `src/robowin/decisao/bracos/quant.py`, `src/robowin/decisao/bracos/claude.py`, `src/robowin/decisao/bracos/claude_gpt.py`, `src/robowin/decisao/bracos/controles.py`, `src/robowin/decisao/orquestrador.py` | `tests/unit/test_decisao_bracos.py`, `tests/unit/test_decisao_orquestrador.py`, `tests/unit/test_decisao_e2e.py` | VD |
| Justificativa pelo benefício líquido de custo e latência | `src/robowin/avaliacao/comparacao.py` (`beneficio_liquido_llm`), `src/robowin/ia/orcamento.py` | `tests/unit/test_aval_metricas_comparacao.py`, `tests/unit/test_ia_orcamento_cache.py` | VA (cálculo); **DU** (custos e ganho reais exigem chaves) |
| Comportamento real dos modelos (qualidade, variância, latência) | `uv run robowin ia testar`, `uv run robowin ia latencia` | `tests/unit/test_ia_prompts_regressao_cli.py` (só com provedor falso) | **DU** (chaves) |

## Pedido 3 — Latência, separação de responsabilidades e falha segura

| Item do pedido | Onde está | Evidência | Status |
|---|---|---|---|
| Latência p50/p95/p99 | `src/robowin/ia/cli_cmds.py` (`robowin ia latencia`), `src/robowin/backtest/latencia_injetada.py` | `tests/unit/test_backtest_latencia.py` | VD (latência real: DU) |
| Separação IA / risco / execução | `src/robowin/decisao/`, `src/robowin/risco/`, `src/robowin/execucao/`, `docs/02-arquitetura.md` | `tests/unit/test_decisao_comum.py`, `tests/unit/test_risco_motor.py`, `tests/unit/test_exec_roteador.py` | VA |
| Alternativa: plano condicional | `src/robowin/decisao/plano_condicional.py`, `src/robowin/decisao/dsl_plano.py`, `src/robowin/decisao/gatilhos.py` | `tests/unit/test_decisao_plano_validade_gatilhos.py` | VA |
| Validade do sinal e tempo máximo | `src/robowin/decisao/validade.py` (`Prazos`, `validar_frescor_entrada`) | `tests/unit/test_decisao_plano_validade_gatilhos.py`, `tests/unit/test_revisao_decisao.py` | VA |
| Bloqueio por atraso, resposta inválida ou divergente | `src/robowin/decisao/validade.py`, `src/robowin/ia/validadores.py` | `tests/unit/test_ia_validadores.py`, `tests/unit/test_decisao_orquestrador.py` | VD |
| Proteção (stop/alvo/zeragem) independente de IA e de rede | `src/robowin/decisao/saidas.py`, `src/robowin/mt5/guardiao.py`, `src/robowin/mt5/gateway.py`, `src/robowin/risco/kill_switch.py` | `tests/unit/test_mt5_guardiao.py`, `tests/unit/test_mt5_gateway.py`, `tests/unit/test_risco_kill.py`, `tests/integracao/test_e2e_espelho.py` | VD (terminal real: DU) |

## Pedido 4 — Risco

| Item do pedido | Onde está | Evidência | Status |
|---|---|---|---|
| 1 contrato, demo | `src/robowin/risco/politica.py` (`TETO_CONTRATOS_V1`), `src/robowin/mt5/execucao_demo.py` (guarda demo) | `tests/unit/test_risco_motor.py`, `tests/unit/test_mt5_execucao.py`; `robowin verificar` (gateway: conta real recusada) | VD |
| Limites (operação/dia/semana/drawdown), política travada por hash | `config/risco.toml`, `config/aprovacoes/risco-v1.toml`, `src/robowin/risco/politica.py` | `tests/unit/test_risco_motor.py`; `robowin verificar` (política de risco) | VA (hash); **DU** (aprovação por pessoa: hoje `pendente`) |
| Stops obrigatórios e folga de slippage | `src/robowin/risco/motor.py`, `src/robowin/mt5/guardiao.py` | `tests/unit/test_risco_invariantes.py`, `tests/unit/test_mt5_guardiao.py` | VD |
| Ordens duplicadas (idempotência) | `src/robowin/execucao/oms.py`, `src/robowin/core/ids.py` | `tests/unit/test_exec_oms.py`, `tests/unit/test_mt5_execucao.py` | VD |
| Reconciliação | `src/robowin/execucao/reconciliacao.py` | `tests/unit/test_exec_reconciliacao.py` | VD |
| Desconexão (feed parado, sem heartbeat) | `src/robowin/mt5/gateway.py`, `src/robowin/mt5/guardiao.py`; `docs/07-risco-e-operacao.md` | `tests/unit/test_mt5_gateway.py`, `tests/unit/test_mt5_revisao.py` | VD |
| Botão de emergência / kill switch | `src/robowin/risco/kill_switch.py`, `src/robowin/painel/app.py` (`/acoes/emergencia`), `uv run robowin emergencia` | `tests/unit/test_risco_kill.py`, `tests/unit/test_painel_app.py`, `docs/capturas/kill-switch-aguardando.png` | VA (painel) / VD (gateway) |
| Encerramento antes do fim do pregão | `src/robowin/risco/tempo.py`, `config/instrumentos.toml` (sessão/zeragem) | `tests/unit/test_risco_motor.py` | VD (horário real da corretora: DU) |
| Slippage | `src/robowin/execucao/preenchimento.py`, `src/robowin/execucao/simulador.py`, `src/robowin/risco/estresse.py` | `tests/unit/test_exec_simulador.py`, `tests/unit/test_risco_estresse_anti.py` | VD (slippage real: DU) |
| Proibição de martingale | `src/robowin/risco/invariantes.py` (`inv_sem_martingale`), `src/robowin/risco/motor.py` | `tests/unit/test_risco_invariantes.py` | VA |
| Só um `order_send`, na guarda demo | `src/robowin/mt5/execucao_demo.py` | `tests/unit/test_mt5_ast.py`; `robowin verificar` (estático) | VA |

## Pedido 5 — Aprendizado controlado

| Item do pedido | Onde está | Evidência | Status |
|---|---|---|---|
| Registro de operações/tentativas | `src/robowin/dados/banco.py`, `src/robowin/core/esquema_banco.py`, `src/robowin/auditoria/cadeia.py` | `tests/unit/test_dados_armazenamento.py`; `robowin verificar` (auditoria) | VA |
| Memória × parâmetros × treinamento (separados) | `docs/08-aprendizado-controlado.md`, `src/robowin/modelos/walkforward.py` | `tests/unit/test_modelos_sinal.py`, `tests/unit/test_modelos_rotulagem.py` | VA |
| Validação fora da amostra (OOS), holdout uma vez | `src/robowin/modelos/microestrutura.py` (`CriterioOOS`), `src/robowin/backtest/vazamento.py` | `tests/unit/test_backtest_vazamento.py`, `tests/unit/test_modelos_sinal.py` | VA (dados sintéticos) |
| Versionamento e reversão | `src/robowin/modelos/registro.py`, `src/robowin/modelos/persistencia.py`, `config/prompts/` | `tests/unit/test_modelos_registro.py`, `tests/unit/test_modelos_persistencia.py` | VA |
| Regras de risco imutáveis em execução | `src/robowin/risco/politica.py` (hash), `src/robowin/risco/invariantes.py` | `tests/unit/test_risco_invariantes.py` | VA |
| Ganho real do aprendizado | — | — | **NV** (exige dados reais) |

## Pedido 6 — Etapas, critérios e custos

| Item do pedido | Onde está | Evidência | Status |
|---|---|---|---|
| Etapas 0–5 e portões (avançar / interromper / reduzir / rejeitar) | `docs/04-testes-e-criterios.md`, `docs/06-roteiro.md`, `src/robowin/avaliacao/estatistica.py` (`sprt_*`) | `tests/unit/test_aval_estatistica.py` | VA (regras) / **DU** (executar as etapas reais, ~4–9 meses) |
| Custos (API, infra, corretagem) | `docs/05-custos.md`, `src/robowin/ia/orcamento.py`, `config/modelos.toml` | `tests/unit/test_ia_orcamento_cache.py` | VA (modelo) / **DU** (tarifas, preços, orçamento reais) |

## Pedido 7 — Primeira entrega (documentos)

| Item do pedido | Onde está | Evidência | Status |
|---|---|---|---|
| Viabilidade | `docs/01-viabilidade.md` | revisão humana | VA |
| Arquitetura | `docs/02-arquitetura.md` | `robowin verificar` (importabilidade) | VA |
| Dados, integrações e infraestrutura | `docs/03-dados-integracoes-infra.md` | — | VA (texto); fontes: **DU** (`config/fontes.toml`, todas `verificado=false`) |
| Plano de testes e critérios | `docs/04-testes-e-criterios.md` | suíte `tests/` | VA |
| Custos | `docs/05-custos.md` | — | VA (estimativa); **DU** (valores reais) |
| Roteiro | `docs/06-roteiro.md` | — | VA |
| Riscos/operação, aprendizado, modelos de IA, pendências, validação em sandbox | `docs/07-risco-e-operacao.md`, `docs/08-aprendizado-controlado.md`, `docs/09-modelos-de-ia.md`, `docs/10-pendencias.md`, `docs/11-validacao-sandbox.md` | — | VA |

## Decisão definitiva — itens 1 a 9

| # | Item | Onde está | Evidência | Status |
|---|---|---|---|---|
| 1 | Conector Python-MT5 | `src/robowin/mt5/conector.py`, `src/robowin/mt5/duble.py`, `scripts/windows/instalar.ps1` | `tests/unit/test_mt5_conector.py`, `tests/unit/test_mt5_duble.py`, `tests/integracao/test_mt5_integracao.py`; `robowin verificar` (diagnóstico) | VD; **NV** (pacote `MetaTrader5` só no Windows) / **DU** (terminal real) |
| 2 | Contrato vigente | `src/robowin/mt5/contrato.py`, `src/robowin/core/calendario_b3.py` | `tests/unit/test_mt5_contrato.py` | VD (vencimentos: DU) |
| 3 | Coleta e armazenamento | `src/robowin/mt5/coleta.py`, `src/robowin/dados/ticks.py`, `src/robowin/dados/qualidade.py` | `tests/unit/test_mt5_coleta.py`, `tests/unit/test_dados_armazenamento.py`, `tests/unit/test_dados_qualidade_sintetico.py` | VD |
| 4 | Motor de IA | `src/robowin/ia/`, `src/robowin/decisao/` | `tests/unit/test_ia_provedores.py`, `tests/unit/test_decisao_e2e.py` | VD (provedor falso); **DU** (chaves) |
| 5 | Backtest sem vazamento | `src/robowin/backtest/vazamento.py`, `src/robowin/backtest/motor.py`, `src/robowin/backtest/anonimizacao.py` | `tests/unit/test_backtest_vazamento.py`, `tests/unit/test_backtest_motor.py` | VA (sintético) |
| 6 | Operação simulada | `src/robowin/decisao/simulacao.py`, `uv run robowin simular` | `tests/unit/test_decisao_e2e.py`; execução manual na criação deste documento: `robowin simular --n-ticks 3000` + `robowin auditoria` (1.677 registros íntegros) | VA (sintético) |
| 7 | Risco independente | `src/robowin/risco/`, `src/robowin/mt5/guardiao.py` | `tests/unit/test_risco_*.py`, `tests/unit/test_mt5_guardiao.py` | VA / VD |
| 8 | Painel em pt-BR | `src/robowin/painel/` | `tests/unit/test_painel_app.py`, `tests/unit/test_painel_grafico_ptbr.py`, `docs/capturas/README.md` (84 verificações em Chromium) | VA (só Chromium; ver `docs/capturas/README.md`) |
| 9 | Testes, logs e documentação | `tests/`, `src/robowin/auditoria/`, `docs/`, `.github/workflows/ci.yml` | `robowin verificar`; `uv run pytest` | VA (CI Windows: roda no GitHub, não aqui) |

## Componentes adicionais

| Componente | Onde está | Evidência | Status |
|---|---|---|---|
| RL de execução | `src/robowin/rl/` (`ambiente.py`, `q_tabular.py`, `bandit.py`), `uv run robowin rl demo` | `tests/unit/test_rl_ambiente.py`, `tests/unit/test_rl_agentes.py` | VA (sintético; sem prova de ganho) |
| Market making | `src/robowin/mm/` (`avellaneda_stoikov.py`, `ponte_demo.py`), `uv run robowin mm demo` | `tests/unit/test_mm_estrategia.py`, `tests/unit/test_mm_ponte_demo.py` | VA (sim.) / VD (demo); experimental |
| Microestrutura | `src/robowin/modelos/microestrutura.py`, `src/robowin/features/` | `tests/unit/test_modelos_sinal.py`, `tests/unit/test_feat_pipeline.py` | VA (sintético) |
| StatArb | `src/robowin/statarb/`, `uv run robowin statarb demo` | `tests/unit/test_statarb_m6.py` | VA (sintético) |
| NLP/LLM de notícias | `src/robowin/noticias/`, `config/prompts/nlp_v1.md`, `config/fontes.toml` | `tests/unit/test_noticias_calendario_classificador.py`, `tests/unit/test_noticias_fontes_dedup.py` | VD; **DU** (fontes e chaves) |

## MT4, ativos/bolsas, espelhamento, modelo persistente, IA plugável

| Item | Onde está | Evidência | Status |
|---|---|---|---|
| MT4 (ponte EA, hedging, sem livro) | `mql4/RobowinBridge.mq4`, `mql4/PROTOCOLO.md`, `src/robowin/mt5/conector_mt4.py`, `src/robowin/mt5/duble_mt4.py`, `src/robowin/mt5/capacidades.py` | `tests/unit/test_mt4_ponte.py`, `tests/unit/test_mt4_hedging.py`, `tests/unit/test_mt4_gateway_capacidades.py`, `tests/unit/test_lint_mql4.py`; `robowin verificar` (lint, diagnóstico MT4) | VD; **NV/DU**: `.mq4` **nunca compilado** (`docs/11-validacao-sandbox.md`) |
| Ativos configuráveis | `config/instrumentos.toml`, `src/robowin/core/instrumentos.py` | `tests/unit/test_ativo_configuravel.py`, `tests/estaticos/test_sem_constantes_de_ativo.py` | VA (só o ativo confirmado; os demais **bloqueados** até confirmação, DU) |
| Bolsas configuráveis | `config/bolsas.toml`, `src/robowin/core/bolsas.py` | `tests/unit/test_ativo_configuravel.py` | VA (B3); NYSE/NASDAQ/LSE/CME são modelos `a_confirmar` (DU) |
| Espelhamento na demo | `src/robowin/decisao/espelho.py` | `tests/unit/test_espelho_demo.py`, `tests/unit/test_trava_espelho.py`, `tests/integracao/test_e2e_espelho.py` | VD; **DU** (conta demo real) |
| Modelo persistente | `src/robowin/modelos/persistencia.py`, `uv run robowin treinar` | `tests/unit/test_modelos_persistencia.py` | VA (artefato com hash, recusa de outro ativo) |
| IA plugável | `src/robowin/ia/provedores/` (Anthropic, OpenAI, Google, Ollama, OpenAI-compatível, falso), `config/robowin.toml` | `tests/unit/test_ia_provedores.py`, `tests/unit/test_ia_catalogo.py` | VD (rede/chaves: DU) |

## Aceitação no Windows do usuário (Fase 5)

| Item | Onde está | Evidência | Status |
|---|---|---|---|
| Comando `robowin aceitacao` (A1–A11, somente DEMO, relatório `aceitacao-<data>.md/.json`) | `src/robowin/aceitacao/` (`etapas.py`, `matriz.py`, `relatorio.py`, `cli_cmds.py`) | `tests/unit/test_aceitacao.py` (roda inteira contra `DubleMT5` e `DubleMT4`; sem a flag 0 ordens; conta real/contest/servidor sem DEMO/login errado recusados com 0 envios; perfil `a_confirmar` bloqueia ordens; nenhum segredo no relatório; REPROVADO muda o código de saída) | VD |
| Toda ordem da aceitação passa por `ExecucaoDemo` | `src/robowin/mt5/execucao_demo.py` | `tests/unit/test_mt5_ast.py` (continua verde com o módulo novo) | VA |
| Compilar o EA no MetaEditor | `scripts/windows/compilar_ea.ps1` | — (escrito **sem poder ser executado** aqui) | **DU** |
| Validação completa + zip sem segredos | `scripts/windows/validar_tudo.ps1` | — (idem) | **DU** |
| Passo a passo para o usuário | `docs/12-aceitacao-no-windows.md` | revisão humana | VA |
| **Aceitação real** (terminal, conta demo, latência de ordens e de IA, kill switch no terminal real) | `docs/12-aceitacao-no-windows.md` | relatório do usuário, sem REPROVADO | **DU** (é o que fecha o "100%") |

## O que continua não verificado (de `docs/10-pendencias.md` e `docs/11-validacao-sandbox.md`)

- **`docs/10-pendencias.md`:** confirmação escrita da corretora (robôs/API, corretagem zero, zeragem, margem, mensagens,
  profundidade do book, Stop Limit nativo, logins demo); API do Broadcast; chaves de API; ofício da grade de
  03/11/2026 e tarifação V4.8; política de mensagens da B3; políticas da Anthropic e da OpenAI; URLs/horários de
  `config/fontes.toml` (todas `verificado=false`); medir no Windows `order_check`, saída com ticket velho, horário de
  `copy_ticks`/deals e offset do servidor; preços de modelos. **MT4:** compilar e testar o EA em MT4 demo, API da
  biblioteca `mql-zmq`, lote mínimo, `MODE_TICKVALUE`, nível de stop, SL/TP na abertura, tipo de execução, histórico.
- **`docs/11-validacao-sandbox.md`:** o EA **não foi compilado**; o linter não resolve tipos/sobrecargas nem expande
  macros e tem tabela de API escrita de memória; só o MetaEditor/MT4 revelam limites de memória, permissões, build,
  `recv/send` e timers.
- **`docs/capturas/README.md`:** só Chromium (sem Firefox/WebKit/modo escuro); estado "Confirmado" do gateway não
  exercido ponta a ponta no navegador; tabelas largas rolam no celular; sem teste de acessibilidade/carga.
- **Windows real:** executado em 28/10/2026 sob Wine (Python 3.12 Windows): o wheel oficial `MetaTrader5` 5.0.6231 importa, as 20 funções usadas existem, `initialize()` sem terminal falha de forma tratada, 72 constantes conferem com o pacote real (`scripts/conferir_constantes_mt5.py`) e 205 de 208 testes passam (as 3 falhas são artefato do ambiente híbrido numpy 1.26 × scipy; detalhes em `docs/11-validacao-sandbox.md`). Isso NÃO inclui terminal MT5/MT4 instalado nem conta de corretora. A suíte completa em Windows nativo roda na CI (`.github/workflows/ci.yml`, job `windows`, ainda não executada); `robowin verificar` não roda no Windows.
