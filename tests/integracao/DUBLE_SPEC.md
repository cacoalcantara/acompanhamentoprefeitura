# Comportamento garantido do `DubleMT5` (src/robowin/mt5/duble.py)

O dublê é um **dublê de testes** (`eh_duble = True`), alimentado só por séries **sintéticas** passadas pelo chamador
(`gerar_ticks_sinteticos`, `gerar_livros_sinteticos`, `criar_duble_padrao`). Nada aqui representa o mercado real.
Implementa o Protocolo `TerminalMT5`. O tempo vem do `Relogio` injetado: a cada chamada, ticks com instante UTC
≤ agora são "processados" (atualizam cotação, disparam SL/TP e ordens limitadas).

| Área | Garantia |
|---|---|
| Conta | `account_info`: `trade_mode` demo(0)/contest(1)/real(2), `margin_mode` EXCHANGE (netting), `trade_allowed`, `trade_expert`. `trocar_conta()` muda login/servidor/modo no meio da sessão (estado de posições/ordens/deals é por conta). Sem `initialize()` ou desconectado, as consultas devolvem `None` e `last_error()` informa. |
| Netting | Uma posição por símbolo. Ordem do mesmo lado soma (preço médio); oposta reduz; maior que a posição **inverte** (deal `INOUT`, **novo ticket**). Com `position=<ticket>`: ticket inexistente → 10036; lado igual ou volume maior que a posição → 10013 (`rejeita_ticket_velho=False` desliga isso e inverte, para provar que o executor se protege sozinho). |
| SL/TP | Vivem no "servidor": disparam como ordem a mercado mesmo sem cliente. SL executa ao preço do tick que o rompe (pior preço da lacuna); TP ao preço do alvo. Deals com `reason` SL(4)/TP(5). |
| order_send | Retornos 10009 (executada), 10008 (pendente colocada), 10010 (parcial), 10013, 10014 (volume), 10015/10016 (preço/stops), 10017, 10018, 10024, 10027, 10030 (preenchimento), 10031, 10036. `injetar_falhas(Falha(retcode, executa, volume))`: `None` → devolve `None`; `executa=True` → a ordem **executa no servidor** mas o cliente recebe falha (caso INCERTA). `injetar_falhas_check` faz o mesmo para `order_check`. |
| Parcial | `Falha(10010, volume=v)` executa só `v`. |
| Ordens pendentes | `BUY_LIMIT`/`SELL_LIMIT` executam ao preço limite quando a cotação cruza; `orders_get`, REMOVE/MODIFY/SLTP suportados. |
| Preenchimento | Validado contra `symbol_info.filling_mode` (FOK/IOC) e `trade_exemode` (EXCHANGE). |
| Relógio do servidor | `offset_servidor_s` ≠ 0 por padrão (10800 s). `time`/`time_msc` de ticks, posições, ordens e deals saem em hora de servidor; `copy_ticks_*` e `history_*` interpretam datas na mesma convenção (datetime rotulado UTC = hora de parede do servidor). |
| Ticks | `copy_ticks_from/range` devolvem `ndarray` estruturado (`time, bid, ask, last, volume, time_msc, flags, volume_real`), só ticks já visíveis. Flags BID/ASK/LAST/VOLUME/BUY/SELL; filtros `COPY_TICKS_INFO/TRADE`. Vários ticks no mesmo ms são permitidos. |
| Backfill | `desconectar()` → leituras `None`; `reconectar()` → `copy_ticks_range` entrega os ticks do período da queda. |
| Livro | `market_book_add` só se `ticks_bookdepth > 0`; `market_book_get` devolve L2 **agregado por preço**, vendas decrescentes depois compras decrescentes, limitado à profundidade. |
| Símbolos | `symbols_get(grupo)` (curingas e `!exclusão`): WINZ26, WING27, INDZ26, WDOX26, BOVA11 sintéticos. `symbol_info`: tick 5, R$ 0,20/ponto (`trade_tick_value`=1,00), lote 1, `filling_mode`, `trade_exemode`, `ticks_bookdepth`, `expiration_time`. `especificacoes` é mutável para simular divergências. |
| Intervenção manual | `operar_manual()` gera deal com `reason` CLIENT/MOBILE/WEB (para testar a reconciliação). |
| Observabilidade | `envios` (cada `order_send` recebido), `checks`, `deals_todos`, `posicao_liquida()`, `volume_pendente()`, `gancho_antes_order_send`. |

Não modelado: margem/saldo realistas, leilões, limites de oscilação, custos, fila no livro, latência de rede.
