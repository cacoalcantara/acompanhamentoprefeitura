# 09 — Modelos de IA, prompts e versões

Papéis (`config/robowin.toml`): proponente `anthropic:claude-opus-5-5`, revisor `openai:gpt-6.1-sol`, NLP
`anthropic:claude-haiku-5-5`. Catálogo `config/modelos.toml` (preço, corte de conhecimento, `fixavel`);
modelo sem preço ou corte é recusado, salvo declaração explícita (abaixo). GPT-6 não tem snapshot datado: `fixavel=false`,
registra-se o modelo servido e roda-se regressão diária. Claude Opus 5.5 não aceita `temperature` (sem determinismo):
repetir N vezes para medir variância e usar o cache de gravação/repetição para backtests. Prompts em
`config/prompts/`, versionados por hash; proibido citar indicadores técnicos; nunca pedir raciocínio literal.

## Qualquer IA, qualquer modelo (não só Claude e GPT)

Três arquivos, nenhuma linha de código:

| Arquivo | O que diz |
|---|---|
| `config/provedores.toml` | **provedor** → adaptador, `base_url`, variável de ambiente da chave, região, observações |
| `config/modelos.toml` | **modelo** (`provedor:modelo`) → preço, data de corte, `fixavel`, `local` |
| `config/robowin.toml` | **papéis** (`[ia.papeis.*]`) e **braços extras** (`[[bracos]]`) apontando para `provedor:modelo` |

Provedores já cadastrados: `anthropic`, `openai`, `azure_openai`, `google` (Gemini API), `gemini_openai` (Gemini pelo
endpoint compatível com OpenAI), `gemini_vertex` (Gemini no Vertex), `aws_bedrock` e `google_vertex` (Claude no Bedrock
e no Vertex, pelos clientes de plataforma do SDK `anthropic`), `mistral`, `deepseek`, `xai` (Grok), `groq`, `together`,
`fireworks`, `openrouter` (centenas de modelos com uma chave), `perplexity`, `cohere`, `cerebras`, `dashscope`
(Qwen), `moonshot` (Kimi), o genérico `openai_compativel` e os locais `ollama`, `lmstudio`, `vllm`, `llamacpp`.
`verificado = true` só onde a `base_url` foi conferida nesta sessão (nos SDKs oficiais instalados); os demais estão
`verificado = false` (valores da documentação pública, que não estava acessível sem rede): confira antes de usar.
`uv run robowin ia listar` mostra provedores, modelos e se a chave está **presente/ausente** (o valor nunca aparece).

### Passo a passo (Windows)

1. **Chave** (uma vez; abra um terminal NOVO depois do `setx`):
   ```
   setx DEEPSEEK_API_KEY "sua-chave"
   ```
   A variável de cada provedor está em `chave_env` no registro (`MISTRAL_API_KEY`, `XAI_API_KEY`, `GROQ_API_KEY`,
   `OPENROUTER_API_KEY`, `GEMINI_API_KEY`...). Locais (Ollama, LM Studio, vLLM, llama.cpp) não precisam de chave.
2. **Modelo** no catálogo:
   ```
   uv run robowin ia adicionar-modelo deepseek:deepseek-chat --preco-entrada 0.3 --preco-saida 1.2 --corte 2025-07-01
   uv run robowin ia adicionar-modelo ollama:llama3.1:8b --corte 2023-12-01            (local: preço zero)
   uv run robowin ia adicionar-modelo openrouter:autor/modelo --preco-desconhecido      (sem preço e sem corte)
   ```
   Preços em US$ por milhão de tokens (`--preco-cache` opcional; `--fixavel` se o id for um snapshot fixo).
3. **Teste** (sem chave falha com a mensagem do que falta; `--falso` ensaia a bateria sem rede):
   ```
   uv run robowin ia testar --modelo deepseek:deepseek-chat
   ```
4. **Braço**: descomente um dos exemplos de `config/robowin.toml` (Gemini, DeepSeek, Grok, Mistral, Llama local via
   Ollama, OpenRouter — todos desligados por padrão) ou troque o modelo de qualquer papel:
   ```toml
   [[bracos]]
   id = "D_deepseek"
   proponente = "deepseek:deepseek-chat"     # provedor:modelo ou nome de um papel de [ia.papeis]
   revisor = "revisor"                       # opcional
   modo = "imediato"
   ```
   O braço entra no manifesto, no painel e na correção de Holm como qualquer outro. Simulação: `uv run robowin simular`
   (provedor FALSO, sem chave) ou `--provedor real` (sem a chave de algum provedor usado, recusa dizendo qual variável falta).

**Provedor novo** (que ainda não está no registro): adicione uma entrada e pronto.
```toml
[provedor.meu_provedor]
adaptador = "openai_compativel"
base_url = "https://api.meu-provedor.exemplo/v1"
chave_env = ["MEU_PROVEDOR_API_KEY"]
saida_estruturada = "json"     # json_schema | json | texto (o que o servidor aceitar)
verificado = false
```

### Falha segura com qualquer modelo

- **Sem preço**: só com `--preco-desconhecido`. Cada chamada reserva e cobra do orçamento um **teto conservador**
  (`teto_chamada_usd`; padrão US$ 1,00, acima do pior caso dos modelos caros do catálogo) e o manifesto recebe
  `rotulo:<modelo> = preco_desconhecido`.
- **Sem data de corte**: o corte vira o futuro distante (`9999-12-31`); o braço é **contaminado em qualquer backtest
  histórico** (`contaminacao:<braco>` no manifesto; em sessão sobre dados históricos a conta ganha o rótulo
  `contaminado_corte_desconhecido` e um incidente). Só vale avaliação prospectiva (dados posteriores ao uso).
- Modelo fora do catálogo, provedor fora do registro ou chave ausente no modo real: recusa com mensagem clara.
- O modelo servido é registrado em toda chamada; ids sem snapshot (`fixavel=false`) e roteadores (OpenRouter) podem
  trocar de modelo por trás: rode `robowin ia testar` com frequência.
- Perplexity pesquisa na internet; DeepSeek processa dados na China; avalie antes (observações no registro).

### Testes sem chave e sem rede

`tests/conftest.py` remove do ambiente todas as variáveis `*_API_KEY` e as credenciais do registro antes de cada teste.
`tests/unit/test_ia_qualquer_provedor.py` roda a bateria de contrato (ok, prazo, 429, recusa, max_tokens, JSON
inválido, modelo servido trocado) contra **todo** provedor do registro com transporte falso: respostas HTTP fabricadas
no formato da API para os SDKs instalados (`anthropic`, `openai` e todos os compatíveis com OpenAI) e cliente falso no
formato do SDK para os opcionais (google-genai, ollama); confere o cliente e a `base_url` de cada provedor e roda uma
simulação ponta a ponta com um braço DeepSeek (modo real, chaves fictícias, transporte falso). Qualquer conexão para fora
da máquina derruba o teste. A bateria achou e corrigiu dois rótulos errados: com `output_format`/`text_format` os SDKs
validavam o JSON no meio do stream, e recusa/corte por max_tokens (Anthropic) ou JSON inválido (OpenAI) viravam
`ValidationError` genérico; agora o schema vai como dicionário e a validação é da camada comum.

**Braco experimental de debate e memoria reflexiva (desligados por padrao).** Papeis extras em `[ia.papeis]`:
`pesquisador` (altista e baixista), `debatedor_risco` (agressivo, neutro, conservador), `sintetizador` (faz o papel de
proponente) e `reflexao`. O braco `tipo = "debate"` de `[[bracos]]` so existe no modo condicional, ve SO o dossie
anonimizado (sem ferramentas nem internet), valida cada etapa de forma deterministica e entrega uma `PropostaIA` comum
(mesmos validadores, revisor opcional e veto do risco). A memoria reflexiva (`[reflexao]`) grava a licao SO depois do
resultado liquidado e do atraso configurado, e a injeta no dossie apenas apos `t_disponivel`. Ambos sao hipoteses a
medir (H4 no manifesto), nao melhorias comprovadas. Origem das ideias e decisoes: `docs/13b-estudo-aurum-tradingagents.md`.
