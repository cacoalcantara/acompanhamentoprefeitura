# 07 — Risco e operação (runbook)

> **Os limites de `config/risco.toml` (1 contrato; R$ 60/operação; R$ 200/dia; R$ 500/semana; drawdown R$ 1.000
> alerta / R$ 1.500 máximo) são a aprovação PROVISÓRIA do titular (`config/aprovacoes/risco-v2.toml`), só para testes
> em conta DEMO. Não são padrão de ninguém:** cada usuário define e aprova os próprios limites, por conta de
> negociação (seção "Limites por usuário e por conta", abaixo). Conta REAL exige uma autorização específica que não
> existe nesta versão.

## Limites por usuário e por conta de negociação

Cada usuário estabelece, altera e aprova os **próprios** limites pelo **painel** (página **Risco**) ou pela CLI, e
eles ficam isolados por usuário **e** por conta de negociação (plataforma + servidor + login). Código:
`src/robowin/risco/usuarios.py` (armazém), `src/robowin/painel/risco_usuarios.py` (página), `robowin usuario` e
`robowin risco` (CLI). Testes: `tests/unit/test_risco_usuarios.py` e `tests/unit/test_painel_risco.py`.

**Armazém** (arquivos simples e auditáveis; padrão `config/usuarios`, fora do git porque contém o login da conta e o
hash do token; mude com `[geral].usuarios`, `ROBOWIN_USUARIOS` ou `--usuarios`):

```
config/usuarios/<usuario>/usuario.toml                 nome + sal + sha256 do token (o token nunca é gravado)
config/usuarios/<usuario>/contas/<plat>-<servidor>-<login>/
    conta.toml        plataforma, servidor, login, tipo (demo|real), autorizacao_real = "bloqueada"
    risco.toml        limites DESTE usuário PARA ESTA conta (travados por hash)
    aprovacoes/risco-vN.toml                          aprovações versionadas (nunca sobrescritas)
    historico/risco-NNNN.toml + historico/indice.jsonl  cada versão salva dos limites (autor, origem, campos alterados)
```

**Regras:**

1. **Usuário novo começa SEM limites** (`SEM_LIMITES`): nada é imposto e operar fica **bloqueado** até definir e aprovar.
   O painel (e `robowin risco definir --modelo-inicial`) oferece um **modelo muito conservador** derivado do perfil do
   ativo (1 contrato; stop curto; perda por operação = pior caso desse stop + folga; dia = 2 operações; semana = 5;
   3 operações/dia; para após 2 perdas seguidas; pausa de 15 min), que só vale depois de você editar/salvar e aprovar.
   Todos os campos são obrigatórios no formulário (nenhum valor entra "por padrão"); a coerência é validada
   (`validar_coerencia`: operação ≤ dia ≤ semana, alerta ≤ drawdown máximo, janela de horário, teto de 1 contrato).
2. **Aprovação expressa** antes de qualquer operação automatizada: digitar `APROVO` (no painel, também o **token de
   ações** `ROBOWIN_TOKEN` digitado no formulário; o cookie não basta) + nome do responsável. A simulação pode rodar
   sem aprovação, **rotulada** `risco_nao_aprovado` (com os limites do próprio usuário ou o modelo conservador; nunca
   os de outro usuário nem os provisórios do titular). Núcleo ao vivo, espelho DEMO, MM na demo, gateway com terminal
   real e aceitação com `--confirmo-conta-demo` **recusam iniciar** sem aprovação válida do par usuário+conta.
3. **Alterar os limites muda o hash e invalida a aprovação** (`NAO_APROVADO`, "hash difere"): é preciso aprovar de novo
   (gera `risco-v<N+1>.toml`). Editar o `risco.toml` à mão tem o mesmo efeito.
4. **Bloqueio automático pelos limites do usuário+conta ativos:** o motor de risco (veto determinístico) e o
   **guardião do gateway** passam a usar os limites aprovados desse par (`ConfigGuardiao.de_risco`: contratos, janela,
   perda diária, stop). Antes o guardião usava valores embutidos; agora, mesmo no modo de arquivo único, ele lê o
   `config/risco.toml` aprovado.
5. **Conta conectada diferente da aprovada ⇒ bloqueio** com mensagem clara: o gateway e a aceitação recusam iniciar
   (`ContaDivergente`) se plataforma/servidor/login diferirem; o núcleo bloqueia novas entradas
   (`conta_divergente`, incidente crítico) até o estado do gateway confirmar a conta aprovada e `trade_mode = demo`.
6. **Auditoria:** `usuario_criado`, `conta_registrada`, `risco_limites_alterados` (sha anterior/novo, campos
   alterados, autor, origem painel/cli), `risco_aprovado` (com usuário, conta, escopo) e `risco_migrado_legado` vão
   para a cadeia de hash em `<[geral].dados>/auditoria` (a página Risco lista os eventos daquela conta).
7. **A IA não lê nem altera limites:** qualquer campo de limite (ou `risco`, `limites`, `usuario`,
   `conta_negociacao`) dentro de `[ia]`, `[[bracos]]`, `[reflexao]`, `[prazos]` ou `[decisao]` é recusado ao carregar
   (`validar_ia_sem_limites`); um teste estático garante que os módulos de IA/braços/notícias não importam o armazém,
   a aprovação nem a política. O veto final continua determinístico.
8. **Conta REAL:** pode ser registrada (`tipo = "real"`), mas `autorizacao_real` é **sempre "bloqueada"** nesta
   versão, qualquer que seja o texto gravado: aprovar e operar recusam (`AutorizacaoRealAusente`). Uma futura etapa
   real exigirá uma **nova autorização específica**, separada da aprovação de limites (e o checklist abaixo). **O
   envio real não existe.**
9. **Isolamento no painel:** login com usuário + token do usuário (`robowin usuario criar` mostra o token uma única
   vez); a sessão só vê e edita as contas daquele usuário (o usuário vem da sessão, nunca do formulário). Tentativas
   inválidas são limitadas como no restante do painel.

```bash
uv run robowin usuario criar --usuario ana --nome "Ana"            # guarda o token mostrado
uv run robowin usuario conta --usuario ana --conta mt5:CORRETORA-DEMO:123456
uv run robowin risco definir  --usuario ana --conta mt5:CORRETORA-DEMO:123456 --modelo-inicial
uv run robowin risco definir  --usuario ana --conta mt5:CORRETORA-DEMO:123456 --campo perda_max_dia_brl=80
uv run robowin risco aprovar  --usuario ana --conta mt5:CORRETORA-DEMO:123456 --responsavel "Ana" --motivo "demo"
uv run robowin risco status   --usuario ana --conta mt5:CORRETORA-DEMO:123456
uv run robowin risco historico --usuario ana --conta mt5:CORRETORA-DEMO:123456
uv run robowin nucleo  --usuario ana --conta mt5:CORRETORA-DEMO:123456 ...   # idem gateway, simular, mm ao-vivo, aceitacao
```

Ou fixe o par em `config/robowin.toml` (`[geral].usuario` e `[geral].conta_negociacao`). Sem nenhum dos dois vale o
**modo de arquivo único** abaixo (compatibilidade).

**Migrar a aprovação provisória do titular** para a conta DEMO dele (uma vez, exige `APROVO`):
`uv run robowin usuario migrar-legado --usuario titular --conta mt5:<SERVIDOR-DEMO>:<LOGIN>`. Copia o
`config/risco.toml` e grava `risco-v1.toml` na conta com `provisoria = true`, `escopo = "somente_demo"` e
`origem = "risco-v2.toml"`. Outros usuários não herdam nada.

## Modo de arquivo único (compatibilidade)

Política: `config/risco.toml` travada por hash (`config/aprovacoes/`); vale quando nenhum usuário/conta está
selecionado. Hoje está aprovada **provisoriamente pelo titular, somente DEMO** (`risco-v2.toml`).

> **Sem aprovação válida** (responsável `pendente` ou hash divergente), a simulação roda mas é rotulada
> `risco_nao_aprovado` (incidente crítico), e **tudo que opera ao vivo recusa iniciar** (núcleo ao vivo, espelhamento,
> market making na demo e gateway com terminal real: `ErroConfig`). Nenhum agente aprova por você.

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
  gap, autorização expressa **específica para conta real** (`autorizacao_real` da conta, hoje sempre "bloqueada"; a
  aprovação de limites DEMO não vale para real). **O envio real não existe nesta versão.**
- **Teste na demo:** saída com ticket velho deve ser rejeitada (não inverter a posição).

## Troca de ativo: risco
Limites em pontos são derivados do perfil (`stop_max_ticks` x tick). O `config/risco.toml` aprovado vale só para o
ativo com `risco_aprovado_padrao = true` (WIN). Outro ativo usa limites mínimos (limites em moeda do `risco.toml`,
menos operações/dia, stop/folga do perfil) e o rótulo `risco_nao_aprovado`, até existir `config/risco-<ativo>.toml`
com registro de aprovação por hash; ao vivo, sem aprovação, a sessão é recusada. Perfil `a_confirmar` bloqueia
qualquer operação (`PerfilNaoConfirmado`) até você confirmá-lo com `robowin perfil confirmar <ATIVO>` (ver
`docs/12-aceitacao-no-windows.md`, "Decisões que só você toma"). Trocar o ativo exige posição zerada e abre novo experimento; modelo,
orçamento e limites de um ativo nunca são reutilizados em outro.

## Qualquer mercado (forex, CFD, metal, cripto): risco
- **Moeda:** todo limite em dinheiro (`perda_max_*`, `dd_*`, `nocional_max_conta`) está na **moeda da conta**. O
  pior caso da operação usa o valor do ponto do símbolo × fator de conversão **do instante**; sem cotação do par de
  conversão o motor veta a entrada (`sem_cotacao_conversao:<lucro>-><conta>`). Saída e zeragem nunca dependem disso.
- **Lote fracionário:** a unidade do robô é o lote mínimo do perfil (0,01 no EURUSD). `contratos_max` conta essas
  unidades; `volume_max_lotes` (> 0) troca o limite por volume do terminal (ex.: 0,05 lote ⇒ 5 unidades), com teto da
  v1 de 1,0 lote; `nocional_max_conta` (> 0) limita preço × valor do ponto × quantidade na moeda da conta. No WIN nada
  muda: 1 contrato. Os dois campos entram no formulário do painel e em `robowin risco definir` (por usuário + conta).
- **Invariantes iguais em todo mercado:** stop obrigatório, sem martingale, sem piramidação, uma posição por símbolo
  (hedging do MT4 incluído); teste por propriedade com lote fracionário em `tests/unit/test_qualquer_mercado.py`.
- **Janela:** `entradas_de/entradas_ate/zeragem` são lidos **no fuso da sessão do instrumento** (forex:
  `America/New_York`), combinados com a grade da bolsa/corretora; entradas param `entradas_antes_fechamento_min`
  antes de qualquer fechamento, pausa ou rollover, e a zeragem é forçada `zeragem_antes_fechamento_min` antes (sem
  swap no day trade). O dia de negociação (perda diária, operações/dia) termina no rollover. Fim de semana do forex,
  pausas e `dias_fechados` ⇒ `fora_do_pregao`.
- **Swap:** se uma posição atravessar o rollover, o swap do perfil (triplo no dia configurado) entra no trade; perfil
  sem swap conhecido gera aviso e o swap fica de fora (a confirmar).

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
