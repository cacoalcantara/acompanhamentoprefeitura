# 01 — Viabilidade e limitações

1. **Milissegundos só no caminho determinístico.** Polling do MT5, features e risco cabem em dezenas de ms.
   Com Claude Opus 5.5 (thinking sempre ligado; TTFT ≈ 12 s em effort `low`, medição de terceiros) a
   cadeia Claude+GPT leva de 20 a 35 s. Alternativa implementada: **plano condicional** (a IA planeja e é
   revisada antes; o motor dispara em ms) e **modo imediato** com validade de 60 s, comparados.
2. **Meta de 80–90% de acerto.** Com stop 300 e alvo 100 pts o acerto de equilíbrio é ≈ 76–78%; entradas
   aleatórias com essa geometria já acertam ~75% antes de custos. A métrica primária é a expectativa
   líquida com IC; o acerto vem sempre com IC de Wilson e ao lado do controle aleatório.
3. **Economia com 1 contrato.** Custos fixos ≈ R$ 112/pregão contra expectativa hipotética ≈ R$ 14.
   A IA custa R$ 6–12 por operação por braço. Resultado inconclusivo = uso do LLM não justificado.
4. **Concordância entre modelos não prova lucro.** O valor do revisor é o resultado das operações que
   ele rejeitou (contrafactual gratuito, `avaliacao.comparacao.valor_do_veto`).
5. **Market making** de varejo: fim da fila, concorrência com HFT, ~0,5 tick de tarifa por ida e volta,
   seleção adversa provável. Só no simulador com fila conservadora; a demo não tem fila.
6. **RL de execução:** ganho limitado a ~1 tick (limite, não medição).
7. **StatArb:** arbitragem pura é de HFT; v1 opera só WIN, pernas múltiplas em modo sombra.
8. **NLP:** contexto e janelas de risco, não reação a manchetes. Fontes `verificado=false` até validação.
9. **Viés de antecipação:** corte de conhecimento até jun/2026; backtests de braços com LLM antes de jul/2026
   são contaminados. Avaliação oficial = prospectiva.
10. **Dados:** o MT5 não guarda histórico de livro; features de book só da coleta própria. Os portões
    estatísticos levam **meses** (ver `06-roteiro.md`).
11. **Gap:** 1% do Ibovespa ≈ R$ 353/contrato; limite de 10% ≈ R$ 3,5 mil, acima do limite de perda do
    usuário. Cenários em `robowin.risco.estresse`; ciência explícita exigida antes da etapa real.
12. **Demo ≠ real:** a demo preenche limitadas sem fila; SL/TP do MT5 ficam no servidor da corretora e
    saem como ordem a mercado.
13. **Regulação/tributos:** não é parecer jurídico. Day trade 20% (IRRF 1%/dia). Uso próprio não exige
    registro na CVM (inferência). Políticas de uso de Anthropic/OpenAI a revisar antes da etapa real.
