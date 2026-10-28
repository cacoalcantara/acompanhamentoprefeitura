# Fragmento de atribuição e licenças: Aurum, TradingAgents, claude-trader e trader-skill

> Fragmento para o orquestrador mesclar em `THIRD_PARTY_NOTICES.md` e `docs/13-inspiracoes.md` (esses dois arquivos
> **não** foram editados aqui). Texto em português com resumo em inglês. Detalhes das decisões: `docs/13b-estudo-aurum-tradingagents.md`.

## Declaração geral / General statement

**PT.** Os quatro repositórios abaixo foram clonados em modo somente leitura, **não foram executados nem instalados** e
nenhuma chave ou segredo foi copiado. Foram lidos para extrair **ideias e arquiteturas**. O código do robowin que se
inspira neles foi **escrito de forma independente** (nomes, tipos, textos e testes próprios, em português); **nenhum
arquivo, função ou trecho foi copiado ou traduzido linha a linha**. Por isso, **nenhum arquivo do robowin é, até onde
sabemos, "obra derivada" em sentido de licença**: o que existe é **inspiração conceitual**, listada abaixo. Mesmo assim,
por transparência, os projetos de origem e suas licenças são reconhecidos aqui. Se alguém entender que algum arquivo é
derivado, aplica-se o regime indicado em cada item (MIT: manter o aviso de copyright e a permissão; Apache-2.0: manter
os avisos, indicar que o arquivo foi **modificado/changed** e incluir o `NOTICE` do projeto, que **não existe** no
TradingAgents).

**EN.** The four repositories below were cloned read-only, never executed or installed, and no key or secret was copied.
They were read for ideas and architecture. The robowin code inspired by them is an independent reimplementation (own
names, types, prompts and tests, in Portuguese); no file, function or passage was copied or translated line by line.
To our knowledge no robowin file is a "derivative work" in the licensing sense; what exists is conceptual inspiration,
acknowledged here out of transparency.

---

## 1. Aurum (Aurum TradingBot)

* **Nome:** Aurum, "systematic gold (XAUUSD) research and trading" (repositório `tradingbot`)
* **URL:** https://github.com/zero-was-here/tradingbot (conforme o README do projeto)
* **Licença:** MIT
* **Aviso de copyright (exato, lido de `LICENSE`):**

```
MIT License

Copyright (c) 2025 zero-was-here
```

(O restante do arquivo é o texto padrão da licença MIT: permissão para usar, copiar, modificar, mesclar, publicar,
distribuir, sublicenciar e vender cópias, condicionada à inclusão do aviso de copyright e do aviso de permissão em
todas as cópias ou partes substanciais, e fornecida "NO ESTADO EM QUE SE ENCONTRA", sem garantias.)

* **Apenas inspiração conceitual** (nenhum código derivado), implementada de forma independente:
  * contabilidade de uso e custo de IA por agente (`aurum/agents/usage.py`) -> `src/robowin/avaliacao/uso_ia.py`;
  * PBO por CSCV e comprimento mínimo de histórico (`aurum/research/stats.py`) -> `src/robowin/avaliacao/robustez.py`;
  * VaR/ES histórico e teste de Kupiec como **monitoramento, nunca veto** (`aurum/risk/var.py`) -> `src/robowin/risco/cauda.py`;
  * PSI e custo de execução real contra o modelo (`aurum/live/monitor.py`) -> `src/robowin/avaliacao/monitoramento.py`;
  * envelope declarativo do que cada agente pode usar (`aurum/agents/tools.py`, `chief.py`) -> `EnvelopeAgente` em
    `src/robowin/decisao/bracos/debate.py`;
  * a postura de "ponto no tempo ou nada" e a honestidade sobre a ausência de vantagem estatística comprovada
    (README e `SPEC.md`) reforçaram desenhos que o robowin já tinha.
* **Código derivado:** nenhum.

## 2. TradingAgents

* **Nome:** TradingAgents: Multi-Agents LLM Financial Trading Framework (Tauric Research)
* **URL:** https://github.com/TauricResearch/TradingAgents (conforme o README do projeto)
* **Artigo / citação pedida pelos autores:** Yijia Xiao, Edward Sun, Di Luo, Wei Wang, "TradingAgents: Multi-Agents LLM
  Financial Trading Framework", 2025, arXiv:2412.20138 (https://arxiv.org/abs/2412.20138).
* **Licença:** Apache License 2.0
* **Aviso de copyright:** o arquivo `LICENSE` do repositório contém **apenas o texto padrão da Apache-2.0**; o apêndice
  traz o modelo **não preenchido**, exatamente:

```
Copyright [yyyy] [name of copyright owner]
```

  Ou seja, **o `LICENSE` não declara um titular de copyright preenchido**. O README atribui o projeto a "Tauric Research"
  (logotipo e organização `TauricResearch`) e pede a citação acima. **Não há arquivo `NOTICE`** no repositório.
  *(Se for preciso um aviso de titular, usar a atribuição "Tauric Research / autores do artigo acima" e confirmar com o projeto.)*
* **Apenas inspiração conceitual** (nenhum código ou texto de prompt copiado), implementada de forma independente:
  * debate entre pesquisador altista e baixista, debate de risco em três perfis (agressivo, neutro, conservador) e
    gestor que decide (`agents/researchers/*`, `agents/risk_mgmt/*`, `agents/managers/*`, `graph/conditional_logic.py`)
    -> `src/robowin/decisao/bracos/debate.py`, `src/robowin/ia/esquemas.py` e os prompts `config/prompts/debate_*`,
    `risco_*` e `sintetizador_v1.md` (textos novos, em português, que **proíbem** indicadores técnicos, internet e ferramentas);
  * registro da decisão como pendente, liquidação tardia do resultado e reflexão posterior, com filtro "só lições já
    conhecidas na data" (`memory/log.py`, `reflection.py`, `settlement.py`) -> `src/robowin/ia/reflexao.py`
    e o prompt `config/prompts/reflexao_v1.md`;
  * a instrução de usar só a evidência fornecida, sem ferramentas externas (`NO_EXTERNAL_TOOLS` em `agents/structured.py`)
    -> cláusula equivalente nos prompts novos.
* **Não usado:** analistas com indicadores técnicos (SMA/EMA/MACD/RSI/Bollinger/ATR/VWMA), dados da internet,
  sentimento, LangGraph, rating de cinco níveis, fallback para texto livre.
* **Código derivado:** nenhum. **Se algum arquivo vier a ser considerado derivado (Apache-2.0, seção 4):** indicar
  que foi **alterado (changed)**, manter os avisos de copyright/patente/marca/atribuição do original e incluir uma
  cópia da licença; como o repositório de origem **não tem `NOTICE`**, não há aviso de NOTICE a reproduzir.

## 3. claude-trader

* **Nome:** claude-trader ("APEX Capital AI")
* **URL:** https://github.com/el-bach/claude-trader (inferida do caminho do clone; os README apontam um
  `YOUR_USERNAME` genérico, **confirmar** antes de publicar)
* **Licença:** MIT
* **Aviso de copyright (exato, lido de `LICENSE`):**

```
MIT License

Copyright (c) 2026 Claude Trader
```

* **Observação de qualidade:** o projeto **não tem testes**; executa `order_send` diretamente no MT5 e põe a IA para
  mover stop e fechar posições sem veto de risco independente. Por isso **nada foi adaptado em código**.
* **Apenas inspiração conceitual:** a ideia de acompanhar o desempenho por agente (`agents/tracker.py`) e de vigiar a
  posição aberta (`agents/monitor.py`) confirmou desenhos que o robowin já tem (resultado por braço no OMS; `MANTER`
  e `ENCERRAR` passando pelo roteador e pelo risco).
* **Código derivado:** nenhum.

## 4. trader-skill ("The Trader skill")

* **Nome:** trader-skill
* **URL:** https://github.com/Jakeschincariol/trader-skill (conforme o README do projeto)
* **Licença:** MIT
* **Aviso de copyright (exato, lido de `LICENSE`):**

```
MIT License

Copyright (c) 2026 Jake Schincariol
```

* **Apenas inspiração conceitual** (nenhum código derivado):
  * o portão de risco que dá PASS/BLOCK com motivos explícitos e a **impressão (hash) da ordem exata que passou no
    risco**, exigida antes de a ordem ser mostrada como pronta (`trader-risk/check.py::fingerprint`,
    `trader-broker/ticket.py`) -> `src/robowin/execucao/aprovacao.py` e `exigir_aprovacao` em `src/robowin/execucao/roteador.py`;
  * o princípio "nenhuma ordem sem o seu sim; papel (simulação) por padrão" confirmou um desenho que o robowin já tinha
    (somente DEMO, `--confirmo-conta-demo`).
* **Rejeitado por ser específico de EUA/ações/longo prazo:** copycat de 13F, Congresso, SEC/8-K, lotes e *wash sale*,
  orçamento por colchão de caixa, rebalanceamento.
* **Código derivado:** nenhum.

---

## Resumo / Summary

| Projeto | Licença | Copyright no `LICENSE` | Inspiração conceitual | Código derivado |
|---|---|---|---|---|
| Aurum | MIT | `Copyright (c) 2025 zero-was-here` | sim | nenhum |
| TradingAgents | Apache-2.0 | modelo não preenchido (`Copyright [yyyy] [name of copyright owner]`); sem `NOTICE` | sim | nenhum |
| claude-trader | MIT | `Copyright (c) 2026 Claude Trader` | sim (só confirmação de desenho) | nenhum |
| trader-skill | MIT | `Copyright (c) 2026 Jake Schincariol` | sim | nenhum |

*Nenhum desses projetos endossa o robowin; nomes e marcas pertencem aos respectivos titulares. Nada aqui é conselho
financeiro: o robowin é um projeto de **simulação** e de pesquisa.*
