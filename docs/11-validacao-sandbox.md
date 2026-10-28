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

## Windows de verdade sob Wine (executado em 28/10/2026)

Procedimento: Wine 9.0 (apt, Ubuntu 24.04) + Python 3.12.10 para Windows x64 obtido do pacote NuGet `python`
(o Python 3.13 nao serve: usa `CopyFile2`, nao implementada no Wine 9) + wheels `win_amd64` do PyPI.
Limitacao do Wine 9: `ucrtbase.dll.crealf` nao existe, entao o numpy recente (>= 2.2) aborta ao importar;
para este teste usou-se numpy 1.26.4 + pandas 2.2.3 numa pasta separada (`PYTHONPATH`), o que **quebra o scipy 1.18**
(`np.long`). Isso e artefato do ambiente de teste, nao do projeto.

Resultados:

| Verificacao | Resultado |
|---|---|
| Python Windows (AMD64) roda sob Wine | OK (3.12.10) |
| `import MetaTrader5` (wheel OFICIAL 5.0.6231, `win_amd64`) | OK |
| 20 funcoes que o projeto usa existem no pacote real (`initialize`, `order_send`, `copy_ticks_*`, `market_book_*`, `positions_get`, `history_deals_get`...) | todas presentes |
| `MetaTrader5.initialize()` sem terminal | falha de forma tratada: `(-10003, 'IPC initialize failed, MetaTrader 5 x64 not found')` |
| `ConectorMT5` do projeto sem terminal | `initialize() -> False`, `last_error()` correto, sem excecao |
| Constantes numericas (`scripts/conferir_constantes_mt5.py`) | 72 conferem, 0 divergem, 2 sem equivalente no pacote Python (mascaras `SYMBOL_FILLING_FOK/IOC`, valores 1/2 da documentacao MQL5) |
| Testes unitarios sob Windows (core, guardiao, contrato, AST de `order_send`, gateway, MT4 ponte/hedging/capacidades, OMS, risco) | 205 de 208 passam; as 3 falhas sao `numpy.long` do scipy no ambiente hibrido (CLI importa `avaliacao`) |

NAO coberto: terminal MT5/MT4 instalado, conexao a servidor de corretora, `order_send` real, `.mq4` compilado.
O job `windows` da CI roda a suite completa e a conferencia de constantes em Windows nativo com numpy 2.

## CI Windows nativo (GitHub Actions) — 28/10/2026

- Execuções 1 a 26 do job `windows` falharam. Causas encontradas e corrigidas (bugs reais, não do ambiente):
  1. `execucao/ponte_gateway.py::_pid_vivo` usava `os.kill(pid, 0)`; no Windows o sinal 0 é `CTRL_C_EVENT` e
     interrompia o processo (KeyboardInterrupt). Agora usa `OpenProcess`/`GetExitCodeProcess` no Windows; teste
     estático proíbe `os.kill` fora do helper.
  2. O git no Windows converte LF→CRLF; o sha256 de `config/risco.toml` deixava de conferir com a aprovação.
     `core.config.sha256_arquivo` normaliza quebras de linha e `.gitattributes` fixa LF.
  3. A checagem do gateway em `robowin verificar` não fechava o log antes de apagar o diretório temporário.
- Execução 27: **os 3 jobs verdes** (linux 3.12, linux 3.13, windows com o wheel oficial `MetaTrader5`).

## CI: o que passa a ser validado no Windows sem terminal (jobs `windows-scripts` e `mql`)

O ambiente de desenvolvimento não tem PowerShell nem MetaEditor, e os hosts da MetaQuotes são bloqueados. A CI do
GitHub (runner `windows-latest`, com `pwsh`, Windows PowerShell 5.1 e internet) passa a executar o que aqui só podia
ser lido:

| Job | O que faz | Falha a CI quando |
|---|---|---|
| `windows-scripts` | `scripts/windows/analisar_scripts.ps1` em PowerShell 7 **e** em Windows PowerShell 5.1: `Parser::ParseFile` em todo `scripts/windows/*.ps1`, bytes não ASCII e PSScriptAnalyzer (instalado se faltar) | erro de sintaxe, byte não ASCII ou achado de severidade `Error`/`ParseError` (avisos só são listados no log) |
| `windows-scripts` | `instalar.ps1 -SemTerminal` (execução seca: `uv sync`, `robowin --help`, `import MetaTrader5`) | qualquer passo com código ≠ 0 |
| `windows-scripts` | `validar_tudo.ps1 -SemTerminal` (passos 1–5, pendências e o zip; pula compilação e aceitação) e confere o zip: `MANIFESTO.txt` presente, só `.json/.md/.txt/.log`, nenhum nome com cara de segredo; publica `relatorios/` e o zip como artefato `validar-tudo-sem-terminal` | passo com código ≠ 0, zip ausente/irregular, ou `-SemTerminal -ConfirmoContaDemo` não recusado com código 2 |
| `mql` (**experimental**, `continue-on-error: true`) | `ci_preparar_metaeditor.ps1`: instala o MetaTrader oficial com `/auto` (MT4 se o instalador ainda existir, depois MT5), localiza `metaeditor.exe`/`metaeditor64.exe`, baixa os headers de `dingmaotu/mql-zmq` e `dingmaotu/mql4-lib` (Apache-2.0, commits fixos, **só na CI**, nada entra no repositório) para `MQL4/Include`; depois `compilar_ea.ps1 -MetaEditor ... -PastaMql4 ...` roda `/compile: /log: /inc:` e publica o log no artefato `mql-compilacao` e no resumo do job | o log do MetaEditor tem erros, não tem a linha de resultado, ou o `.ex4` não é gerado (o job aparece vermelho, mas não derruba o workflow enquanto for experimental) |

Limites honestos do job `mql`:

- A MetaQuotes não distribui mais o MT4. Se só o MT5 instalar, a compilação usa o `metaeditor64.exe`, que **pode não
  compilar `.mq4`**; o resumo do job diz qual MetaEditor foi usado e o resultado é registrado como vier. Só um 0 erros
  do MetaEditor **do MT4** vale como "o EA compila"; do MT5 é indício, não prova.
- Sem MT4 instalado não há o `stdlib.mqh` original; a CI grava um **stub** que só declara `ErrorDescription` (importado
  de `stdlib.ex4`, como no original) e o resumo do job avisa isso.
- Compilar não testa o EA: comportamento no terminal, DLLs (`libzmq.dll`) e a conta demo continuam dependendo do
  usuário (docs/12).
