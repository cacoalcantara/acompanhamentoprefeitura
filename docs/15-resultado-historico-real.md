# 15 — Primeiro replay com dados REAIS da B3 (CI `historico-b3`, execução 1, 29/10/2026)

Replay walk-forward **causal** de 9 pregões reais do WIN (09/10 a 27/10/2026), negócios intradiários oficiais da B3
(`arquivos.b3.com.br/rapinegocios`). Para cada pregão o modelo só foi treinado com pregões anteriores, o replay
seguiu o instante de disponibilidade e nenhum braço via o futuro. Todos os pregões são posteriores ao corte de
conhecimento dos LLMs do catálogo (jun/2026). Limitações: lado agressor **inferido** (regra do tick), sem livro,
**sem chaves de API** ⇒ os braços "claude"/"claude_gpt" rodaram com o provedor **FALSO** (respostas sem
inteligência; servem só para provar o encanamento). Tempo: 584 s; memória: 2,0 GB.

| Braço | n | Acerto (IC Wilson 95%) | Expectativa/op (IC 95% bootstrap) | Resultado |
|---|---|---|---|---|
| quant (modelo de microestrutura, sem LLM) | 29 | 44,8% [28,4; 62,5] | R$ −1,09 [−3,09; +1,26] | R$ −31,50 |
| quant_cond | 23 | 39,1% | R$ −3,89 [−5,31; −1,77] | R$ −89,50 |
| controle aleatório | 53 | 34,0% [22,7; 47,4] | R$ −4,31 [−7,84; −2,60] | R$ −228,50 |
| claude / claude_gpt (IA FALSA) | 53 / 59 | 30,2% / 33,9% | R$ −4,25 / −3,58 | R$ −225,50 / −211,50 |
| não operar | 0 | — | — | R$ 0,00 |

Leitura honesta: **nenhum braço mostrou vantagem**. O quant foi o menos ruim e é o único cujo IC inclui zero,
mas 29 operações em 7 pregões é amostra insuficiente (selo `nao_validado`; o pré-registro exige centenas de
operações). O risco funcionou: bloqueios por perdas seguidas, limite diário, rolagem e pausa após perda aparecem
nos motivos. A geometria padrão do perfil (stop 250 / alvo 30 pontos) exige acerto de 92–94% só para empatar; ela é
hipótese a revisar no manifesto, não recomendação. Os braços com IA só têm significado com chaves reais
(`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` como *secrets* do repositório ou no seu Windows). O relatório completo
(por dia, qualidade dos dados, latências) está no artefato `historico-b3` da execução.
