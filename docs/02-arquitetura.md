# 02 — Arquitetura e fluxo de decisão

Processos: **gateway** (Windows; único `order_send`; guardião independente), **núcleo** (features, modelos,
StatArb, NLP, braços, risco, execução simulada, IA), **painel**. Barramento: ZeroMQ (PUB com número de
sequência + ROUTER/DEALER); em testes, barramento em processo.

Fluxo: gatilho → dossiê anonimizado (t0) → proponente (Claude) → validação determinística →
revisor (GPT, só entradas) → frescor → **motor de risco (veto final)** → OMS → política de execução
(padrão ou RL) → simulador/gateway. Saídas (stop, alvo, tempo, zeragem) são determinísticas, com **um dono
por posição**. Atraso, erro, recusa, JSON inválido, divergência, orçamento esgotado, lacuna de sequência ou
dado velho **bloqueiam entradas**; saídas e proteção continuam.

Braços: A-quant, B (Claude), C (Claude+GPT), MM, controles; modos imediato e condicional; braços extras e
outros provedores por configuração (`[[bracos]]`, `config/modelos.toml`). Ver `09-modelos-de-ia.md`.
Kill switch: painel, CLI `robowin emergencia` ou arquivo `EMERGENCIA`; rearme só manual.
