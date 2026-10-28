# 09 — Modelos de IA, prompts e versões

Papéis (`config/robowin.toml`): proponente `anthropic:claude-opus-5-5`, revisor `openai:gpt-6.1-sol`, NLP
`anthropic:claude-haiku-5-5`. Catálogo `config/modelos.toml` (preço, corte de conhecimento, `fixavel`);
modelo sem preço ou corte é recusado. GPT-6 não tem snapshot datado: `fixavel=false`, registra-se o modelo
servido e roda-se regressão diária. Claude Opus 5.5 não aceita `temperature` (sem determinismo): repetir N
vezes para medir variância e usar o cache de gravação/repetição para backtests. Trocar de modelo, adicionar
Gemini/Ollama/OpenAI-compatível ou criar braços extras é só configuração (`robowin ia testar`). Prompts em
`config/prompts/`, versionados por hash; proibido citar indicadores técnicos; nunca pedir raciocínio literal.

**Braco experimental de debate e memoria reflexiva (desligados por padrao).** Papeis extras em `[ia.papeis]`:
`pesquisador` (altista e baixista), `debatedor_risco` (agressivo, neutro, conservador), `sintetizador` (faz o papel de
proponente) e `reflexao`. O braco `tipo = "debate"` de `[[bracos]]` so existe no modo condicional, ve SO o dossie
anonimizado (sem ferramentas nem internet), valida cada etapa de forma deterministica e entrega uma `PropostaIA` comum
(mesmos validadores, revisor opcional e veto do risco). A memoria reflexiva (`[reflexao]`) grava a licao SO depois do
resultado liquidado e do atraso configurado, e a injeta no dossie apenas apos `t_disponivel`. Ambos sao hipoteses a
medir (H4 no manifesto), nao melhorias comprovadas. Origem das ideias e decisoes: `docs/13b-estudo-aurum-tradingagents.md`.
