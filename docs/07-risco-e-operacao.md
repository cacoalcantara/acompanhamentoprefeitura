# 07 — Risco e operação (runbook)

Política: `config/risco.toml` travada por hash (`config/aprovacoes/`). Padrões: 1 contrato; R$ 60/operação;
R$ 200/dia; R$ 500/semana; drawdown R$ 1.000 alerta / R$ 1.500 máximo; entradas 09:15–17:30; zeragem 17:45.
**Responsável precisa aprovar** o registro antes de qualquer etapa real.

> **Aprovação pendente (ação do usuário):** `config/aprovacoes/risco-v1.toml` traz `responsavel = "pendente: ..."`.
> Enquanto estiver assim, a simulação roda mas é rotulada `risco_nao_aprovado` (incidente crítico), e **tudo que
> opera ao vivo recusa iniciar** (núcleo ao vivo, espelhamento e market making na demo: `ErroConfig`). Nenhum agente
> aprova por você: a decisão é sua, pelo comando guiado abaixo.

### Aprovar a política de risco (`robowin risco aprovar`)

```bash
uv run robowin risco status                      # estado da aprovação efetiva (APROVADO / PENDENTE / INVALIDO)
uv run robowin risco aprovar --responsavel "Seu Nome" --motivo "limites conferidos para a demo"
```

1. O comando mostra os limites de `config/risco.toml` em português (perda por operação, dia e semana, drawdown de
   alerta e máximo, contratos, operações/dia, perdas seguidas e pausa, stop/folga, janela de entradas, zeragem,
   bloqueio de evento, ordens/min) e o `sha256` do arquivo, calculado **exatamente** como a verificação calcula
   (bytes do arquivo).
2. Recusa política incoerente (ex.: `contratos_max` acima do teto da v1) e responsável vazio ou começando com
   `pendente`.
3. Exige que você **digite `APROVO`**. Qualquer outra resposta (ou stdin fechado) cancela: nada é gravado e o código de
   saída é `1`. Para uso não interativo (script seu, já revisado), `--sim-eu-aprovo` equivale a digitar `APROVO` (o
   resumo é impresso do mesmo jeito).
4. Grava um **novo** registro `config/aprovacoes/risco-vN.toml` (N = maior existente + 1; nunca sobrescreve: os
   anteriores ficam como histórico) com `sha256`, `data`, `responsavel`, `motivo`, `arquivo` e `aprovado_em_utc`, e
   registra o evento `risco_aprovado` na cadeia de auditoria (`<[geral].dados>/auditoria`, ou `--auditoria DIR`).

Quem carrega a política (núcleo, backtest, espelho, MM, `robowin verificar`) continua apontando para
`config/aprovacoes/risco-v1.toml`, mas usa a **versão mais recente válida** (`core.config.resolver_aprovacao`): o
registro `risco-vN.toml` de maior N que seja legível e cujo `sha256` confira com o `risco.toml` atual. Se nenhum
conferir (você editou o `risco.toml` depois de aprovar), o carregamento recusa por hash divergente e `robowin
verificar` mostra REPROVADO com o comando para reaprovar. Com responsável preenchido, `robowin verificar` mostra
**APROVADO** na linha da política de risco. O que ainda falta (risco, perfis, chaves) aparece em
`uv run robowin pendencias`.

- **Emergência:** botão do painel (token), `robowin emergencia` ou arquivo `EMERGENCIA`; cancela, zera,
  bloqueia. No modo dividido, use também o "fechar tudo" manual do MT5.
- **Desconexão:** feed parado bloqueia entradas; sem heartbeat 15 s bloqueia, 60 s zera a posição demo.
- **Reconciliação:** a cada 10 s; deals manuais (CLIENT/MOBILE/WEB) bloqueiam.
- **Fim do dia:** zeragem pelo robô antes da zeragem compulsória da corretora.
- **Gap:** ver `robowin.risco.estresse`. Um movimento até o limite pode custar ≈ R$ 3,5 mil.
- **Checklist antes da etapa real:** confirmação escrita da corretora (robôs/API, zeragem, margem,
  mensagens), políticas da Anthropic e da OpenAI revisadas, capital e limites definidos, ciência do risco de
  gap, autorização expressa. **O envio real não existe nesta versão.**
- **Teste na demo:** saída com ticket velho deve ser rejeitada (não inverter a posição).

## Troca de ativo: risco
Limites em pontos são derivados do perfil (`stop_max_ticks` x tick). O `config/risco.toml` aprovado vale só para o
ativo com `risco_aprovado_padrao = true` (WIN). Outro ativo usa limites mínimos (limites em moeda do `risco.toml`,
menos operações/dia, stop/folga do perfil) e o rótulo `risco_nao_aprovado`, até existir `config/risco-<ativo>.toml`
com registro de aprovação por hash; ao vivo, sem aprovação, a sessão é recusada. Perfil `a_confirmar` bloqueia
qualquer operação (`PerfilNaoConfirmado`) até você confirmá-lo com `robowin perfil confirmar <ATIVO>` (ver
`docs/12-aceitacao-no-windows.md`, "Decisões que só você toma"). Trocar o ativo exige posição zerada e abre novo experimento; modelo,
orçamento e limites de um ativo nunca são reutilizados em outro.

## MT4 (hedging): risco e operação
- **Hedging:** em conta MT4 uma ordem oposta **abre outra posição** em vez de reduzir. Por isso, no modo `hedging`
  (`ConfigDemo.conta`, definido pela plataforma): a saída é **sempre por ticket** (`OrderClose`; nunca ordem
  oposta); a entrada é **recusada se já houver posição** (qualquer origem, inclusive manual); vale
  `|posições abertas por ticket + pendentes + nova| ≤ contratos_max` no guardião **e** no executor; após cada deal a
  conferência por ticket detecta posição a mais ou saída que abriu posição e fecha o excesso por ticket, travando o
  executor. Testes por propriedade (hypothesis) cobrem hedging e netting.
- **Sem backfill:** lacuna do feed (EA reiniciado, buffer de ticks estourado, falha do transporte) **não** se
  recupera; o gateway bloqueia entradas (`feed_lacuna`, até a virada do dia ou rearme manual) e sinaliza
  `lacuna_pendente` ao núcleo. Saídas e zeragem continuam funcionando.
- **Capacidades:** sem livro, o market making e as features de livro ficam desligados (`CapacidadeAusente` /
  alerta `capacidade_ausente`); tempo em segundos.
- **Conta real:** o EA e o cliente recusam ordens fora de conta DEMO (erro 1006); o MT4 só distingue demo/real, e
  qualquer conta que não seja demo é tratada como real.
- **Emergência:** `robowin emergencia --plataforma mt4` (ou o arquivo `EMERGENCIA`) cancela as pendentes e fecha
  **cada** posição pelo ticket; confirme no terminal (e use "Fechar tudo" manual se o EA não responder).
- **Deals do histórico:** o MT4 só tem ordens fechadas; o cliente cria deals IN/OUT. Fechamento manual de posição do
  robô (magic ≠ 0) só é detectado enquanto o EA estiver vivo (`fechado_ea`); após reinício do EA, vale o magic.
- **Limite:** o `.mq4` não foi compilado nem testado; antes de qualquer demo, leia o cabeçalho do arquivo.
