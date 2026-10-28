# 10 — Pendências e fontes a confirmar

Ver o plano. Principais: confirmar por escrito com a corretora (robôs/API no MT5, corretagem zero para robô,
zeragem, margem, mensagens, profundidade do book, Stop Limit nativo, logins demo); API do Broadcast;
chaves de API; ofício da grade de 03/11/2026 e tarifação V4.8; parâmetros da política de mensagens da B3;
políticas da Anthropic (12/11/2026) e da OpenAI (20/11/2026); validar as URLs/horários de `config/fontes.toml`
(todas `verificado=false`); medir no seu Windows: `order_check`, saída com ticket velho, horário de
`copy_ticks`/deals, offset do servidor; confirmar preços de modelos. Hosts bloqueados na sessão de
desenvolvimento impediram a verificação direta de B3, OpenAI, CVM, Receita e corretoras.

**MT4 (ponte EA), a confirmar:** compilar e testar `mql4/RobowinBridge.mq4` em um MT4 demo (nunca foi compilado);
conferir a API da biblioteca `mql-zmq` (recv/send/setLinger) e as constantes `SYMBOL_*`; confirmar com a corretora
se o ativo existe no MT4, o tamanho do lote (`MODE_MINLOT`), `MODE_TICKVALUE`, nível de stop, se SL/TP na abertura
é aceito (senão `SltpEmSeguida`), execução (instant/market) e o histórico da conta ("Todo o histórico").

> **Atualização 28/10/2026:** o EA `mql4/RobowinBridge.mq4` compila sem erros no MetaEditor oficial do MT4 (CI, job
> `mql`). Continua pendente executá-lo num terminal MT4 com conta DEMO (aceitação A11).
