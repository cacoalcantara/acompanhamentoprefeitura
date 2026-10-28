# 03 — Dados, integrações e infraestrutura

- **MT5:** pacote oficial `MetaTrader5` (só Windows x64, polling, `order_send` síncrono, 1 terminal por
  processo). Conta DEMO da XP (servidor informado pela corretora); login dedicado ao robô.
- **Coleta:** ticks (`copy_ticks_from`, dedup por `time_msc`), livro L2 agregado (`market_book_*`),
  offset do servidor calibrado, backfill na reconexão. Parquet + DuckDB; decisões e execuções em SQLite.
- **IA:** Anthropic (`claude-opus-5-5`, `claude-haiku-5-5`), OpenAI (`gpt-6.1-sol`), Google, Ollama,
  OpenAI-compatível. Sem região no Brasil; latência via internet.
- **Notícias:** Broadcast (Agência Estado) só após a especificação do contrato; fontes oficiais (BCB,
  IBGE, Fed, BLS) em `config/fontes.toml` com `verificado=false` até validação.
- **Infra sugerida:** Windows com MT5 (PC ou VPS em São Paulo); relógio sincronizado (NTP.br).

## Perfis de instrumento e de bolsa (ativo configurável)
`config/instrumentos.toml` é a única fonte dos números do ativo: `classe`, `bolsa`, `vencimento`
(`quarta_dia15_meses_pares` para WIN/IND; `primeiro_dia_util_mes` para WDO/DOL; `nenhum` para ação/ETF),
`ponto_valor_brl` (por lote, na moeda da bolsa), `tick_pontos`, `lote`, tarifas por vigência, `stop_max_ticks`,
`slippage_folga_ticks`, `stop_padrao_ticks`/`alvo_padrao_ticks`, `aceita_book`, `rlp`. `config/bolsas.toml` traz
fuso, calendário, sessões, moeda, tarifas-modelo, limite de oscilação e regime tributário.
`mt5/contrato.py` valida o símbolo da corretora contra o perfil (tick, valor do ponto por lote, lote, vencimento) e
**recusa** se divergir. **`a_confirmar`** = valor sem fonte confirmada: bloqueia gateway/núcleo/MT5 até o usuário
confirmar; a simulação sintética roda rotulada. Limite conhecido: o relógio interno da simulação ainda usa o fuso
`America/Sao_Paulo`; bolsas de outros fusos só têm o modelo e a operação bloqueada.

## MT4 (ponte EA) — plataforma alternativa
`[mt5].plataforma = "mt5" | "mt4"` em `config/robowin.toml`, ou `--plataforma` em `diagnostico`, `gateway`,
`coletar` e `emergencia`. O MT4 **não tem pacote Python**: `mql4/RobowinBridge.mq4` (Expert Advisor) atende comandos
JSON por ZeroMQ REQ/REP em 127.0.0.1 (contrato em `mql4/PROTOCOLO.md`); `mt5/conector_mt4.py` (`ConectorMT4`) o
adapta ao mesmo Protocolo de terminal (`account_info`, `symbol_info`, ticks com `flags`, `positions_get`,
`history_deals_get`, `order_check` simulado, `order_send`). `mt5/duble_mt4.py` é o dublê rotulado (servidor ZeroMQ
falso, conta hedging, sem livro e sem histórico de ticks). `mt5/capacidades.py` declara as capacidades por
plataforma; capacidade exigida e ausente ⇒ **bloqueio com mensagem** (MM e features de livro desligados no MT4).

| | MT5 | MT4 (ponte) |
|---|---|---|
| Conta | netting (EXCHANGE) | **hedging**: ordem oposta abre outra posição; limite e saída por **ticket** |
| Livro L2 | sim | **não** (features `livro_*`, `ofi_*`, `microprice_*` e MM desligados) |
| Histórico de ticks / backfill | `copy_ticks_*` | **não**: o EA grava cada tick num buffer; lacuna só **bloqueia entradas** |
| Tempo | ms (`time_msc`) | **segundos** (+ `GetTickCount` local); sem ms inventados |
| Ticks de negócio (last, agressor) | sim | **não** (só bid/ask; fluxo agressor indisponível) |
| `order_check` | do terminal | **simulado localmente** (volume, passo, stops, margem) |

### Instalar o EA no MT4 (passo a passo; **não verificado em MT4 real**)
1. MT4 build 600+ (demo da corretora). Instale a biblioteca `mql-zmq` (dingmaotu): copie `Include/Zmq`, `Include/Mql`
   para `MQL4/Include/` e `libzmq.dll`/`libsodium.dll` (32 bits) para `MQL4/Libraries/`.
2. Copie `mql4/RobowinBridge.mq4` para `MQL4/Experts/` e compile no MetaEditor (F7). **O arquivo nunca foi compilado
   pelos autores:** corrija erros de sintaxe/API (pontos marcados `CONFERIR`) antes de seguir.
3. Ferramentas ▸ Opções ▸ Expert Advisors: marque **Permitir negociação automatizada** e **Permitir importação de
   DLL** (necessária ao ZeroMQ). Ligue o botão **AutoTrading** da barra. Desmarque "Desativar negociação automática
   ao trocar de conta/perfil" se quiser que persista.
4. Abra o gráfico do contrato vigente (ex.: o símbolo do ativo) e arraste o EA. Parâmetros: `BindEndereco`
   (127.0.0.1), `Simbolos` (extras, sem espaços), `SomenteLeitura` (comece com `true`), `SltpEmSeguida` (`true` em
   corretoras ECN/market execution que recusam SL/TP na abertura), `MaxDesvio`.
5. Histórico: aba Histórico da Conta ▸ botão direito ▸ **Todo o histórico** (o robô lê as ordens fechadas pelo MT4).
6. Rode `robowin diagnostico --plataforma mt4` (conta DEMO, hedging, símbolo, especificações, `order_check`
   simulado) e só depois `robowin gateway --plataforma mt4`. Rolagem do contrato: mova o EA para o gráfico do novo
   contrato (o buffer de ticks é por símbolo/instância).

### Limites do MT4 (honestos)
- Só o lado Python é testado, contra o dublê. O `.mq4` e a integração real ficam **a confirmar** em um MT4 demo.
- Corretoras brasileiras costumam oferecer MT5 para a B3; o MT4 só serve se a corretora liberar o ativo
  (**a confirmar**). Volume em "lotes" do MT4 e `MODE_MINLOT`/`MODE_TICKVALUE` diferem por corretora: a validação do
  símbolo contra o perfil recusa divergências (falha segura).
- Precisão de 1 s: ordem dos ticks do mesmo segundo vem do `seq` do EA; latências abaixo de 1 s não são mensuráveis
  pelo carimbo do servidor. Estratégias que exigem milissegundos, livro ou fluxo agressor ficam bloqueadas.
- Sem DLL/AutoTrading o EA não responde: o cliente dá timeout, o gateway bloqueia entradas (feed parado) e o
  guardião continua tentando zerar.
