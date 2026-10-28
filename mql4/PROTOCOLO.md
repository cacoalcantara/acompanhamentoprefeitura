# Protocolo da ponte MT4 (versão 1)

> **Aviso:** o Expert Advisor `RobowinBridge.mq4` **não foi compilado nem testado** (sem MetaEditor/MT4/Windows
> neste projeto). Este documento é o contrato; o lado Python (`src/robowin/mt5/conector_mt4.py`) e o dublê
> (`src/robowin/mt5/duble_mt4.py`) são testados contra ele. Qualquer divergência encontrada ao compilar o EA
> deve ser corrigida **no EA**, não no contrato (ou no contrato e nos três lugares, com nova versão).

## Transporte
- ZeroMQ **REQ** (cliente Python) → **REP** (EA), em `127.0.0.1` (padrão `tcp://127.0.0.1:5560`; `ipc://` aceito).
  O EA recusa `BindEndereco` fora de 127.0.0.1.
- Uma mensagem = um JSON UTF-8 **compacto** (sem espaços), só ASCII (`\uXXXX` para o resto; o EA troca por `?`).
- Cliente: timeout curto (padrão 1,5 s); no timeout o socket REQ é **recriado** (Lazy Pirate). Leituras tentam
  de novo 1 vez; **comandos de ordem nunca são reenviados** (sem resposta ⇒ ordem INCERTA, reconciliar).
- Por que ZeroMQ e não arquivos: o MQL4 não abre sockets de servidor sozinho; usa-se a biblioteca `mql-zmq`
  (DLL). Exige "Permitir importação de DLL". Arquivos/named pipe não foram implementados.

## Envelope
Requisição: `{"v":1,"id":"<texto único>","cmd":"<comando>","args":{...}}` (+ `"tk":"<token>"` quando o EA tem o input `Token`)

Resposta ok: `{"v":1,"id":"<mesmo id>","ok":true,"dados":{...}}`

Resposta com erro: `{"v":1,"id":"<mesmo id>","ok":false,"erro":{"codigo":<int>,"msg":"<texto>","origem":"mt4|ponte"}}`

- `v` ≠ 1 ⇒ erro 1003. `id` ausente/JSON inválido ⇒ 1001. Comando desconhecido ⇒ 1008.
- **Idempotência:** nos comandos de ordem o EA guarda as últimas 64 respostas por `id`; o mesmo `id` devolve a
  mesma resposta sem reexecutar.

## Erros
`origem: "mt4"`: `codigo` é o `GetLastError()` do MQL4 (ex.: 129 preço inválido, 130 stops inválidos, 131 volume
inválido, 132 mercado fechado, 133 negociação desabilitada, 134 sem margem, 138 requote, 141 muitas requisições,
146 contexto ocupado, 149 hedge proibido, 150 FIFO, 4108 ticket inválido, 4109 trade não permitido).

`origem: "ponte"` (códigos ≥ 1000): 1001 JSON inválido, 1002 argumento inválido, 1003 versão, 1004 símbolo
desconhecido/sem buffer, 1005 ticket, **1006 conta não DEMO (todas as ordens recusadas)**, 1007 EA em
`SomenteLeitura`, 1008 comando desconhecido, 1009 SL/TP recusado após abrir (a posição foi fechada por segurança), **1010 token ausente/inválido**, **1011 limite de posições+pendentes por símbolo/magic (`MaxPosicoes`)**, **1012 magic fora de `Magics`**.

Conversão para `retcode` do MT5 (cliente Python, `protocolo_mt4.retcode_de_erro`): 138/135/136/137 → 10004
(requote); 129 → 10015; 130/145 → 10016; 131/4051 → 10014; 132 → 10018; 133/64/65 → 10017; 134 → 10019; 8/141/146 →
10024; 4109 → 10027; 4108 → 10036; **1/128 → 10012 e 6 → 10031 (INCERTA: pode ter executado)**; demais → 10011.

## Tempo
- `t_s` = hora do **servidor em segundos** (`MarketInfo(MODE_TIME)`); `gtc` = `GetTickCount()` do EA (ms locais,
  volta a zero a cada ~49 dias, só serve para medir intervalos). **Não há milissegundos do servidor.** O cliente
  expõe `time_msc = t_s * 1000` (sem inventar ms) e usa `seq` para ordenar ticks do mesmo segundo.
- `epoca`: identificador da instância do EA (muda ao recarregar o EA; buffers de ticks zeram).

## Comandos de leitura
| cmd | args | dados |
|---|---|---|
| `ola` | `versao_cliente` | `versao_protocolo`, `ea`, `ea_versao`, `epoca`, `duble`, `build`, `caminho`, `caminho_dados`, `conta_demo`, `somente_leitura` |
| `conta` | – | `login`, `servidor`, `empresa`, `moeda`, `nome`, `demo` (`IsDemo`), `trade_allowed` (`IsTradeAllowed`), `expert_enabled` (`IsExpertEnabled`), `conectado`, `dlls_permitidas`, `saldo`, `patrimonio`, `lucro`, `margem`, `margem_livre`, `alavancagem`, `hedging` (sempre `true`), `t_servidor_s`, `gtc` |
| `simbolos` | – | `simbolos`: lista de nomes |
| `simbolo` | `simbolo` | `nome`, `caminho`, `descricao`, `digits`, `ponto`, `visivel`, `spread`, `negociavel`, `execucao`, `tick_size`, `tick_value`, `lot_size`, `lote_min`, `lote_max`, `lote_passo`, `stop_level`, `freeze_level`, `margem_requerida`, `expiracao_s`, `moeda_lucro`, `moeda_base` |
| `selecionar` | `simbolo`, `ativar` | `ok` (SymbolSelect) |
| `tick` | `simbolo` | `t_s`, `t_corrente_s` (`TimeCurrent`), `gtc`, `bid`, `ask`, `seq` |
| `ticks` | `simbolo`, `desde_seq` (−1 = tudo do buffer), `epoca` (a que o cliente conhece; 0 = nenhuma), `max` (≤ 5000) | `epoca`, `ticks`: `[[seq,t_s,gtc,bid,ask,vol_tick],...]`, `seq_max`, `perdeu`, `reiniciou`, `mais` |
| `posicoes` | `simbolo` (vazio = todos) | `posicoes`: itens (abaixo) |
| `pendentes` | `simbolo` | `ordens`: itens (abaixo) |
| `historico` | `de_s`, `ate_s` (servidor) | `itens` (até 500 mais recentes): `ticket`, `tipo`, `simbolo`, `volume`, `p_abertura`, `p_fechamento`, `t_abertura_s`, `t_fechamento_s`, `sl`, `tp`, `lucro`, `comissao`, `swap`, `magic`, `comentario`, `fechado_ea` |

Item de posição/pendente: `ticket`, `simbolo`, `tipo` (0 compra, 1 venda, 2 compra limitada, 3 venda limitada,
4/5 stop), `volume` (lotes MT4), `p_abertura`, `sl`, `tp`, `magic`, `comentario`, `t_abertura_s`, `comissao`,
`swap`, `lucro`, `p_atual`.

**Stream de ticks:** o MT4 não tem `copy_ticks`. O EA grava **cada tick** (`OnTick` do gráfico e polling por
`OnTimer` dos símbolos extras) num **buffer circular** (20 000 por símbolo). O cliente puxa por `seq`; se o
buffer estourar entre duas leituras ⇒ `perdeu:true`; se o EA reiniciar ⇒ `reiniciou:true`. Ambos são lacuna
**real e irrecuperável** (não há histórico de ticks). Ticks com bid/ask/tempo iguais ao anterior são fundidos.

`fechado_ea`: 1 = fechada por `ordem_fechar` desta instância do EA; 0 = fechada depois que o EA iniciou por outro
meio (SL/TP/manual); −1 = fechada antes do EA iniciar (desconhecido). O cliente deriva o `reason` do deal de
saída: `[sl]`/`[tp]`/`so:` no comentário ⇒ SL/TP/SO; senão `fechado_ea=1` (ou −1 com magic) ⇒ EXPERT; senão
manual (CLIENT).

## Comandos de ordem (só conta DEMO, só fora de `SomenteLeitura`)
| cmd | args | dados |
|---|---|---|
| `ordem_abrir` | `simbolo`, `tipo` (0..3), `volume`, `preco` (pendentes; a mercado o EA usa o preço atual), `desvio`, `sl`, `tp`, `magic`, `comentario`, `validade` (`gtc`\|`dia`) | `ticket`, `preco`, `volume`, `t_s` |
| `ordem_fechar` | `ticket`, `volume` (0/≥ aberto = tudo), `desvio` | `ticket`, `preco`, `volume_fechado`, `ticket_restante` (parcial: o restante ganha **novo** ticket, comentário `from #<ticket>`) |
| `ordem_modificar` | `ticket`, `sl`, `tp`, `preco` (só pendentes) | `ticket` |
| `ordem_apagar` | `ticket` | `ticket` |

- **Fechar é sempre por ticket** (`OrderClose`). Não existe "ordem oposta" no protocolo: em conta hedging ela
  abriria outra posição. `ordem_fechar` com ticket que não é posição aberta ⇒ erro 4108.
- `SltpEmSeguida` (EA): abre sem SL/TP e aplica em seguida (corretoras ECN). Se o SL/TP for recusado, o EA
  **fecha a posição** e responde 1009.
- Sem retentativa no EA: erro ⇒ resposta de erro; quem decide é o robô.

## Segurança
Defesa em profundidade no EA (o Python também confere; o `.mq4` continua **NÃO compilado/verificado em MT4 real**):
- `MaxPosicoes` (padrão 1): `ordem_abrir` é recusada (1011) se posições abertas + pendentes do símbolo/magic já
  estiverem no máximo.
- `Magics` (lista separada por vírgula; vazio = sem filtro, não recomendado): `ordem_abrir` só aceita esses magics;
  `ordem_fechar`/`ordem_modificar`/`ordem_apagar` recusam (1012) ticket cujo magic não esteja na lista (posição
  manual, magic 0, nunca é tocada pela ponte).
- `Token` (opcional): se preenchido, **todo** pedido precisa trazer `tk` igual (1010). O cliente Python envia
  `[mt5].mt4_token` ou a variável `ROBOWIN_MT4_TOKEN`. É segredo compartilhado simples, não criptografia: o
  transporte continua restrito a 127.0.0.1.

Ordens só em conta DEMO (EA e cliente conferem, `IsDemo()`); o cliente trata qualquer conta não demo como
real. `trade_mode`/`margin_mode` do `account_info` são derivados: demo ⇒ `demo`, senão `real`; margem sempre
`RETAIL_HEDGING`. O `order_check` do cliente é **simulado localmente** (volume, passo, nível de stop, margem
livre, permissões) porque o MT4 não tem `OrderCheck`.
