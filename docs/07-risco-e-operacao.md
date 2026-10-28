# 07 — Risco e operação (runbook)

Política: `config/risco.toml` travada por hash (`config/aprovacoes/`). Padrões: 1 contrato; R$ 60/operação;
R$ 200/dia; R$ 500/semana; drawdown R$ 1.000 alerta / R$ 1.500 máximo; entradas 09:15–17:30; zeragem 17:45.
**Responsável precisa aprovar** o registro antes de qualquer etapa real.

> **Aprovação pendente (ação do usuário):** `config/aprovacoes/risco-v1.toml` traz `responsavel = "pendente: ..."`.
> Enquanto estiver assim, a simulação roda mas é rotulada `risco_nao_aprovado` (incidente crítico), e **tudo que
> opera ao vivo recusa iniciar** (núcleo ao vivo, espelhamento e market making na demo: `ErroConfig`). Para liberar:
> 1. confira `config/risco.toml` e preencha `responsavel` (seu nome/identificação) e `motivo` no registro de aprovação;
> 2. regenere o hash do arquivo de risco: `sha256sum config/risco.toml` e grave em `sha256` do mesmo registro
>    (o hash cobre o `risco.toml`, não o registro; reaprove sempre que alterar `risco.toml`).

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
qualquer operação (`PerfilNaoConfirmado`). Trocar o ativo exige posição zerada e abre novo experimento; modelo,
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
