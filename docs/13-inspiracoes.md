# 13 — Inspirações externas: o que foi adaptado, o que não foi, e por quê

> **SOMENTE SIMULAÇÃO / conta DEMO.** Nada aqui envia ordem real nem prova lucratividade.

Fonte analisada (somente leitura): **day-trader-skill**, de Jake Schincariol (licença MIT), clonado em
`/home/user/jakeschincariol/day-trader-skill`. Aviso de copyright e texto da licença: `THIRD_PARTY_NOTICES.md`.

## O que o repositório é de fato

Um pacote de **skills do Claude Code** (instruções em `SKILL.md` + scripts Python auxiliares) para **ações dos EUA**,
com dados do Yahoo, Nasdaq, SEC e do Congresso americano. São 11 skills (`trader-alerts`, `-analyst`, `-broker`,
`-budget`, `-entry`, `-exit`, `-news`, `-pelosi`, `-risk`, `-scanner`, `-timemachine`) e **12 arquivos de teste**
(`tests/test_*.py`). São instruções para um chat do Claude conduzir uma pessoa, não um motor de execução.

## O que foi adaptado

### 1. Time Machine → `src/robowin/avaliacao/maquina_do_tempo.py`

Ideia e método inspirados no `trader-timemachine`; **reimplementação independente** (nenhum código copiado), sobre
os tipos do robowin:

| Método (ideia) | Como ficou no robowin |
|---|---|
| Reconstruir o resto do pregão 10.000 vezes sorteando barras de 5 min do próprio ativo, na mesma hora do dia (± 2 barras), de sessões anteriores, reescaladas ao preço atual | `simular`: numpy vetorizado (10 mil replays em bem menos de 1 s), seed padrão 19, barras sorteadas da **mesma hora do dia** (± 2), reescaladas ao preço corrente e **arredondadas ao tick do perfil** |
| Só sessões completas (descartar meio-pregão, gaps e o dia do plano) | `barras_de_ticks`: usa a **grade de sessão do perfil/bolsa** (`config/instrumentos.toml`, `config/bolsas.toml`); sessão completa = todas as barras da grade têm tick; as descartadas ficam listadas com o motivo |
| Checar o stop antes do alvo; barra que toca os dois conta como stop | idem, e o stop ainda pode pagar **slippage em ticks** (opcional, conservador) e executa no pior entre o nível e a abertura da barra (gap) |
| Reaplicar a mesma regra nas sessões passadas reais | `historico(..., regra=callable)`: a regra só enxerga as barras que fecharam **antes** da barra de entrada |
| Veredito comparado ao equilíbrio 1/(1+R) | Equilíbrio **líquido de custos** `(L + c)/(R + L)` com os custos do perfil (`execucao/custos.py`, inclusive a taxa de zeragem quando a posição fica aberta) na **moeda do instrumento** |
| Recusar com menos de 20 sessões completas | `ErroSessoesInsuficientes`; recusa também entrada/zeragem fora da grade, plano incoerente (stop do lado errado) e dados sem rótulo de origem (sintético ou não) |

Diferenças deliberadas: **causal** (só sessões anteriores à data do plano; há teste de perturbação do futuro),
**rotulado** (`SIMULACAO`, `SINTETICO`, `ativo_a_confirmar`) e sempre com o aviso *"simulação de bootstrap das
últimas sessões, NÃO é previsão"*. Nenhum indicador técnico tradicional é usado.

Uso: `uv run robowin maquina-do-tempo --ativo WIN --sintetico --lado compra --stop-pontos 100 --alvo-pontos 200`
(ou `--dados DIR` para um armazém de ticks; `--entrada HH:MM --zeragem HH:MM --seed N --replays N --json`).

### 2. Probabilidades do plano como evidência para a IA → `src/robowin/decisao/odds.py`

Depois que o proponente propõe stop/alvo, o orquestrador calcula as probabilidades do plano proposto e as entrega
ao **revisor** (GPT) no dossiê da revisão como o campo anonimizado `probabilidades_do_plano` (probabilidades, R médio,
veredito e número de sessões; **sem datas e sem níveis absolutos**, validado por `ia.dossie.verificar_anonimizacao`).
O prompt `config/prompts/revisor_v1.md` foi atualizado (o hash do prompt muda e fica registrado no manifesto): é
simulação, não previsão, e concordância não prova lucro. Falha ou dados insuficientes => o campo vem como
`indisponivel` e **nunca** bloqueia nem aprova por omissão.

Opcional e **desligado por padrão**: `[decisao].veto_por_odds = false` (margem em `margem_veto_odds`). Ligado,
bloqueia a entrada se a fração de alvos entre os desfechos resolvidos, descontados os custos, ficar abaixo do
equilíbrio líquido + margem; o bloqueio aparece rotulado `veto_por_odds:...` nos motivos da decisão e na contagem
`bloq:veto_por_odds` do resumo.

### 3. Princípios já cobertos (nada novo a implementar)

- **"Nenhuma ordem sem sim"** (`trader-broker`: dois "sim" do usuário + um PASS das regras). No robowin já existem:
  a guarda de conta **demo** e a **aprovação de risco por hash** (`config/aprovacoes/`, `risco/politica.py`), o único
  `order_send` (teste estático de AST) e `ExecucaoRealNaoAutorizada` para qualquer envio real.
- **Checagem PASS/BLOCK com motivo** (`trader-risk`): já coberta pelo **motor de risco** independente
  (`risco/motor.py`), que aprova ou veta cada intenção com a lista de motivos gravada na decisão; saídas e proteção
  nunca esperam por IA nem por rede.

## O que NÃO foi adotado, e por quê

| Item do day-trader-skill | Motivo |
|---|---|
| Scanner de setups (ORB, flags, sobrevendido) e *entry timer* por padrões gráficos | Conflitam com a regra do projeto de **não usar indicadores nem estratégias técnicas tradicionais** (testes estáticos falham se aparecerem). |
| Dados do Yahoo, Nasdaq, SEC e Congresso (`trader-news`, `trader-pelosi`, `trader-analyst`...) | Não se aplicam a B3/MT5: outro mercado, outra moeda, outro calendário e outras fontes; o robowin usa ticks do MT5 e fontes configuráveis em `config/fontes.toml`. |
| O formato de skills em si | São instruções para um chat do Claude conduzir uma pessoa, não um motor de execução com risco, auditoria e simulação determinística. |

## Divergência com a descrição recebida

A descrição que veio na imagem do usuário fala em **MT5, XAUUSD, "Aurum TradingBot" e ~1.450 testes**. Verificado
no repositório clonado: **não há MT5, nem XAUUSD, nem "Aurum TradingBot"**, e há **12 arquivos de teste** (não ~1.450
testes). O repositório analisado é um pacote de skills do Claude para ações dos EUA. Logo, a descrição da imagem
parece se referir a **outro projeto**. **Se quiser adaptar aquele, envie o link correto** do repositório; nada foi
assumido ou inventado sobre ele.
