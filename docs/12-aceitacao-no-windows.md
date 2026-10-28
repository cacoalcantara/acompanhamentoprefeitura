# 12 — Aceitação no seu Windows (conta DEMO)

> **Somente conta DEMO.** O comando `robowin aceitacao` recusa conta real, conta de torneio, servidor sem `DEMO` no
> nome e login diferente do esperado, e **aborta** nesses casos. Sem a flag `--confirmo-conta-demo` ele **não envia
> nenhuma ordem** (só lê). Nada aqui prova lucratividade.

## Por que existe

Este repositório é desenvolvido em Linux, sem terminal MetaTrader, sem corretora e sem acesso às APIs de IA. Tudo que
só existe na **sua** máquina (terminal MT5/MT4 real, conta demo, compilação do `.mq4`, latência real de ordens e das
APIs de IA) fica **fora do que consigo provar**. A forma honesta de chegar a "100%" é você rodar um roteiro
automático e me devolver o relatório, que eu audito. Por isso:

> **"100%" significa: aceitação rodada no Windows do usuário, com relatório sem nenhuma etapa REPROVADA e com as
> etapas aplicáveis APROVADAS.** Etapa sem pré-requisito aparece como NÃO VERIFICÁVEL (com o motivo) e **nunca**
> conta como aprovada.

## Pré-requisitos

1. Windows 10/11 x64, [uv](https://astral.sh/uv) (o script instala se faltar) e Python 3.13 (o `uv` baixa).
2. Terminal **MetaTrader 5** (ou MT4) da corretora, **aberto e logado em conta DEMO**, com **Algo Trading ligado**
   (botão verde) e negociação automatizada permitida nas opções do terminal.
3. O ativo precisa estar no *Market Watch* e com dados de mercado (e livro de ofertas, no MT5) habilitados. A B3
   exige a assinatura de dados da corretora.
4. **Pregão aberto**: dia útil, entre 09:15 e 17:30 (janela de entradas de `config/risco.toml`); rode entre ~10:00 e
   ~16:30. Fora do pregão, A4–A9 ficam NÃO VERIFICÁVEIS (e sem ordens).
5. Em `config/robowin.toml`, seção `[mt5]`, preencha `login` e `servidor` da conta DEMO (ou passe `-Login` e
   `-Servidor`). A **senha** vai só pela variável de ambiente `ROBOWIN_MT5_SENHA` (ou deixe o terminal já logado).
6. Perfil do ativo **confirmado** (`a_confirmar = false` em `config/instrumentos.toml`). Hoje só o **WIN** está
   confirmado; com perfil `a_confirmar` as etapas de ordem **não rodam** (A3 lista as especificações observadas para
   você conferir).
7. Opcional, para A10: `ANTHROPIC_API_KEY` e/ou `OPENAI_API_KEY` no ambiente (gastam alguns centavos de dólar).
8. **MT4:** biblioteca `mql-zmq` instalada na pasta de dados do terminal (`MQL4\Include\Zmq`, `MQL4\Libraries\libzmq.dll`),
   EA compilado (passo 6 do script) e anexado a **um** gráfico da conta demo.
9. Conta demo **zerada**: sem posições nem ordens abertas. A8/A9 acionam o kill switch, que zera **toda** a conta.

## Passo a passo

```powershell
# 1) só leitura + testes (nenhuma ordem): confere ambiente, instala, roda pytest, constantes do MT5, lint do EA, verificar
powershell -ExecutionPolicy Bypass -File scripts\windows\validar_tudo.ps1

# 2) aceitação completa na DEMO (ordens de teste de 1 contrato); a senha NÃO vai por parâmetro
$env:ROBOWIN_MT5_SENHA = "<senha da conta DEMO>"        # opcional se o terminal já estiver logado
powershell -ExecutionPolicy Bypass -File scripts\windows\validar_tudo.ps1 `
    -ConfirmoContaDemo -Login 12345678 -Servidor "XPMT5-DEMO" -Repeticoes 20 -MinutosColeta 5

# MT4: acrescente -Plataforma mt4 (e, se preciso, -MetaEditor "C:\...\metaeditor.exe" -PastaMql4 "C:\...\MQL4")
```

O script termina gerando `robowin-aceitacao-<data>.zip` na raiz do repositório: **esse zip é o que você me envia**.
Para rodar só a aceitação (sem os outros passos): `uv run robowin aceitacao --confirmo-conta-demo --login 12345678
--servidor XPMT5-DEMO` (opções: `--plataforma mt5|mt4`, `--ativo`, `--minutos-coleta N`, `--repeticoes N`, `--saida DIR`,
`--json`, `--pular-ia`). Para ensaiar sem terminal: acrescente `--duble` (dados sintéticos, relógio simulado; **não vale
como aceitação**).

> **Aviso honesto:** `compilar_ea.ps1` e `validar_tudo.ps1` foram escritos **sem poder ser executados** no ambiente de
> desenvolvimento (Linux). Se um deles falhar por motivo do próprio script (caminho do MetaEditor, codificação do log),
> compile pelo MetaEditor (F7) e me mande a mensagem.

## O que cada etapa faz e mede

| Etapa | O que faz | Aprovada quando | Vira NÃO VERIFICÁVEL quando |
|---|---|---|---|
| **A1** Ambiente | Sistema, versão do Python, do pacote `MetaTrader5` (ou handshake do EA no MT4), `initialize()` e terminal conectado | Python ≥ 3.12; MT5 real em Windows; terminal conectado | — (falha = REPROVADO e aborta) |
| **A2** Conta | Lê a conta: **identidade** (demo, servidor com `DEMO`, login/servidor esperados), `trade_allowed` e `trade_expert` (Algo Trading), netting (MT5) ou hedging (MT4), moeda == moeda do perfil | Tudo confere | — (identidade errada = REPROVADO e **aborta**; nada mais roda) |
| **A3** Contrato e especificações | Contrato vigente do calendário B3 existe na corretora; tick, valor do ponto, lote e moeda do símbolo == perfil; calibra o offset do relógio do servidor | Todos iguais ao perfil confirmado | Perfil `a_confirmar` (mostra o que foi **observado**) |
| **A4** Coleta | Coleta ticks e livro por N minutos: flags BUY/SELL, offset do servidor calibrado e estável, profundidade do livro (declarada × observada), lacunas e `dados.qualidade` | Há ticks de negócio com BUY/SELL, offset estável, livro com ≥ 1 nível, nenhum achado crítico | Pregão fechado; A3 não aprovada; MT4 (sem livro: A4.3 é "não se aplica") |
| **A5** Ordens | (1) compra **limitada de 1 contrato LONGE do mercado**, conferida em `orders_get` e **cancelada**; (2) **mercado de 1 contrato com SL**, posição conferida (volume, SL, magic) e **fechada por ticket**; (3) **saída com ticket velho deve ser REJEITADA** sem abrir nem inverter posição; (4) estado final: posição zero e nenhuma ordem pendente | Os 4 itens | Sem `--confirmo-conta-demo`; perfil `a_confirmar`; A2/A3 não aprovadas; pregão fechado; conta com posição/ordem prévia no símbolo |
| **A6** Latência | N repetições de `order_send` → confirmação (limitada longe + cancelamento): **p50/p95/p99**, respeitando o teto de 20 mensagens/min e a pausa entre repetições | Todas confirmadas, p95 ≤ `--limite-latencia-ms` (padrão 1500), orçamento nunca excedido | Mesmos pré-requisitos de A5 |
| **A7** Reconciliação | Deals do teste classificados por `reason` (EXPERT/SL/TP/SO esperados; manual/desconhecido reprova); volume líquido dos deals == posição do terminal (zero) | Todos os deals do teste presentes e coerentes | A5 não gerou deals |
| **A8** Kill switch e queda do núcleo | Usa o **gateway real em processo** com heartbeat curto de teste (2 s/5 s; produção 15/60 s): (1) comando de emergência confirma "zerado e travado" e recusa nova entrada; (2) arquivo `EMERGENCIA` zera e trava; (3) sem heartbeat do núcleo as entradas são vetadas e a posição é zerada | Os 3 cenários | Mesmos de A5; conta com posição/ordem de outro uso |
| **A9** Zeragem por horário | Configura a zeragem para o próximo minuto; a posição deve continuar aberta até o horário e ser zerada em até 8 s depois | Zerou após (não antes) do horário | Mesmos de A8 |
| **A10** IA ao vivo | TTFT e tempo total **p50/p95/p99** dos modelos dos papéis de `config/robowin.toml` que tenham chave; nunca imprime chaves | ≤ 20% de falhas e p95 dentro do prazo de `[prazos]` | Sem chave (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`); `--pular-ia` marca "não aplicável" |
| **A11** MT4 | Handshake do EA (protocolo, nome/versão, não ser o dublê) e hedging por ticket (saída fecha **aquele** ticket; ticket velho não abre posição oposta) | Handshake ok e A5.2/A5.3 aprovadas em conta hedging | Plataforma MT5 ("não se aplica") |

Se um SL/TP fechar a posição de teste antes do evento que A8/A9 queriam medir (mercado volátil), o cenário é repetido até
3 vezes; se persistir, fica NÃO VERIFICÁVEL ("inconclusivo"), nunca aprovado.

### Quanto de ordem a aceitação envia

No máximo **1 contrato** por vez, sempre pelo `ExecucaoDemo` (único ponto de envio do projeto, com a guarda de conta
demo conferida **antes de cada envio**), com SL em toda ordem a mercado e magic próprio (`990777`). Uma ordem a
mercado custa o spread e, na demo, não custa dinheiro. A limpeza (cancelar e zerar o que tiver o magic da aceitação)
roda mesmo se algo falhar no meio, e o estado final é conferido.

## Como interpretar o relatório

Arquivos `aceitacao-<AAAA-MM-DD-HHMMSS>.md` (leitura) e `.json` (máquina) em `relatorios\aceitacao\`:

- **Estados:** `APROVADO` (critério verificado) · `REPROVADO` (critério falhou; o detalhe diz por quê) ·
  `NAO VERIFICAVEL AQUI` (faltou pré-requisito; o detalhe diz qual; **não** é aprovação).
- **Veredito:** `REPROVADO` (qualquer etapa reprovada) · `INCOMPLETO (nao verificadas: ...)` (nenhuma reprovada, mas há
  etapas aplicáveis sem verificar: rode de novo no pregão, com `-ConfirmoContaDemo`, chaves etc.) · `ACEITO` (nenhuma
  reprovada e todas as aplicáveis aprovadas). Contra o `--duble` o veredito diz que **não vale como aceitação**.
- **Código de saída:** `0` sem REPROVADO · `1` com REPROVADO (inclui conta recusada) · `2` erro de uso/configuração ·
  `3` terminal indisponível.
- **Latências:** `colocar_limitada`, `cancelar` e `envio_ate_confirmacao` (p50/p95/p99 em ms, medidas no cliente em
  volta de cada envio) e, na A10, TTFT/total por modelo. Com `--repeticoes` < 20 o p99 é só indicativo.
- **Versões e hashes:** commit do git (e se a árvore está modificada), Python, pacote `MetaTrader5`, build do terminal,
  e `sha256` de `config/risco.toml`, da aprovação de risco, de `config/instrumentos.toml`, do perfil e do `.mq4`.
- **`ordens_enviadas`:** número de `order_send` realmente chamados; é **0** sem a flag.

## O que NUNCA é enviado (nem entra no relatório)

- Senha da conta, chaves de API e tokens (variáveis `ROBOWIN_*`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`...). Tudo passa
  por `ia.segredos.redigir`; o relatório mostra o login **mascarado** (`****45`) e o nome do usuário do Windows nos
  caminhos vira `***`. O servidor (ex.: `XPMT5-DEMO`) aparece.
- Arquivos `.env`, `*.key`, `*.pem`, `*senha*`, `*token*`. O `validar_tudo.ps1` copia só `.json/.md/.txt/.log`,
  **exclui** qualquer arquivo que contenha o valor de uma variável secreta ou um padrão de chave/senha, e grava o
  `MANIFESTO.txt` (sha256 de cada arquivo e lista dos excluídos). **Abra o manifesto antes de enviar.**
- Dados pessoais da conta (nome, empresa): o roteiro nem os lê.

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| A1 REPROVADO "initialize falhou" | Terminal fechado, caminho errado (`--terminal`), ou Python/terminal com bitness diferente |
| A2.2 REPROVADO | Botão Algo Trading desligado ou negociação automatizada bloqueada nas opções |
| A2.1 REPROVADO e tudo "abortado" | Conta real/torneio, servidor sem `DEMO`, ou `--login`/`--servidor` diferentes da conta logada |
| A4/A5/A6/A8/A9 "pregão não está aberto" | Fora de 09:15–17:30, feriado, ou sem ticks novos (assinatura de dados) |
| A4.3 REPROVADO | Livro de ofertas (DOM) sem permissão para o ativo |
| A5/A8 "conta ja tem posicao/ordem" | Zere a conta demo manualmente antes |
| A3.2 NÃO VERIFICÁVEL | Perfil `a_confirmar`: confira as especificações observadas e edite `config/instrumentos.toml` |
