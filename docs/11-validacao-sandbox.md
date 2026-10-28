# 11 - Validacao em sandbox (o que foi e o que NAO foi verificado)

Este documento registra o que so pode ser validado fora do ambiente de desenvolvimento do robowin (sem Windows,
sem MetaEditor/MT4/MT5 e com os hosts da MetaQuotes bloqueados). Tudo aqui e SOMENTE SIMULACAO/DEMO.

## MQL4 (mql4/RobowinBridge.mq4)

**O EA NAO foi compilado nem testado.** Nao existe compilador MQL4 neste ambiente. O que existe e um linter
estatico, `scripts/lint_mql4.py` (somente stdlib), que pega erros evidentes e confere o protocolo. Ele NAO e um
compilador: passar no linter nao significa que o MetaEditor aceita o arquivo.

Uso (tambem roda na CI, job `linux`):

```
uv run python scripts/lint_mql4.py mql4/RobowinBridge.mq4        # sai com 1 se houver ERRO
uv run python scripts/lint_mql4.py mql4/RobowinBridge.mq4 --estrito --sem-revisar
```

Severidades: **ERRO** (quase certamente quebra compilacao ou contrato), **AVISO** (suspeito),
**REVISAR** (o linter nao consegue decidir; conferir a mao / no MetaEditor).

O que o linter verifica: tokenizacao (comentarios, strings com escape, hex, `C'r,g,b'`, `D'...'`); balanceamento de
`() {} []`, `#import`/`#endif`, `#include` malformado; `;` faltando ou sobrando; funcao nao-void sem `return`
(heuristica de caminhos); identificadores/funcoes/constantes MQL4 fora da tabela; aridade das funcoes MQL4, das
funcoes do arquivo e das importadas por `#import` (aridade e tipos string x numero, `&`); recursos do MQL5 usados por
engano (`CTrade`, `PositionSelect`, `MqlTradeRequest`, `OrderSend(req,res)`, `TRADE_ACTION_*`...); retorno de
`OrderSend` sem checagem de -1 e retorno de `OrderClose/Modify/Delete/Select` descartado; getters `Order*()` sem
`OrderSelect` na mesma funcao; divisao/modulo por zero literal; consistencia de protocolo (comandos, chaves lidas e
escritas, codigos de erro e versao) entre o EA, `mql4/PROTOCOLO.md`, `protocolo_mt4.py` e `conector_mt4.py`.

### Resultado contra o EA real

- **ERRO: 0.** Nenhum erro evidente de sintaxe/estrutura/protocolo foi encontrado (e isso NAO prova que compila).
- Correcao feita no EA (robustez, nao era erro de compilacao): em `CmdOrdemAbrir`, o `OrderModify` do caminho
  `SltpEmSeguida` usava `OrderOpenPrice()`, que depende de um `OrderSelect` anterior que pode ter falhado; passou a
  usar `p_exec` (preco de execucao ja calculado). O aviso "NAO compilado/testado" do cabecalho foi mantido.
- **AVISO: 1.** `PROTOCOLO.md` lista o arg `versao_cliente` do comando `ola`, que o EA nunca le (inofensivo: o
  cliente envia, o EA ignora).

### REVISAR (conferir no MetaEditor / com a biblioteca instalada)

1. **Biblioteca mql-zmq (`#include <Zmq/Zmq.mqh>`)**: tipos `Context`, `Socket`, `ZmqMsg` e metodos `.bind()`,
   `.recv(msg, nowait)`, `.send(msg)`, `.setLinger()`, `.getData()` foram escritos de memoria; o linter nao enxerga
   o `.mqh`. Conferir assinaturas, o construtor `Context("nome")` e `ZmqMsg(string)`, e que as DLLs
   `libzmq.dll`/`libsodium.dll` (x86) estao em `MQL4/Libraries`. O EA nao tem `#import` proprio: a DLL entra via a
   biblioteca, portanto a checagem de assinatura de DLL do linter nao tem o que comparar neste EA.
2. **Constantes `SYMBOL_*`** em `CmdSimbolo`: `SYMBOL_TRADE_EXEMODE`, `SYMBOL_PATH`, `SYMBOL_DESCRIPTION`,
   `SYMBOL_CURRENCY_PROFIT`, `SYMBOL_CURRENCY_BASE` (e `SymbolInfoInteger/String` no MQL4 build >= 600). Se alguma nao
   existir no seu build, remover o campo no EA e em `PROTOCOLO.md`/`conector_mt4.py`.
3. **`long`/`ulong`** (24 linhas): exigem MT4 build >= 600; no MQL4 antigo nao existem.
4. **`ItemOrdemSelecionada()` e `ChecarMagicDoTicket()`** chamam `Order*()` sem `OrderSelect` proprio: dependem de o
   chamador ter selecionado o ticket (todos os chamadores atuais selecionam). Nao reutilizar fora desse contrato.
5. **Includes externos** (`Zmq/Zmq.mqh`) e `stdlib.mqh` (`ErrorDescription`) precisam existir em `MQL4/Include`.
6. **Chaves que o EA escreve e o cliente Python nunca le** (`conta_demo`, `ea`, `ea_versao`, `hedging`, `mais`,
   `somente_leitura`, `t_corrente_s`, `t_servidor_s`, `ticket_restante`): documentadas no protocolo, ignoradas pelo
   `conector_mt4.py` hoje (ex.: `ticket_restante`, `mais`). Decidir se o cliente deve usa-las.
7. `StringSplit(Magics, ',', partes)` e `StringToInteger`: semantica de separador/argumentos conferir no build.

### Limites do linter (o que ele NAO faz)

- Nao resolve tipos nem sobrecargas: conversoes implicitas, `const`/referencias, retorno de expressoes e membros de
  classes/structs nao sao checados; so ha inferencia de tipo para literais, casts e variaveis simples.
- Nao expande `#define` nem avalia `#ifdef`; macros sao tratadas como identificadores conhecidos.
- A analise de "todos os caminhos retornam" e heuristica (nao entende `goto`, nem condicoes sempre verdadeiras
  alem de `while(true)`/`for(;;)`).
- Escopo de variaveis e plano por funcao (nao detecta uso fora do bloco nem sombreamento).
- As tabelas de funcoes/constantes MQL4 sao uma lista curada e escrita de memoria: o que nao esta nela vira REVISAR,
  nao ERRO; o que esta nela pode estar com aridade errada na tabela. Sao ~150 funcoes, nao a API inteira.
- O que so o MetaEditor/MT4 revelam (limites de memoria dos arrays globais, permissoes, build, comportamento de
  `recv/send`, timers) continua sem verificacao ate o teste em conta DEMO.
