# Especificação final: a IA decide o trade inteiro e o risco determinístico mantém o veto

> Especificacao gerada por workflow multiagente (4 leitores, 3 projetos, 3 juizes, sintese) em 29/10/2026.
> Decisao do usuario: a IA VE o envelope derivado dos limites (`[planejador].expor_envelope = "envelope"`):
> pontos, unidades, segundos e se ainda pode entrar; nunca valores em R$ de limites, arquivos, usuario ou conta.


Esta etapa foi só de leitura. O ponto de partida é a Proposta 2 (plano-completo, 2 de 3 votos), com ideias enxertadas das Propostas 1 e 3. As falhas graves apontadas pelos julgamentos foram eliminadas. Antes de escrever, conferi no código os nomes e números de linha citados.

Raiz do repositório: `/home/user/acompanhamentoprefeitura`. Os caminhos de código são relativos a `src/robowin/`; os caminhos de `config/`, `tests/`, `docs/` e `.github/` são relativos à raiz.

---

## 1. Objetivo: o que a IA passa a decidir e o que continua determinístico

### 1.1 Objetivo

Criar uma família de braço nova, **`planejador`** (variantes P e PC), em que um único papel de IA plugável (`[ia.papeis.planejador]`, qualquer `provedor:modelo`) devolve um **`PlanoTradeIA`** com toda a decisão de trading:

- O código **compila** esse plano para a DSL de plano condicional que já existe (`dsl_plano.py` / `plano_condicional.py`).
- Regras de gestão declaradas pela IA são executadas a cada tick por código puro (`decisao/gestao.py`).
- Toda ação passa pelo `MotorRiscoPadrao`.

Os braços B, C, quant, controles e debate continuam como variantes pré-registradas. As correções de bug que mudam o comportamento deles recebem o rótulo `v1.1` no manifesto.

### 1.2 O que a IA decide (lista completa)

**Na entrada (posição zerada):**
1. Entrar ou não: `acao ∈ {COMPRAR, VENDER, NAO_OPERAR}`. Não existe `Acao` nova.
2. Lado.
3. Momento:
   - `gatilhos` vazio significa "agora": entra no primeiro tick depois de armado, se o preço ainda estiver dentro da faixa;
   - `gatilhos` na DSL que já existe (`preco_toca`, `preco_rompe_acima/abaixo`, `spread_max_pontos`, `janela_tempo`) significa entrada condicional;
   - `expira_s` define quanto tempo o plano fica armado.
4. Faixa de execução assimétrica: `faixa_contra_pontos` (quanto o preço pode andar contra a tese antes de desistir) e `faixa_favor_pontos` (quanto pode andar a favor, sem perseguir).
5. Quantidade, até o teto do envelope. No WIN a v1 fica em 1 (`TETO_CONTRATOS_V1`).
6. Stop e alvo em pontos a partir da referência de t0. Os **níveis absolutos que a IA viu ficam fixos** (ancoragem em t0).
7. Tempo máximo da posição (`tempo_max_s`).
8. Regras de gestão automática, executadas por código entre as chamadas: `breakeven`, `trailing`, `parcial`, `sem_progresso`, `apertar_apos`, `ao_vencer_tempo`.
9. Quando quer ser chamada de novo: `reavaliar_em_s` e `reavaliar_se` (perto do stop, perto do alvo, metade do tempo, regra disparou, lucro acima do stop).
10. `p_alvo`: a probabilidade declarada de o alvo sair antes do stop e do tempo. Só é pontuada. **Nunca libera nem bloqueia entrada.**
11. `manter_plano_vigente`: não substituir um plano já armado.

**Com posição aberta:**

12. `ENCERRAR`, preso ao ticket visto em t0.
13. `MANTER` sem ajuste: só fica registrado.
14. `MANTER` com `ajuste`, com níveis em **pontos de resultado a partir do preço médio de entrada** (−30 perde 30; 0 é empate; +10 trava 10):
    - apertar o stop (`stop_resultado_pontos`);
    - mover o alvo nos dois sentidos (`alvo_resultado_pontos`);
    - mudar o tempo total (`tempo_max_total_s`), dentro do teto do usuário;
    - realizar parcial (`reduzir_quantidade`, de 1 a q−1);
    - trocar as regras de gestão (`substituir_gestao`).

### 1.3 O que continua determinístico e fora do alcance da IA

**Veto final.** O `MotorRiscoPadrao.avaliar` decide sobre toda `Intencao`: entrada, disparo de plano, ajuste da IA, ajuste automático e redução. Ele é *fail-closed*: qualquer ação fora de `COMPRAR` ou `VENDER` que não seja tratada explicitamente recebe `acao_desconhecida`.

**Limites do usuário+conta** (`risco/usuarios.py`, aprovados por hash do arquivo):
- perda por operação, dia e semana; drawdown;
- operações por dia, perdas seguidas e pausa;
- quantidade e exposição; stop máximo;
- janelas de horário;
- o novo `tempo_max_posicao_s`.

**Proteções do motor:**
- stop obrigatório, com mínimo de `STOP_MIN_TICKS`, que só aperta e fica a pelo menos `DIST_MIN_STOP_TICKS·tick + spread` do preço executável;
- sem martingale, piramidação, preço médio ou inversão;
- contabilidade por episódio.

**Proteções que não dependem da IA:**
- SL e TP no servidor ou no simulador desde a entrada;
- saída por tempo em `_saidas`, sempre com o limite `min(meta, teto do usuário)`;
- `precisa_zerar` (kill, fora do pregão, zeragem, rolagem, limite de oscilação);
- `GestorSaidas`, com um dono por posição;
- guardião do gateway;
- conta REAL bloqueada;
- `order_send` só em `mt5/execucao_demo.py`.

**A IA não lê nem altera limites.**
- Ela nunca recebe o `ConfigRisco`, arquivos, hashes, usuário ou conta.
- Recebe no máximo um **envelope derivado, só de leitura**, em pontos, unidades e segundos (`EnvelopeDecisao`), calculado pela mesma função que o motor usa.
- Nenhum valor em dinheiro de limite vai para o dossiê do planejador.
- O esquema tem `extra="forbid"`: não existe campo que chegue a um limite.
- Qualquer nome de `ConfigRisco` dentro de `[planejador]` é recusado por `validar_ia_sem_limites`.

**Ponto a confirmar com o usuário.** O padrão é `[planejador].expor_envelope = "envelope"`. Se "a IA nunca lê limites" for literal, existe a variante `"nenhum"`: o dossiê leva só `entradas_possiveis`, `stop_min_pontos` e `dist_min_ajuste_pontos`, e o resto passa a ser vetado sem aviso prévio.

---

## 2. Correções de bugs e de problemas empíricos

Os itens marcados **[v1.1]** mudam o comportamento de B, C, quant ou controles e ficam rotulados no manifesto.

| # | Problema (onde) | Correção (WP) |
|---|---|---|
| C1 | O aviso de "92–94%" junta o teto de stop 250 com o alvo padrão 30, combinação que nenhum braço usou (`avaliacao/relatorio.texto_aviso_acerto` l.31-57; `historico/relatorio.py` l.82-85; `docs/15-resultado-historico-real.md:21`) | Aviso por braço com a geometria realizada (medianas das intenções executadas). Sem geometria, o texto diz "teto, não geometria usada". Doc 15 passa a dizer 30/30/60 s (FALSO 30/40/300 s; equilíbrio ≈59% e ≈52%). O teste `test_aval_impostos_relatorio.py:132-138` cobre os dois caminhos (WP8) |
| C2 | Dossiê anuncia `stop_max_pontos=300` e o validador corta em 250 (`orquestrador._dossie` l.894-896) | Fonte única `MotorRiscoPadrao.envelope()`. O dossiê v1 passa a mostrar o máximo efetivo para q=1 **[v1.1]** (WP1, WP7) |
| C3 | O retorno de `modificar_stop` é ignorado e a decisão vira "executada" (`roteador.executar_intencao` l.127-129; `orquestrador._processar_resultado` l.689-692) | `ResultadoExecucao.motivo="protecao_nao_aplicada:<m>"` e decisão `nao_aplicada` (WP2, WP7) |
| C4 | Dossiê mostra o stop original depois de um aperto (`MetaPosicao.stop_pontos`) | Stop e alvo atuais lidos de `sim.posicao().stop/alvo` **[v1.1]** (WP7) |
| C5 | `MANTER` e `ENCERRAR` não ficam presos à posição vista em t0 (l.680-693) | `ResultadoDecisao.posicao_ticket_t0`/`quantidade_t0`; se divergir, `ignorada:posicao_mudou_desde_t0`. O motor também confere `Intencao.posicao_ticket` **[v1.1]** (WP0, WP1, WP7) |
| C6 | O dossiê do líder é usado por braço com posição diferente: C com posição nunca recebe gestão (`_ciclo` l.970-975) | O dossiê do líder vale só para braços **sem** posição. Cada braço posicionado tem dossiê próprio; o cache por hash junta casos idênticos **[v1.1]** (WP7) |
| C7 | O stop do `MANTER` é medido do meio na entrega, sem distância mínima (`motor._motivos_manter` l.249-256) | Caminho legado ganha `stop_colado_no_preco`. O planejador usa níveis relativos à entrada (WP1) |
| C8 | Stop menor que 1 tick, arredondado, fica no preço de entrada. `simulador.enviar` (l.236-238) não confere o lado do SL e do TP | Motor: `stop_abaixo_do_minimo` (`< STOP_MIN_TICKS·tick`) e `alvo_abaixo_de_um_tick`. Simulador: `RET_INVALID_STOPS` com SL ou TP do lado errado da cotação (WP1, WP2) |
| C9 | `_motivos` manda qualquer ação que não seja NAO_OPERAR/ENCERRAR/MANTER para `_motivos_entrada`, onde `lado = VENDA if not COMPRAR` (motor.py:243-261) | Despacho *fail-closed*: `COMPRAR`/`VENDER` vão para entrada; qualquer outra ação, para `acao_desconhecida` (WP1) |
| C10 | **Achado novo, conferido no código.** Em `simulador._aplicar`, `_criar_alvo(c, p)` roda **antes** de `p.qtd = novo`, então a ordem de alvo nasce com `restante=1` mesmo quando q>1 | Criar o alvo depois de atualizar `p.qtd`. Depois de uma parcial, `restante = min(restante, p.qtd)` (WP2) |
| C11 | Uma parcial apaga `meta`: `_registrar_evento_sim` chama sempre `_fechar_posicao` (l.391) | `EventoSim.restante`. Se for maior que 0, segue para `_registrar_parcial`, que não zera a meta (WP2, WP7) |
| C12 | `registrar_trade` conta cada trade: perdas seguidas, `ultimo_tamanho` e martingale ficam errados com parciais (`risco/sessao.py:66-79`) | `registrar_parcial` (só P&L) e `registrar_trade` ao fechar o episódio, com o líquido somado e `ultimo_tamanho = q máximo do episódio` (WP1, WP2) |
| C13 | O espelho DEMO não propaga o aperto de stop: `espelho.py` não tem SLTP e `gateway.tratar` não tem `modificar_sltp` | `EspelhoDemo.espelhar_protecao` vai para `gateway.tratar("modificar_sltp")`, depois `guardiao.avaliar_sltp`, depois `executor.modificar_sltp` (que já existe em `execucao_demo.py:274`) (WP3) |
| C14 | `espelhar_entrada` usa `contratos_max`: em forex com `volume_max_lotes`, espelha 1 unidade | Usa `motor.unidades_max` (WP3) |
| C15 | `RevisaoIA` aprova só com `{"veredito":"APROVAR"}` porque os booleanos têm padrão `True` (`core/tipos.py:284-290`) | `RevisaoPlanoIA` com `coerente`, `dados_ok` e `regras_ok` obrigatórios. `RevisaoIA` de C fica intacta (WP0) |
| C16 | Injeção de prompt: textos do proponente vão ao revisor e à reflexão sem `_RE_PERIGOSO` nem anonimização (só `ia/reflexao.py:35` aplica) | `ia/validadores.texto_seguro()` em todo texto livre do planejador, do proponente (antes do revisor), do debate e de `resumo_ultima_entrada` **[v1.1]** (WP4, WP6, WP7) |
| C17 | Evidências não conferem o valor citado e aceitam campo `null` (`validadores.validar_evidencias`) | `conferir_valores=True` no planejador: o valor de `campo = valor` confere com tolerância de 1% ou 0,01, e citação a `null` é recusada (WP4) |
| C18 | `min_desde_abertura` usa 9h fixo; o `de` calculado não é usado (`orquestrador.py:898,906`) | Usar `de` de `limites_janela_s` **[v1.1]** (WP7) |
| C19 | A IA é chamada (e paga) quando nenhuma entrada é possível; `perdas_seguidas_maximo` x482 conta tentativas (`_grupo_ativo` l.784-793) | `motor.entradas_possiveis()` em `_grupo_ativo` para grupos de IA e do quant (os controles mantêm o fluxo de RNG). Relatório separa tentativas de episódios (WP1, WP7, WP8) |
| C20 | Desvio simétrico de 4 ticks contra latência de LLM de 6–15 s (x571) | O planejador é sempre plano com faixa assimétrica e níveis ancorados em t0 (`validade.ajustar_entrada_ancorada`). O legado continua como está, documentado (WP5) |
| C21 | `resposta_tardia`, `erro_http` e `divergencia_revisor` são injeções do FALSO (`historico/runner.py:74-75`, `simulacao.revisao_falsa`) | Seção "injetado (FALSO)" separada no relatório, nunca usada em comparação (WP8) |
| C22 | Máquina do Tempo nunca disponível no CI (15 pregões contra `MIN_SESSOES=20`; `.github/workflows/historico-b3.yml:54`) | `--ultimos "$((DIAS + 25))"` (WP8) |
| C23 | Rótulos do quant otimistas (`modelos/rotulagem.py:109-127`: alvo por toque, slippage de 1 tick) com `theta=0` | Variante nova `quant_v2` com `ConfigBarreiras.modelo_execucao="simulador"` (alvo exige atravessar o preço ou passar a fila; slippage médio Pareto) e `theta` só de pregões anteriores. O v1 continua como padrão (WP9) |
| C24 | Importador marca ZIP sintético como REAL (`dados/b3_negocios.py`) | REAL só com URL B3 e sha256 registrados; sem isso, `origem=desconhecida` (WP9) |
| C25 | CLI do histórico usa por padrão `risco-v1.toml` e `risco_do_ativo` (`historico/cli_cmds.py:35,66`) | `--aprovacao` padrão `None`, resolvido por `resolver_aprovacao`; usa `risco_para` (usuário+conta) (WP8) |
| C26 | `Roteador(exigir_aprovacao=False)` é o padrão | Padrão `True` (WP2) |
| C27 | Prompt fixo em WIN, B3, "R$ 0,20" (`config/prompts/proponente_v1.md:1-7`); `mensagem_proponente` afirma "todos os números são pontos relativos" | Prompts do planejador genéricos e `mensagem_planejador` correta. Os prompts v1 ficam congelados por pré-registro, com pendência anotada no doc 16 (WP4) |
| C28 | `FEATURES_PERMITIDAS_PADRAO` (`ia/dossie.py:22`) tem nomes inexistentes | O padrão passa a ser `features.registro.nomes_permitidos()` (WP4) |
| C29 | `_perto` gera falso positivo com valores em dinheiro ou segundos entre 100 e 50.000; o ao vivo não passa `niveis_extras_absolutos` | `_perto` só vale para `movimento`, `posicao`, `statarb`, `features`, `gestao` e `plano_vigente`, e isenta chaves `*_brl`, `*_conta`, `*_s`, `*_min`, `*_restantes`. O ao vivo passa os preços dos ativos auxiliares (WP4, WP7) |
| C30 | **Vazamento (falha grave da P2).** `u_pts = rv·meio` junto com `rv_1m` (retorno log) em `FEATURES_DOSSIE` reconstrói o preço. O v1 já permite uma estimativa grosseira (`rv_1m` em log contra `movimento.*_pts`) | O dossiê v2 **não usa unidade `u`** e não leva features de retorno logarítmico (`FEATURES_DOSSIE_V2 = FEATURES_DOSSIE − {"rv_1m"}`). A volatilidade vai só em pontos (`vol_pts_60s`, `vol_pts_300s`). Há teste de que nenhuma feature v2 tem unidade relativa ou log (WP4, WP7) |
| C31 | Custos mostrados à IA viriam do modelo interno do simulador (`ParamsSlippage`) | `custos.slippage_stop_medio_pontos` = média **causal** dos slippages de stop de pregões anteriores (tabela de episódios), com reserva em `custos_do_perfil` quando n<30. Nunca de `ParamsSlippage` (WP5) |
| C32 | `Prazos.claude_s`/`gpt_s` têm nome de fornecedor | Propriedades `proponente_s`/`revisor_s` e apelidos no TOML. Os campos guardados não mudam, para não alterar o manifesto de B/C (WP5) |
| C33 | `incerteza` não é usada | O planejador grava `p_alvo` e `incerteza` em `previsoes_ia`, pontuados por Brier, BSS e calibração (WP0, WP9) |

---

## 3. Pacotes de trabalho (WP) disjuntos por arquivo

**Regra:** cada arquivo pertence a um único WP. Um WP só altera testes da sua lista. Arquivos novos e alterações de config ficam com o WP indicado.

**Ordem:**
- WP0 entra primeiro, pequeno e bloqueante.
- WP1 a WP5 e WP9 rodam em paralelo depois do WP0.
- WP6 depende do WP0, da API do WP4 (`validar_plano_trade`, `mensagem_planejador`) e do WP5 (`compilar_plano`).
- WP7 integra e depende de WP1 a WP6.
- WP8 depende do WP0 (tabelas), da API do WP9 (`preregistro`) e do WP5 (`dossie_conta`).

Todos podem programar contra as assinaturas abaixo antes de as dependências chegarem.

### WP0 – Contratos compartilhados (bloqueante)

**Arquivos:** `core/tipos.py`, `ia/esquemas.py`, `decisao/bracos/base.py`, `decisao/registro.py`, `dados/decisao_ia.py` (novo), `risco/usuarios.py`, `painel/risco_usuarios.py`, `config/robowin.toml`.
**Proibido:** editar `config/risco.toml` e `config/aprovacoes/*` (o hash aprovado precisa continuar valendo).

**`core/tipos.py`** (só adições opcionais; `Acao` sem membro novo; `PropostaIA` e `RevisaoIA` intactas):

```python
class RegraGestao(Base):
    tipo: Literal["breakeven","trailing","parcial","sem_progresso","apertar_apos","ao_vencer_tempo"]
    mfe_pontos: float | None = None          # excursao favoravel minima (pontos, desde a entrada) p/ ativar
    apos_s: int | None = None                 # segundos desde a abertura p/ ativar
    distancia_pontos: float | None = None     # trailing/apertar_apos: distancia stop-preco (ou stop-extremo)
    folga_ticks: int | None = None            # breakeven: entrada + custo ida e volta + folga
    quantidade: int | None = None             # parcial: unidades a realizar (1..q-1)
    acao_vencimento: Literal["encerrar","proteger_e_estender"] | None = None
    extensao_s: int | None = None

class AjustePosicao(Base):                    # so com acao=MANTER; niveis em PONTOS DE RESULTADO desde o preco medio
    stop_resultado_pontos: float | None = None   # -30 perde 30; 0 empate bruto; +10 trava 10
    alvo_resultado_pontos: float | None = None
    tempo_max_total_s: int | None = None         # total desde a abertura
    reduzir_quantidade: int | None = None        # 1..q-1
    substituir_gestao: tuple[RegraGestao, ...] | None = None

class PlanoTradeIA(Base):                     # esquema do papel "planejador"
    acao: Acao
    hipoteses: tuple[str, ...] = (); evidencias_a_favor: tuple[str, ...] = (); evidencias_contra: tuple[str, ...] = ()
    riscos: tuple[str, ...] = (); invalidacoes: tuple[str, ...] = (); justificativa_curta: str = ""
    incerteza: float = Field(ge=0, le=1, default=0.5)
    p_alvo: float | None = Field(default=None, ge=0, le=1)
    quantidade: int = 1
    stop_pontos: float | None = None          # distancia a partir da referencia de t0 (nivel absoluto fixo)
    alvo_pontos: float | None = None
    tempo_max_s: int | None = None
    gatilhos: tuple[Condicao, ...] = ()       # DSL existente; vazio = "agora" (dentro da faixa)
    faixa_contra_pontos: float | None = None
    faixa_favor_pontos: float | None = None
    expira_s: int = 120
    gestao: tuple[RegraGestao, ...] = ()
    reavaliar_em_s: int | None = None
    reavaliar_se: tuple[Literal["perto_do_stop","perto_do_alvo","metade_do_tempo","regra_disparou",
                                "lucro_acima_stop"], ...] = ()
    manter_plano_vigente: bool = False
    ajuste: AjustePosicao | None = None

class RevisaoPlanoIA(Base):
    veredito: Literal["APROVAR","REJEITAR"]
    coerente: bool; dados_ok: bool; regras_ok: bool           # SEM padrao
    afirmacoes_sem_suporte: tuple[str, ...] = ()
    p_alvo_revisor: float | None = Field(default=None, ge=0, le=1)
    justificativa_curta: str = ""

class RegraCompilada(Base):   # mesmos campos de RegraGestao, ja em pontos na grade do tick
    tipo: str; mfe_pontos: float | None = None; apos_s: int | None = None; distancia_pontos: float | None = None
    folga_pontos: float | None = None; quantidade: int | None = None
    acao_vencimento: str | None = None; extensao_s: int | None = None

class PlanoGestaoCompilado(Base):
    regras: tuple[RegraCompilada, ...] = (); passo_min_pontos: float; intervalo_ajuste_s: float
    reavaliar_em_s: int | None = None; reavaliar_se: tuple[str, ...] = (); versao_dsl: int = 1

class EnvelopeDecisao(Base):  # derivado, somente leitura; calculado SO por MotorRiscoPadrao.envelope
    unidades_max: int
    stop_max_pontos_por_q: tuple[float, ...]   # indice q-1; ja descontados perda op/dia/semana, folga e fator
    stop_min_pontos: float
    dist_min_ajuste_pontos: float
    tempo_max_s: int                           # total desde a abertura (posicao atual ou nova)
    entradas_possiveis: bool
    motivos_sem_entrada: tuple[str, ...] = ()  # codigos, sem valores numericos

class ConfigPlanejador(Base):  # [planejador]: parametros PRE-REGISTRADOS da variante; so restringem, nunca sao limite
    ativo: bool = False; expor_envelope: Literal["envelope","nenhum"] = "envelope"
    k_custo: float = 1.0                 # alvo restante > k*custo_ida_volta_estresse + 1 tick
    razao_liquida_min: float = 0.0       # 0 = desligado (sem geometria imposta)
    max_regras: int = 6; tolerancia_max_ticks: int = 8; tolerancia_max_fracao_stop: float = 0.5
    tempo_min_s: int = 30; margem_zeragem_s: int = 120; expira_max_s: int = 600; reavaliar_min_s: int = 10
    passo_min_ticks: int = 1; passo_min_ticks_espelhado: int = 2; intervalo_ajuste_s: float = 2.0
    reserva_gestao_frac: float = 0.3; exigir_p_alvo: bool = True

class PreRegistro(Base):  # cadeia de hash do experimento (WP9 grava/verifica)
    id_experimento: str; t_registro_ns: int; inicio_avaliacao: str; variantes: dict[str, str]
    comparacoes_primarias: tuple[str, ...]; margens_nao_inferioridade: dict[str, float]
    metrica_unidade: Literal["pregao"] = "pregao"; alfa: float = 0.05; n_pregoes_alvo: int
    regras_parada: tuple[str, ...] = (); hash_anterior: str = ""; hash_registro: str = ""
```

- **`Intencao`** ganha campos opcionais. Os padrões reproduzem o comportamento atual:
  ```python
  posicao_ticket: int | None = None
  quantidade_t0: int | None = None
  gestao: PlanoGestaoCompilado | None = None
  ajuste: AjustePosicao | None = None
  ancora_t0: float | None = None   # presente => niveis da entrada ancorados no preco de t0
  p_alvo: float | None = None
  ```
- **`ConfigRisco`** ganha `tempo_max_posicao_s: int = 300`. O padrão é o maior tempo fixo de hoje (LLM com 300 s); o arquivo aprovado não muda.
- `ManifestoExperimento` **não muda**, para não alterar hashes já registrados.

**`ia/esquemas.py`:** `ESQUEMAS_POR_PAPEL["planejador"]=PlanoTradeIA` e `["revisor_plano"]=RevisaoPlanoIA`.

**`decisao/bracos/base.py`:**
- `ContextoDecisao` ganha `envelope: EnvelopeDecisao | None = None`, `custo_ida_volta_pontos: float = 0.0`, `plano_vigente: bool = False` e `latencia_ia_s: tuple[float, float] | None = None` (p50, p95).
- `ResultadoDecisao` ganha `posicao_ticket_t0: int | None = None`, `quantidade_t0: int = 0`, `reavaliar_em_s: int | None = None`, `reavaliar_se: tuple[str, ...] = ()` e `p_alvo: float | None = None`. O comentário de `resultado` passa a listar também `ajuste` e `manter_sem_ajuste`.
- `_resultado` preenche `posicao_ticket_t0` e `quantidade_t0` a partir de `ctx.posicao`.
- `_intencao(..., quantidade: int = 1, **extras)`: sai o `quantidade=1` fixo da l.154. O padrão continua 1.

**`dados/decisao_ia.py`** (novo): mesmo padrão de `dados/features_ciclo.py`, com tabelas **fora** do esquema congelado M0 e `BancoSQLite.registrar_migracao("decisao_ia", DDL)`. Tabelas:
- `posicoes_episodio(id, braco, ticket, decisao_id, intencao_id, lado, q_inicial, ancora_t0, stop_inicial_pts, alvo_inicial_pts, tempo_max_s, t_aberta_ns, t_fechada_ns, liquido_total, n_parciais, n_ajustes_ia, n_ajustes_auto, mfe_pts, mae_pts, motivo_final, p_alvo, sombra_de, trades_json)`;
- `gestao_eventos(id, t_ns, braco, ticket, origem, acao, regra, sl_antes, sl_depois, tp_antes, tp_depois, prazo_ns, aplicado, motivo)`;
- `previsoes_ia(id, decisao_id, t0_ns, braco, lado, p_alvo, incerteza, stop_pts, alvo_pts, tempo_s, ancora, executada, y_alvo, fonte_y, resolvido_ns)`;
- `contrafactuais(id, decisao_id, braco_ref, politica, liquido, motivo, t_fim_ns, y_alvo, seed)`.

Com `garantir_tabelas(banco) -> bool`.

**`decisao/registro.py`:** métodos `episodio(**campos)`, `gestao_evento(**campos)` e `previsao_ia(**campos)`, que chamam `garantir_tabelas`. Num dublê sem migração, não gravam nada.

**`risco/usuarios.py`:**
- `SECOES_IA += ("planejador",)`;
- `modelo_inicial` preenche `tempo_max_posicao_s=300`;
- o rótulo vai para `ROTULOS`.

**`painel/risco_usuarios.py`:** o campo aparece no formulário (via `CAMPOS_RISCO`).

**`config/robowin.toml`:**
- `[ia.papeis.planejador]` (o padrão é o mesmo modelo do proponente);
- `[ia.papeis.revisor_plano]` (o padrão é o mesmo do revisor);
- `[planejador]` com os campos de `ConfigPlanejador` (`ativo = true`);
- `[planejador.gatilhos]` com `periodo_s = 240`, `eventos_posicao_permitidos = ["fechou"]` e `perto_frac = 0.3`;
- `[prazos]`: `planejador = { proponente_s = 60, revisor_s = 20, expira_min_max = 10 }`;
- exemplos comentados de `[[bracos]] tipo = "planejador"`.

**Testes:**
- `tests/unit/test_contratos_planejador.py` (novo):
  - `extra=forbid`;
  - `PlanoTradeIA` recusa `perda_max_operacao_brl` e `tempo_max_posicao_s`;
  - `verificar_schema` (sem campos de raciocínio) passa para os dois esquemas novos;
  - **o sha256 de `PropostaIA.model_json_schema()` e de `RevisaoIA` é igual a constantes capturadas antes da mudança**;
  - `ConfigRisco().tempo_max_posicao_s == 300`;
  - `carregar_politica("config/risco.toml", "config/aprovacoes/risco-v1.toml")` continua passando;
  - `Intencao` nova serializada sem os campos novos é igual à antiga.
- `tests/unit/test_dados_decisao_ia.py` (novo): migração idempotente e banco antigo sem as tabelas continua funcionando.
- `test_risco_usuarios.py` e `test_painel_risco.py`, ajustados ao campo novo:
  - `validar_ia_sem_limites({"planejador": {"tempo_max_posicao_s": 900}})` levanta `ErroConfig`;
  - um arquivo aprovado sem o campo continua aprovado e com o mesmo hash.

Se algum hash "dourado" de sessão mudar por causa dos campos opcionais novos, o WP0 atualiza o valor com justificativa no commit.

### WP1 – Motor de risco (veto final)

**Arquivos:** `risco/motor.py`, `risco/politica.py`, `risco/sessao.py`, `risco/invariantes.py`.

**`risco/politica.py`:**
- Constantes inteiras (não usar literal `5.0` nem `0.20`, que `tests/estaticos/test_sem_constantes_de_ativo.py` barra): `STOP_MIN_TICKS = 2` e `DIST_MIN_STOP_TICKS = 1`.
- `stop_maximo_efetivo_pontos(cfg, pv, quantidade: int | None = None)`: `None` mantém o comportamento atual (`contratos_max`).
- `stop_max_por_quantidade(cfg, pv_conta, umax, restante_dia, restante_semana) -> tuple[float, ...]`, onde `q ↦ min(cfg.stop_max_pontos, min(perda_max_op, restante_dia, restante_semana)/(q·pv_conta) − folga)`.
- `validar_coerencia` passa a exigir `1 <= tempo_max_posicao_s <= hhmm(zeragem) − hhmm(entradas_de)`.

**`risco/motor.py`:**
- `EstadoMercado.spread_pts: float = 0.0`.
- `entradas_possiveis(braco, t_ns) -> tuple[str, ...]`: motivos que não dependem da intenção. Cobre:
  - kill, fora do pregão ou da janela, evento, feed, dado crítico, vol extrema, leilão, limite de oscilação, rolagem;
  - perda diária ou semanal atingida, drawdown, operações do dia, perdas seguidas, pausa, ordem pendente;
  - `orcamento_de_perda_insuficiente` (o stop mínimo com q=1 não cabe).

  `_motivos_entrada` passa a usar essa função (fonte única).
- `envelope(braco, t_ns) -> EnvelopeDecisao`: puro e só leitura. `tempo_max_s = min(cfg.tempo_max_posicao_s, s_ate_zeragem − margem)`; com posição aberta, é o total desde `aberta_ns`. A margem é passada pelo chamador, com padrão 0.
- `_motivos`:
  ```python
  if a == NAO_OPERAR: return []
  if a == ENCERRAR: return ["posicao_diferente_da_vista"] if it.posicao_ticket not in (None, pos.ticket) else []
  if a == MANTER: return self._motivos_ajuste(it, pos, t)
  if a in (COMPRAR, VENDER): return self._motivos_entrada(it, pos, t)
  return ["acao_desconhecida"]
  ```
- `_motivos_ajuste(it, pos, t)` substitui `_motivos_manter`.
  - **Caminho legado** (`ajuste is None` e `stop_pontos`): mesma semântica de hoje, mais `stop_colado_no_preco`.
  - **Caminho novo:**
    - posição diferente da vista (ticket ou `quantidade_t0`): `posicao_diferente_da_vista`;
    - stop: nível = `preco_medio ± stop_resultado`, arredondado em direção ao preço. Precisa passar em `stop_so_aperta` (senão `stop_so_pode_apertar`), ficar do lado certo e a pelo menos `DIST_MIN_STOP_TICKS·tick + spread_pts` de `it.preco_referencia`, que é o preço executável de saída (senão `stop_colado_no_preco`);
    - alvo: do lado do lucro e a pelo menos essa distância (senão `alvo_invalido`/`alvo_colado_ao_preco`);
    - tempo: `decorrido < tempo_total <= cfg.tempo_max_posicao_s` e `aberta + tempo_total <= zeragem` (senão `tempo_max_acima_do_limite`/`tempo_ja_vencido`);
    - redução: `1 <= r < pos.quantidade` (senão `reducao_invalida`). **Nunca é vetada por janela, kill ou limites;**
    - com kill ativo, só passam aperto de stop e redução (senão `kill_ativo_so_aperto`);
    - nada pode aumentar a posição.
- `_motivos_entrada` ganha:
  - `tempo_max_acima_do_limite`, quando `tempo_max_s` não é `None` e é ≤0, maior que o teto ou passa da zeragem;
  - `stop_abaixo_do_minimo` e `alvo_abaixo_de_um_tick`;
  - `gestao_invalida:<tipo>`, uma rechecagem estrutural independente do validador: tipo no conjunto fechado; parcial só com q≥2 e quantidade < q; distâncias ≥ `dist_min`.
- `registrar_trade` e `registrar_parcial` (novo) repassam para a sessão.

**`risco/sessao.py`:**
- `EstadoBraco` ganha `liquido_episodio` e `q_max_episodio`.
- `registrar_parcial(braco, liquido, quantidade, t)` soma o P&L realizado do dia e da semana, sem mexer em sequência e pausa.
- `registrar_trade(braco, liquido, quantidade, t)` fecha o episódio: `total = liquido_episodio + liquido`; `perdas_seguidas`, `ultima_perda_ns` e `ultima_foi_perda` usam o total; `ultimo_tamanho = max(q_max_episodio, quantidade)`. Sem parciais, o comportamento é idêntico ao de hoje.

**`risco/invariantes.py`:** `inv_reducao_so_reduz(qs)`, `inv_stop_monotono(lado, seq)`, `inv_distancia_minima(lado, stop, preco_saida, dist)` e `inv_tempo_no_teto(aberta, prazo, teto)`.

**Testes:** `test_risco_motor.py`, `test_risco_invariantes.py` e os novos `test_risco_motor_planejador.py` e `test_risco_sessao_episodio.py`. Precisam cobrir:
- `acao_desconhecida` (com `model_construct` de ação forjada);
- ENCERRAR com ticket velho é vetado, e ENCERRAR sem ticket nunca é vetado;
- todos os motivos de ajuste, com kill restringindo a aperto e redução;
- redução nunca vetada por limites;
- teto de tempo;
- stop mínimo;
- `entradas_possiveis` igual aos motivos não intencionais de `_motivos_entrada` (propriedade);
- `envelope` coerente com o veto: um stop igual a `stop_max_pontos_por_q[q-1]` é aprovado e um tick a mais é vetado;
- duas parciais com prejuízo contam uma perda seguida;
- martingale usa o q máximo do episódio;
- o motor ignora qualquer valor de limite que apareça num dossiê.

### WP2 – Execução (roteador e simulador)

**Arquivos:** `execucao/roteador.py`, `execucao/simulador.py`.

**Simulador:**
- `modificar_protecao(braco, ticket, sl, tp) -> str | None`: `None` significa aplicado; senão devolve `sem_posicao`, `ticket_divergente`, `stop_pendente`, `stop_afrouxaria`, `stop_lado_errado`, `alvo_lado_errado` ou `fora_da_grade`. O TP novo cancela e recria a ordem de alvo, e a fila recomeça. `modificar_stop` passa a usar essa função.
- Corrigir C10 (alvo com q>1) e, depois de uma parcial, alvo com `restante = min(restante, p.qtd)`.
- `EventoSim.restante: int = 0` (posição restante depois do trade).
- `PosicaoSim.intencao_id`.
- `enviar` recusa entrada com SL ou TP do lado errado da última cotação (`RET_INVALID_STOPS`).

**Roteador:**
- `exigir_aprovacao=True` como padrão.
- `MANTER` com `it.ajuste`: níveis absolutos a partir de `posicao.preco_medio`, arredondados em direção ao preço, aplicados com `sim.modificar_protecao`. A recusa vira `ResultadoExecucao(dec, motivo="protecao_nao_aplicada:<m>")`. `reduzir_quantidade` vira `_montar_saida(..., quantidade=r)`, uma saída sem aprovação por hash.
- `MANTER` legado: o retorno de `modificar_stop` passa a ser conferido.
- `ENCERRAR` usa `it.posicao_ticket`, quando existe, no `position_ticket`.
- `ResultadoExecucao.niveis_aplicados: tuple[float | None, float | None] | None`, para o espelho.
- `processar_eventos`: trade com `e.restante > 0` chama `motor.registrar_parcial`; senão, `registrar_trade`.

**Testes:** `test_exec_roteador.py`, `test_exec_simulador.py` e o novo `test_exec_protecao.py`. Precisam cobrir:
- aperto aplicado; afrouxamento recusado; TP recriado;
- alvo com q=3 fecha 3;
- parcial redimensiona o alvo, e o stop age sobre o restante;
- SL do lado errado é recusado;
- recusa do executor aparece em `motivo`;
- padrão `exigir_aprovacao=True` com a suíte inteira verde.

### WP3 – Espelho DEMO, gateway e guardião

**Arquivos:** `decisao/espelho.py`, `mt5/gateway.py`, `mt5/guardiao.py`.

- **`EspelhoDemo.espelhar_protecao(sl, tp, t_ns) -> ResultadoEspelho`:** envia `{"tipo":"modificar_sltp","ticket","sl","tp","dono":"nucleo"}`, com token, pelo mesmo canal. Se a corretora recusar (stops_level ou freeze_level), registra incidente e a demo fica com o SL anterior. O guardião continua.
- **`EspelhoDemo.saida_parcial(quantidade, motivo, t_ns)`:** usa o tipo `ordem` que já existe (`ComandoOrdem(tipo="saida", quantidade=q, position_ticket)`). No MT4 (hedging), roda `_descobrir_ticket()` de novo depois da parcial.
- **`espelhar_entrada`:** `min(pos_sim.quantidade, motor.unidades_max)`.
- **`validar_braco_espelhavel(braco) -> None`:** levanta `ErroConfig` se `familia ∈ {"sombra", "controle"}` ou se o braço for passivo. Isso garante que sombras e controles nunca dobram a exposição na demo.
- **`Gateway.tratar`:** novo tipo `modificar_sltp`, que passa por `guardiao.avaliar_sltp(ticket, sl, tp)` e depois por `executor.modificar_sltp`. Nenhum `order_send` novo.
- **`Guardiao.avaliar_sltp(...) -> tuple[bool, str]`:** nunca remove o SL, nunca afrouxa, confere o ticket. Aperto é permitido mesmo em emergência.

**Testes:** `test_espelho_demo.py`, `test_mt5_gateway.py`, `test_mt5_guardiao.py` e `test_mt4_hedging.py`:
- `modificar_sltp` sem token é recusado;
- aperto chega ao dublê e afrouxamento é recusado pelo guardião e pela execução DEMO;
- parcial no dublê MT4 redescobre o ticket;
- `validar_braco_espelhavel` recusa sombra e controle.

`test_mt5_ast.py` não muda.

### WP4 – IA: validador, dossiê v2, prompts, sanitização

**Arquivos:** `ia/validadores.py`, `ia/dossie.py`, `ia/prompts.py`, `ia/reflexao.py`, `ia/regressao.py`, `config/prompts/planejador_v1.md` (novo), `config/prompts/revisor_plano_v1.md` (novo).

**Contratos em `ia/validadores.py`** (sem importar `robowin.risco`):
- `texto_seguro(texto, niveis_absolutos=()) -> list[str]` aplica `_RE_PERIGOSO` (movido de `reflexao.py:35`, que passa a importar), `verificar_anonimizacao` e `_RE_INDICADOR`.
- `validar_evidencias(textos, dossie, *, conferir_valores=False, tol_rel=0.01, tol_abs=0.01)`.
- `validar_plano_trade(p, dossie, *, env, cfg, tick_pontos, custo_ida_volta_pontos, slip_stop_medio_pontos) -> ResultadoValidacao`. Recusa todo valor não finito.
  - **Entradas:**
    - coerência com a posição (que já existe);
    - `env.entradas_possiveis`;
    - `stop_pontos`, `alvo_pontos`, `tempo_max_s`, faixas e (com `exigir_p_alvo`) `p_alvo` presentes;
    - `invalidacoes` não vazio; pelo menos uma evidência a favor e uma contra, com valores conferidos;
    - `1 <= q <= env.unidades_max`;
    - `env.stop_min <= stop <= env.stop_max_pontos_por_q[q-1] − faixa_favor`. Assim o stop ancorado, que cresce até `faixa_favor`, continua viável;
    - `alvo − faixa_favor > k_custo·custo_ida_volta + tick`;
    - `faixa_contra <= stop − env.stop_min`;
    - `faixa_favor <= min(tolerancia_max_fracao_stop·stop, tolerancia_max_ticks·tick)`;
    - `tempo_min_s <= tempo_max_s <= env.tempo_max_s`;
    - `expira_s <= expira_max_s`;
    - gatilhos por `validar_condicao`, no máximo `MAX_GATILHOS`, `ate_ns` proibido (usar `janela_tempo` com `valor`);
    - regras: no máximo `max_regras`; parcial só com q≥2 e `1 <= quantidade < q`; distâncias ≥ `env.dist_min_ajuste_pontos`;
    - `reavaliar_em_s ∈ [reavaliar_min_s, tempo_max_s]`;
    - com `razao_liquida_min > 0`: `(alvo − custo)/(stop + custo + slip) >= razao`.
  - **`MANTER` com ajuste:**
    - posição aberta;
    - `stop_resultado >= posicao.stop_resultado_pontos` atual;
    - `alvo_resultado > posicao.resultado_pontos + dist_min`;
    - `tempo_aberta_s < tempo_total <= env.tempo_max_s`;
    - `1 <= reduzir < q`.
  - **Todo texto livre** passa por `texto_seguro`.
- `revisao_plano_aprova(r: RevisaoPlanoIA | None) -> bool` exige `APROVAR` e os três booleanos verdadeiros.

**`ia/dossie.py`:**
- `montar_dossie(..., versao: int = 1, extras: Mapping[str, Any] | None = None)`. O v1 fica idêntico.
- O v2 acrescenta as seções `instrumento`, `custos`, `envelope`, `sessao` ampliada, `mercado`, `posicao` v2, `plano_vigente`, `retorno` e `latencia_ia` (conteúdo na seção 3/WP5), e `movimento.vol_pts_60s`/`vol_pts_300s` (desvio-padrão das diferenças, em pontos).
- O v2 recusa feature cuja unidade em `features/registro.py` seja relativa ou log (C30).
- `_perto` restrito por caminho e com as chaves isentas (C29).
- O padrão de `features_permitidas` passa a ser `nomes_permitidos()` (C28).

**`ia/prompts.py`:**
- `NOMES["planejador"]="planejador_v1"` e `NOMES["revisor_plano"]="revisor_plano_v1"`;
- `mensagem_planejador(dossie) -> str`: "preços e distâncias em pontos relativos; demais campos com a unidade no nome";
- `mensagem_revisor_plano(dossie, compilado: dict) -> str`: o revisor recebe o plano **compilado**, com níveis em pontos, equilíbrio e custo.

**Prompts novos**, genéricos (sem WIN, B3, Ibovespa, "R$" ou valores de tick). Explicam:
- a semântica de cada campo, com exemplos −30/0/+10;
- ancoragem em t0 e faixa;
- latência (`latencia_ia`): use faixa ou gatilhos;
- custos e a fórmula de equilíbrio;
- o envelope é dos limites do usuário, não pode ser alterado, e proposta fora dele é reprovada;
- afrouxar stop é recusado;
- `NAO_OPERAR` é legítimo;
- `p_alvo` será pontuado;
- as proibições de hoje.

**`ia/regressao.py`:** bateria fixa para o papel `planejador`.

**Testes:** `test_ia_validadores.py`, `test_ia_dossie.py`, `test_ia_reflexao.py`, `test_ia_prompts_regressao_cli.py` e os novos `test_validar_plano_trade.py` (cada regra acima, NaN/inf, valor citado divergente, `null`, injeção, indicador) e `test_dossie_v2.py`:
- isenções do `_perto` (ativo perto de 200 com perda máxima de 200; tempo de 5000 s);
- sem `rv_1m`;
- nenhuma chave proibida.

Estáticos novos:
- `tests/estaticos/test_isolamento_ia.py` (AST): nenhum módulo em `src/robowin/ia/` ou `src/robowin/decisao/bracos/` importa `robowin.risco` (ou `..risco`), nem referencia ou instancia `ConfigRisco`;
- `tests/estaticos/test_prompts_genericos.py`: os prompts do planejador não têm `RE_ATIVO`, "R$", "B3" nem "Ibovespa".

### WP5 – Núcleo puro de decisão (compilador, gestão, dossiê da conta, gatilhos, prazos)

**Arquivos:** `decisao/compilador_plano.py` (novo), `decisao/gestao.py` (novo), `decisao/dossie_conta.py` (novo), `decisao/validade.py`, `decisao/gatilhos.py`, `decisao/plano_condicional.py`.

**`compilador_plano.py`** (não importa `robowin.risco`):

```python
@dataclass(frozen=True)
class PlanoCompilado:
    intencao_base: Intencao
    plano_abs: PlanoCondicional
    gestao: PlanoGestaoCompilado
    resumo: dict[str, Any]

def compilar_plano(p: PlanoTradeIA, *, ref_t0: float, t0_ns: int, tick_pontos: float,
                   env: EnvelopeDecisao, cfg: ConfigPlanejador, braco: str, ciclo_id: str,
                   espelhado: bool = False, custo_ida_volta_pontos: float = 0.0) -> PlanoCompilado

def compilar_gestao(regras: Sequence[RegraGestao], *, tick_pontos: float, cfg: ConfigPlanejador,
                    espelhado: bool, reavaliar_em_s: int | None, reavaliar_se: Sequence[str]) -> PlanoGestaoCompilado
```

Regras do compilador:
- stop e alvo arredondados para baixo na grade (stop mais curto, alvo mais perto);
- a faixa vira invalidações relativas a `ref_t0`:
  - compra: `preco_rompe_acima(+favor)` e `preco_rompe_abaixo(−contra)`;
  - venda: o espelho disso;
- sem gatilhos, usa `janela_tempo(valor=expira_s)`;
- `expira_ns = t0 + expira_s`;
- a `Intencao` base leva `modo="condicional"`, `quantidade`, `tempo_max_s`, `gestao`, `ancora_t0=ref_t0`, `p_alvo` e `validade_ns=expira_ns`;
- `passo_min = (espelhado ? passo_min_ticks_espelhado : passo_min_ticks)·tick`.

**`validade.py`:**
- `PRAZOS_PLANEJADOR = Prazos(claude_s=60, gpt_s=20, validade_s=600, desvio_max_ticks=inf, margem_s=0)`;
- propriedades `Prazos.proponente_s`/`revisor_s`;
- `prazos_de_config` lê `[prazos].planejador` e os apelidos `proponente_s`/`revisor_s`;
- nova função:
  ```python
  def ajustar_entrada_ancorada(it: Intencao, *, preco_exec: float, compra: bool, stop_min: float, alvo_min: float,
                               favor_max: float) -> tuple[Intencao | None, tuple[str, ...]]
  ```
  `d = preco_exec − ancora` (compra) ou `ancora − preco_exec` (venda); `stop' = stop + d`; `alvo' = alvo − d`. Violações: `stop_restante_insuficiente`, `alvo_restante_insuficiente` e `perseguicao_acima_do_aceito`. Devolve a `Intencao` com `preco_referencia=preco_exec`. O motor veta se `stop'` estourar a perda.

**`gestao.py`:**
- `EstadoGestao` (dataclass): `lado`, `entrada`, `qtd_inicial`, `qtd`, `aberta_ns`, `prazo_ns`, `sl`, `tp`, `extremo_favoravel`, `mfe_pontos`, `mae_pontos`, `regras_disparadas: set[int]`, `ultimo_ajuste_ns`, `eventos_emitidos: set[str]`, `ajustes_ia`, `ajustes_auto`, `parciais`, `liquido_parcial`, `origem_ultimo_stop`.
- `iniciar_estado(pos: Posicao, it: Intencao, tempo_max_s: int) -> EstadoGestao`.
- `atualizar_excursao(est, *, bid, ask, last, por_cotacao)`: usa a mesma referência de disparo do stop no simulador (último negócio na B3, cotação de saída em forex).
- Ações e avaliação:
  ```python
  @dataclass(frozen=True)
  class ApertarStop: nivel: float; regra: str
  # idem: Reduzir(q, regra), Encerrar(motivo), EstenderPrazo(prazo_ns, regra), EventoReavaliacao(nome)

  def avaliar_gestao(plano: PlanoGestaoCompilado, est: EstadoGestao, *, bid: float, ask: float, last: float,
                     t_ns: int, tick: float, dist_min: float, custo_ida_volta_pontos: float,
                     teto_prazo_ns: int, por_cotacao: bool, perto_frac: float) -> list[AcaoGestao]

  def simular_caminho(plano, est0, ticks) -> list[tuple[int, AcaoGestao]]  # para contrafactuais (WP9)
  ```
- Semântica das regras:

  | Regra | Efeito |
  |---|---|
  | `breakeven` | com MFE ≥ X, stop em entrada ± (custo + folga) |
  | `trailing` | com MFE ≥ ativação, stop = extremo − distância |
  | `parcial` | com MFE ≥ X, reduz `quantidade` (inerte com q=1) |
  | `sem_progresso` | se depois de T s o MFE < Y, encerra |
  | `apertar_apos` | em t ≥ T, stop = ref − d |
  | `ao_vencer_tempo` | `encerrar`; ou `proteger_e_estender` só se o stop já trava lucro líquido, até `teto_prazo_ns` |

- Garantias:
  - stops monotônicos, quantizados por `passo_min_pontos`, com pelo menos `intervalo_ajuste_s` entre ajustes e sempre a pelo menos `dist_min` do preço executável;
  - eventos `perto_do_stop`, `perto_do_alvo`, `metade_do_tempo`, `regra_disparou` e `lucro_acima_stop` emitidos uma vez por posição e só quando pedidos em `reavaliar_se`.

**`dossie_conta.py`:**

```python
@dataclass(frozen=True)
class CustosPublicos:
    ida_volta_pontos_base: float; ida_volta_pontos_estresse: float; meio_spread_pontos: float
    slippage_stop_medio_pontos: float; fonte_slippage: str

def custos_publicos(perfil, *, spread_pts: float, slippages_stop_anteriores: Sequence[float],
                    min_amostras: int = 30) -> CustosPublicos

def secoes_planejador(*, pos: Posicao, est: EstadoGestao | None, env: EnvelopeDecisao, expor: str,
                      perfil, custos: CustosPublicos, sessao: Mapping, mercado: Mapping,
                      plano_vigente: Mapping | None, retorno: Mapping, latencia: Mapping,
                      ultimo_preco: float) -> dict[str, Any]

def limites_legado(env: EnvelopeDecisao, est_risco: Any, cfg_risco: Any) -> dict[str, float]   # v1 [v1.1]
```

- `custos_publicos` usa a média causal; com menos de `min_amostras`, a reserva é `avaliacao.relatorio.custos_do_perfil` convertido para pontos. Nunca usa `ParamsSlippage`.
- Conteúdo de `secoes_planejador`:
  - `instrumento`: `tick_pontos`, `valor_ponto_conta`, `stop_dispara_por`;
  - `custos`, incluindo `formula_equilibrio`;
  - `envelope` (ou a versão reduzida, se `expor="nenhum"`);
  - `sessao`: `min_desde_abertura` com `de`, `min_para_fim_entradas`, `min_para_zeragem`, `min_para_evento`;
  - `mercado`: `feed_ok`, `leilao`, `vol_extrema`;
  - `posicao`: lado, q, q inicial, `resultado_pontos` pela cotação de saída, MFE/MAE, tempo aberta, máximo e restante, `stop_resultado_pontos` e `alvo_resultado_pontos` **atuais**, distâncias, regras com estado, ajustes e parciais;
  - `plano_vigente`;
  - `retorno`: só códigos, sem números;
  - `latencia_ia`.

**`gatilhos.py`:**
- `ConfigGatilhos` ganha `eventos_posicao_permitidos: tuple[str, ...] = ("abriu", "fechou")`, `reserva_gestao_frac: float = 0.0` e `perto_frac: float = 0.3`.
- `EstadoGatilho` ganha `eventos_gestao`, `reavaliacao_ns`, `com_posicao` e `plano_terminou`.
- Disparos novos: `gestao:<evento>`, `reavaliacao_programada` e `plano_terminou`.
- Sem posição, só se gasta `(1 − reserva)·chamadas_restantes`. `intervalo_min_s` e o teto continuam valendo, e `reavaliar_em_s` nunca passa deles.

**`plano_condicional.py`:**
- `armar(..., estado_atual: EstadoPlano | None = None)`: se a invalidação já estiver ativa ao armar, devolve o evento `invalidado_ao_armar` sem armar (C8 do mapa);
- `TipoEventoPlano` ganha `invalidado_ao_armar`.

**Testes:** `test_decisao_plano_validade_gatilhos.py` e os novos `test_compilador_plano.py`, `test_gestao_regras.py` (hypothesis sobre caminhos aleatórios), `test_dossie_conta.py` e `test_gatilhos_gestao.py`. Precisam cobrir:
- arredondamento; faixa na compra e na venda; "agora" vira `janela_tempo`;
- ancoragem nos três motivos;
- cada regra de gestão;
- stop monotônico e com `dist_min`; quantidade nunca cresce; prazo ≤ teto; determinismo; quantização e intervalo;
- custos causais sem `ParamsSlippage`;
- reserva de gestão, eventos de uma vez por posição, "abriu" desligado.

### WP6 – Braços (planejador, sombras, controle do plano, fábrica, FALSO)

**Arquivos:** `decisao/bracos/planejador.py` (novo), `decisao/bracos/sombra.py` (novo), `decisao/bracos/controles.py`, `decisao/bracos/fabrica.py`, `decisao/bracos/llm.py`, `decisao/bracos/debate.py`, `decisao/simulacao.py`.

**`planejador.py`:**
- `ServicoPlanejamento(ServicoProposta)`: papel `planejador`, esquema `PlanoTradeIA`, `_buscar` com `mensagem_planejador`, validação por `validar_plano_trade` com `ctx.envelope`. O cache `(ciclo, modo, hash_dossie)` é reaproveitado, então P e PC fazem uma chamada pareada.
- `ServicoRevisaoPlano(ServicoLLM)`: papel `revisor_plano`, `RevisaoPlanoIA`, `revisar(ctx, compilado.resumo, prazo)`.
- `BracoPlanejador(BracoBase)`:
  - `familia="planejador"`, `ia=True`, `modo="condicional"`;
  - `Geometria(0, 0, 0)` como sentinela (o manifesto marca `sem_geometria_fixa`);
  - `grupo_ciclo = f"planejador:{servico.chave}"`.
- Mapeamento em `adecidir`:

  | Resposta | Resultado |
  |---|---|
  | `NAO_OPERAR` | `nao_operar` |
  | `MANTER` sem ajuste | `manter_sem_ajuste` |
  | `MANTER` com ajuste | `ajuste`, com `Intencao(MANTER, ajuste, posicao_ticket, quantidade_t0, gestao=compilar_gestao(...) se substituir)` |
  | `ENCERRAR` | `saida`, com `posicao_ticket` |
  | entrada com posição | `bloqueada:posicao_aberta_do_braco` |
  | entrada com `manter_plano_vigente` e plano armado | `nao_operar:plano_vigente_mantido` |
  | demais entradas | `compilar_plano`, revisor opcional (PC) e depois `plano` |

  O resultado carrega `reavaliar_em_s`, `reavaliar_se` e `p_alvo`.

**`sombra.py`:**
- `BracoSombra(BracoBase)`: `familia="sombra"`, `ia=False`, `passivo=True`, `referencia: str`, `politica: Literal["sem_gestao", "trailing_fixo"]`.
- `derivar(it_ref, novo_id) -> Intencao` copia ação, quantidade, stop e alvo **já ancorados**, tempo, `preco_referencia` e validade:
  - `sem_gestao`: `gestao=None`;
  - `trailing_fixo`: regra pré-registrada, com breakeven quando MFE ≥ 1×stop e trailing ativado com 1×stop e distância de 1×stop.
- `adecidir` devolve sempre `nao_operar` (nunca decide sozinha).

**`controles.py`:** `BracoAleatorioPlano(BracoAleatorio)`, com `observar_plano(c: PlanoCompilado)`. Copia o plano compilado inteiro (gatilhos, faixa, gestão, tempo e quantidade), sorteia o lado com seed (valores invertidos, `rompe_acima` ↔ `rompe_abaixo`) e usa a mesma frequência do braço de referência. Nunca copia ajustes da IA.

**`fabrica.py`:**
- `TIPOS_BRACO += ("planejador",)`;
- `braco_planejador(e)`;
- `planejadores_padrao(cfg) -> list[BracoBase]`, que cria `planejador` (P), `planejador_revisor` (PC), `sombra_sem_gestao`, `sombra_trailing_fixo` (referência P) e `controle_aleatorio_plano` quando `[planejador].ativo`;
- `[[bracos]]` aceita `revisor`, `sombras` e `controle_plano`.

**`llm.py` e `debate.py`:** `texto_seguro` nos textos do proponente antes do revisor e nas saídas de etapa do debate **[v1.1]**.

**`simulacao.py`:**
- `plano_falso(dossie)` e `revisao_plano_falsa(...)`, rotulados `[FALSO]`. Exercitam:
  - entrada com faixa, breakeven, `sem_progresso` e `reavaliar_se`;
  - `MANTER` com `stop_resultado_pontos=0` quando `resultado ≥ stop`;
  - `ENCERRAR` quando `tempo_restante_s < 30` e o resultado é negativo;
  - parcial com q≥2 num perfil forex.
- `ProvedorFalsoSessao._executar` despacha `PlanoTradeIA`/`RevisaoPlanoIA`. `RevisaoPlanoIA` usa a latência do revisor.
- `construir_orquestrador` inclui `planejadores_padrao` e chama `espelho.validar_braco_espelhavel`.
- O manifesto ganha os hashes `esquema:planejador`, `esquema:revisor_plano` (`model_json_schema`), `dsl_gestao:v1`, `compilador:v1`, `[planejador]`, `[planejador.gatilhos]`, `prazos:planejador` e `correcoes:v1.1`.
- Sem literais `5.0` e `0.20` novos.

**Testes:** `test_decisao_bracos.py`, `test_decisao_debate.py` e os novos `test_bracos_planejador.py` e `test_sombra_controle_plano.py`:
- cada mapeamento;
- cache pareado entre P e PC;
- revisor só em entradas;
- sombra copia a entrada no mesmo t;
- aleatório reprodutível pela seed;
- composição padrão com `ativo`.

### WP7 – Orquestrador (integração)

**Arquivos:** `decisao/orquestrador.py`, `decisao/saidas.py`.

**`MetaPosicao`** ganha `gestao`, `estado: EstadoGestao | None`, `ticket`, `qtd_inicial`, `episodio_id` e `p_alvo`.

**Abertura:** `_abrir_posicao` calcula o tempo como `min(v for v in (it.tempo_max_s, g.tempo_max_s if g.tempo_max_s > 0 else None) if v)`, limitado por `cfg.risco.tempo_max_posicao_s` e **nunca 0**. Stop e alvo vêm de `pos.stop`/`pos.alvo`. Também chama `iniciar_estado` e grava `registro.episodio` (abertura).

**Tick:**
- `_marcar_a_mercado` chama `atualizar_excursao`.
- `_gestao(t, tick)` (novo) roda **antes** de `_saidas` em `on_tick` e `on_tempo`, e pula contas com `precisa_zerar` ou com dono de saída.
  - `ApertarStop` e `EstenderPrazo` viram `Intencao(MANTER, ajuste, posicao_ticket, quantidade_t0, preco_referencia=bid se comprado, ask se vendido)`, que passa por `roteador.executar_intencao`. Se aplicado: `gestao_evento(origem="auto")` e `_espelhar("espelhar_protecao", ...)`.
  - `Reduzir` segue o mesmo caminho, com `reduzir_quantidade`.
  - `Encerrar` chama `_sair(conta, "gestao", detalhe=regra)`.
  - `EventoReavaliacao` vai para `conta.eventos_gestao`.
  - Uma exceção vira `kill.registrar_erro` e incidente. A posição fica protegida pelo SL e pelo prazo.
- `_saidas`: limite `min(meta.tempo_max_s, cfg.risco.tempo_max_posicao_s)`.

**Eventos do simulador:** em `_registrar_evento_sim`, trade com `e.restante > 0` vai para `_registrar_parcial`: grava o trade, mantém a meta, libera a reivindicação de parcial e gera o evento `parcial`. Senão, vai para `_fechar_posicao`, que grava o episódio com o líquido total e o motivo detalhado:
- `stop_inicial` ou `stop_movido_ia`/`stop_movido_auto`;
- `alvo`, `tempo`, `encerrar_ia`, `gestao:<regra>`, `zeragem`, `kill`.

**Entrega (`_processar_resultado`):**
- Para `manter`, `ajuste` e `saida`: se `AssinaturaPosicao` atual diferir de `(posicao_ticket_t0, quantidade_t0)`, a decisão vira `ignorada:posicao_mudou_desde_t0`. A referência é o preço executável de saída. Só vira `executada` se aplicado; senão, `nao_aplicada`.
- `manter_sem_ajuste` só é gravado.
- `_sair(..., ticket_esperado=)`.

**Planos:**
- `_armar_plano` passa `estado_atual`.
- `_disparar_plano`: para intenções com `ancora_t0`, aplica `ajustar_entrada_ancorada` antes de `_executar_entrada`. Para sombras, usa a intenção já ajustada.

**Envio de entrada (`_executar_entrada`):**
- alimenta `motor.mercado.spread_pts`;
- depois de enviar uma entrada de P: `_replicar_sombras(it, t)` (cada sombra executa por roteador e motor próprios, com o mesmo `ConfigRisco`; veto da sombra fica registrado como `sombra_vetada`) e `controle_aleatorio_plano.observar_plano(compilado)`.

**Ciclos:**
- `_grupo_ativo`: braço sem posição em grupo de IA ou do quant só ativa o grupo se `motor.entradas_possiveis()` estiver vazio. Os controles mantêm a regra atual. Sombras passivas ficam fora de grupos e gatilhos.
- `_ciclos` monta `eventos_gestao`, `reavaliacao_ns`, `com_posicao` e `plano_terminou`.
- `_ciclo` usa o dossiê do líder só para braços sem posição; cada braço posicionado tem dossiê próprio (C6).
- `_dossie`:
  - v1: `limites_legado`, stop e alvo atuais e `de` **[v1.1]**;
  - v2 (planejador): `secoes_planejador` + `montar_dossie(versao=2)`, `FEATURES_DOSSIE_V2` e `niveis_extras_absolutos` com os preços auxiliares;
  - `ContextoDecisao` recebe `envelope`, `custo_ida_volta_pontos`, `plano_vigente` e `latencia_ia_s` (p50/p95 dos próprios carimbos `proposta_ia`).

**Registro e resumo:**
- `previsao_ia` para toda entrada proposta pelo planejador, executada ou não;
- `resumo_ultima_entrada` passa por `texto_seguro`;
- deduplicação de vetos e bloqueios repetidos (`stats` com `tentativas` e `episodios`);
- `resumo()` ganha `episodios_veto`, `parciais`, `gestoes_ia`, `gestoes_auto`, `gestoes_vetadas` e `pares_incompletos_sombra`;
- `__init__` chama `validar_braco_espelhavel` para o braço espelhado.

**`saidas.py`:** `MOTIVOS_SAIDA += ("gestao", "parcial")`. A redução só acontece sem dono vigente.

**Testes:** `test_decisao_orquestrador.py`, `test_decisao_e2e.py`, o novo `test_orquestrador_planejador.py` (ponta a ponta com FALSO) e o novo `tests/propriedades/test_ia_adversaria.py`. Precisam cobrir:
- o tempo escolhido pela IA fecha por `_saidas`, limitado pelo teto;
- a regra aperta o stop, o SL do simulador muda e o espelho falso recebe `modificar_sltp`;
- parcial num perfil forex (`volume_max_lotes=0.05`) é contabilizada como um episódio;
- `MANTER` ou `ENCERRAR` de ticket velho vira `ignorada`;
- C posicionado e B zerado fazem chamadas separadas;
- zero chamadas de IA com `entradas_possiveis` não vazio;
- veto repetido conta um episódio;
- plano invalidado ao armar;
- sombra replica no mesmo tick e sofre o próprio veto;
- o dossiê mostra o stop atual;
- sem tempo 0 com a geometria sentinela.

O teste de propriedade gera `PlanoTradeIA` aleatórias e adversárias (NaN, inf, negativos, gigantes, sinais trocados, gestão absurda) e passa cada uma por validador, compilador, ancoragem, motor e roteador. Toda intenção aprovada precisa satisfazer:
- `inv_exposicao` e `inv_perda_operacao`;
- `inv_sem_martingale` e `stop_so_aperta`;
- `inv_distancia_minima`;
- tempo ≤ teto;
- q ≤ `unidades_max`.

### WP8 – Relatórios, histórico, backtest, CI e docs

**Arquivos:** `avaliacao/relatorio.py`, `avaliacao/metricas.py`, `avaliacao/comparacao.py`, `historico/relatorio.py`, `historico/runner.py`, `historico/cli_cmds.py`, `backtest/motor.py`, `.github/workflows/historico-b3.yml`, `docs/15-resultado-historico-real.md`, `docs/16-decisao-completa-ia.md` (novo).

**Relatórios:**
- `texto_aviso_acerto_por_braco(perfil, geometrias: Mapping[str, tuple[float, float]], custos)`. O antigo continua como reserva, com "teto, não geometria usada" (C1).
- `decidir_selo(..., preregistro: PreRegistro | None = None, datas_pregoes=(), provedor_falso=False, hashes_variantes=None)` recusa:
  - pregão com data ≤ `t_registro`;
  - hash de variante divergente;
  - provedor FALSO.

  Usa `preregistro.verificar` (WP9). `tentativas` = total de variantes registradas na cadeia.
- Seções novas:
  - "injetado (FALSO)": modelo servido terminando em `-FALSO`, com `resposta_tardia`, `erro_http` e `divergencia_revisor`;
  - tentativas contra episódios;
  - métricas de gestão por posição;
  - diferenças pareadas com as sombras;
  - dias encerrados por perdas seguidas.

**Comparação e métricas:**
- `comparacao.diferencas_por_entrada(ref, sombra, chave="intencao_id")`;
- `comparacao.nao_inferioridade(dif, margem)`;
- `metricas.agrupar_por_episodio(trades, episodios)`.

**Histórico:**
- `historico/runner.py`: resumo por posição no dia e gancho opcional para `contrafactual.contrafactuais_da_sessao`.
- `historico/cli_cmds.py`: `risco_para` e `--aprovacao` padrão `None`, resolvido por `resolver_aprovacao` (C25).

**Backtest (`backtest/motor.py`):**
- recusa braço `planejador` com `ErroConfig` ("use o Orquestrador/historico");
- o `_dossie` v1 usa `dossie_conta.limites_legado` e os campos de posição atuais, em paridade com o ao vivo **[v1.1]**.

**CI e docs:**
- CI com `--ultimos "$((DIAS + 25))"`, cache de 60 pregões, agendamento diário e artefatos com `previsoes_ia` e `contrafactuais`;
- doc 15 corrigido; doc 16 descreve o pedido, o contrato, a ordem do veto, as variantes e as pendências (prompts v1 com WIN fixo).

**Testes:** `test_aval_impostos_relatorio.py`, `test_aval_metricas_comparacao.py`, `test_historico.py` e `test_backtest_motor.py`:
- aviso por braço; seção de injetados; episódios;
- selo recusado antes do registro e com FALSO;
- CLI com `risco_para`;
- paridade de chaves entre o dossiê do backtest e o do ao vivo.

### WP9 – Avaliação estatística, pré-registro, contrafactual, calibração, proveniência, rotulagem

**Arquivos:** `avaliacao/preregistro.py` (novo), `avaliacao/contrafactual.py` (novo), `avaliacao/calibracao_ia.py` (novo), `avaliacao/cli_cmds.py`, `modelos/calibracao.py`, `modelos/rotulagem.py`, `dados/b3_negocios.py`, `config/experimentos/` (novo, só por acréscimo).

**`preregistro.py`:**

```python
def registrar(pr: PreRegistro, dir: Path = Path("config/experimentos")) -> PreRegistro
def verificar(dir: Path) -> list[PreRegistro]
def elegivel(pr: PreRegistro, *, data_pregao: str, hashes_variantes: Mapping[str, str],
             provedor_falso: bool) -> tuple[bool, tuple[str, ...]]
def n_tentativas(dir: Path) -> int
```

`registrar` grava `<id>.toml` e acrescenta em `registro.jsonl` com cadeia sha256, no estilo de `auditoria/cadeia.py`. `verificar` levanta erro se a cadeia estiver quebrada. A CLI fica em `avaliacao/cli_cmds.py` (descoberta automática): `robowin experimento registrar|verificar`.

**`contrafactual.py`:**
- `replay_operacao(ticks, livros, perfil, *, t_entrada_ns, lado, stop, alvo, tempo_max_s, q, gestao, seed) -> ResultadoReplay`: usa um `SimuladorExecucao` novo com `RelogioSimulado`, mesmo preenchimento e mesma latência, e `gestao.simular_caminho`. Devolve `liquido`, `motivo`, `t_fim` e `y_alvo`.
- `contrafactuais_da_sessao(banco, ticks)` cobre:
  - entradas executadas de P: "sem gestão" e "trailing fixo";
  - entradas **não executadas** (faixa ou ancoragem, revisor, veto por motivo da intenção, plano expirado): o plano da IA a partir da entrega;
  - `seed = sha256(decisao_id)` (números aleatórios comuns);
  - `y_alvo` de `previsoes_ia`, sempre pelo replay do plano original sem gestão.

  É só diagnóstico e nunca altera limites.

**`calibracao_ia.py`:**
- `pontuar(previsoes)` calcula Brier, Brier de referência (taxa-base de sessões anteriores e o teórico `s/(s+a)`), BSS com bootstrap por pregão, ECE e curva de confiabilidade;
- `recalibrador_walk_forward(previsoes, t)`: isotônica só com o passado a partir de `MIN_CALIBRACAO=200`; antes disso, encolhe para a taxa-base com peso `n/(n+n0)`.

**`modelos/calibracao.py`:** `decomposicao_brier` (Murphy).

**`modelos/rotulagem.py`:** `ConfigBarreiras.modelo_execucao: Literal["toque", "simulador"] = "toque"`. `"simulador"` define a variante `quant_v2` (C23).

**`dados/b3_negocios.py`:** proveniência (C24).

**Testes:** novos `test_aval_preregistro.py` (hash estável, cadeia quebrada detectada, elegibilidade, `n_tentativas`), `test_aval_contrafactual.py` (mesma seed dá o mesmo resultado; replay "sem gestão" de operação sem gestão bate com o trade real), `test_aval_calibracao_ia.py` (valores conhecidos; sem vazamento: mudar desfechos futuros não altera `p_cal(t)`), e os existentes `test_modelos_rotulagem.py` e `test_dados_b3.py`.

---

## 4. Medição sem sobreajuste

### 4.1 Braços pré-registrados (experimento `exp-planejador-AAAAMMDD`)

- **Tratamento:**
  - `planejador` (P, sem revisor);
  - `planejador_revisor` (PC, mesma chamada pareada mais `revisor_plano`).
- **Sombras de P** (custo de API zero; conta e motor próprios com o mesmo `ConfigRisco`; nunca espelhadas):
  - `sombra_sem_gestao`: mesmas entradas, mesmo stop, alvo e tempo, sem regras nem ajustes;
  - `sombra_trailing_fixo`: mesmas entradas, gestão determinística pré-registrada.
- **Controles:**
  - `controle_aleatorio_plano`: plano compilado de P com lado sorteado;
  - `nao_operar`, `controle_aleatorio`.
- **Linha de base:** `claude_gpt_cond` (C v1.1), `claude_gpt`, `claude` (B), `quant`, opcionalmente `quant_v2`.

### 4.2 Métrica e hipóteses

**Métrica primária:** líquido **por pregão** na moeda da conta, **descontado o custo de API do pregão** (pela cotação registrada). As comparações são pareadas por pregão. Como a IA escolhe frequência e quantidade, a expectativa por operação engana e fica como secundária, junto com o múltiplo de R (líquido dividido pelo pior caso aprovado).

**Hipóteses primárias** (Holm com m=3, unilaterais, α=0,05):

| Hipótese | Comparação | O que mede |
|---|---|---|
| H1 | P − C condicional > 0 | Vale dar a decisão completa à IA |
| H2 | P − controle_aleatorio_plano > 0 | Há habilidade de direção e de momento além do plano |
| H3 | P − sombra_sem_gestao > 0 | A gestão agrega valor; também por entrada, só pares completos |

**Não inferioridade de risco** (margens no `PreRegistro`; condição para promover, valha o que valer o P&L): pior dia, fração de dias que atingem a perda diária, drawdown máximo e perda média das perdedoras, sempre P contra C.

**Exploratórias** (relatadas, sem afirmação de resultado):
- P − sombra_trailing_fixo (a IA bate uma regra simples?);
- PC − P (valor do revisor);
- BSS de `p_alvo` e da `incerteza`;
- custo de cada filtro pelos contrafactuais das entradas bloqueadas;
- motivos de saída;
- ações de gestão por posição;
- taxa de pares incompletos;
- latência e custo por pregão.

### 4.3 Protocolo

1. **Registro antes da coleta:**
   - `robowin experimento registrar` grava os hashes de `planejador_v1.md`, `revisor_plano_v1.md`, esquemas, `dsl_gestao:v1`, `compilador:v1`, `[planejador]`, `[planejador.gatilhos]`, `prazos:planejador`, `correcoes:v1.1`, regra da sombra trailing, seeds, margens e `n_pregoes_alvo`;
   - os 9 pregões já vistos e todas as rodadas de desenvolvimento ficam como dados de projeto;
   - `decidir_selo` recusa pregão com data ≤ `t_registro`, hash divergente ou FALSO.
2. **Qualquer mudança** de prompt ou parâmetro cria uma variante nova (`planejador_v2`), com registro novo. `n_tentativas()` entra no DSR.
3. **Dados:** só chaves reais e pregões posteriores ao registro e ao corte de treino do modelo (rótulo de contaminação que já existe). O FALSO serve só para testar o encanamento.
4. **Tamanho e parada:**
   - `n_pregoes = n_expectativa(1,5 × sd das diferenças diárias pareadas, MDE)`, com o sd estimado num piloto que depois é descartado;
   - parada antecipada só por `estatistica.sprt_media_normal` sobre a diferença diária, com limites registrados, para dano ou futilidade;
   - relatórios intermediários levam o rótulo "interino" e não recebem selo.
5. **Calibração:** `p_alvo` bruto nunca bloqueia nem libera. Uma variante exploratória com filtro por EV usando `p_cal` walk-forward só é permitida depois de 200 previsões resolvidas, e como variante registrada.
6. **Contrafactuais:** só como diagnóstico, nunca como métrica primária, e nunca alteram limites.
7. **Dados acumulados:** CI diário e cache, porque a B3 mantém só uns 20–30 pregões online. Antes de atingir n, o relatório diz "amostra insuficiente".

---

## 5. Critérios de aceite verificáveis

Rodar na raiz `/home/user/acompanhamentoprefeitura`:

1. `uv run ruff check .` sem erros.
2. `uv run mypy src` sem erros.
3. `uv run pytest -rfE` com tudo verde, incluindo todos os testes novos listados nos WPs.
4. `uv run pytest tests/estaticos tests/unit/test_mt5_ast.py tests/unit/test_feat_registro.py -q`: `order_send` só em `execucao_demo.py`; nenhum indicador; sem literais `5.0`/`0.20` novos; `ia/` e `decisao/bracos/` sem import de `robowin.risco` nem `ConfigRisco`; prompts do planejador genéricos.
5. `uv run pytest tests/propriedades -q`: nenhuma intenção aprovada fura um invariante.
6. `git diff --exit-code -- config/risco.toml config/aprovacoes/`: o arquivo aprovado e as aprovações não mudaram.
7. Política carregada e teto padrão:
   ```
   uv run python -c "from robowin.risco.politica import carregar_politica as c; print(c('config/risco.toml','config/aprovacoes/risco-v1.toml').tempo_max_posicao_s)"
   ```
   imprime `300`.
8. Isolamento da IA:
   ```
   uv run python -c "from robowin.risco.usuarios import validar_ia_sem_limites as v
   try: v({'planejador':{'tempo_max_posicao_s':900}}); raise SystemExit('FALHOU')
   except Exception as e: print('ok', type(e).__name__)"
   ```
   imprime `ok ErroConfig`.
9. Esquemas legados congelados: `uv run pytest tests/unit/test_contratos_planejador.py -q`. O sha256 de `PropostaIA`/`RevisaoIA.model_json_schema()` é igual às constantes capturadas antes.
10. Ponta a ponta com FALSO:
    ```
    uv run robowin simular --seed 7 --saida /tmp/claude-0/-home-user-acompanhamentoprefeitura/4e76b7e9-4157-5c79-bc7e-357230d1ff29/scratchpad/sim_plan
    ```
    Depois, `uv run python -I` com `sqlite3` sobre o banco gerado deve mostrar:
    - pelo menos um registro em `posicoes_episodio` com `braco='planejador'`;
    - pelo menos um em `gestao_eventos` com `origem='auto' AND aplicado=1`;
    - em `gestao_eventos`, `sl_depois` monotônico por ticket (nunca afrouxa);
    - toda posição do planejador com `t_fechada_ns - t_aberta_ns <= (tempo_max_posicao_s + 2)*1e9`;
    - nenhuma decisão `executada` de `MANTER`/`ENCERRAR` com ticket diferente do de t0;
    - nenhum braço `sombra_*`/`controle_*` como braço espelhado.
11. Pré-registro: `uv run robowin experimento registrar --id exp-teste --dir <tmp>` seguido de `uv run robowin experimento verificar --dir <tmp>` termina com código 0. Adulterar uma linha de `registro.jsonl` faz `verificar` terminar com código diferente de 0.
12. Sem rede e sem chave: `env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY uv run pytest -rfE` continua verde. Os testes não abrem conexões; os dublês ficam como estão.
13. Revisão manual de `docs/16-decisao-completa-ia.md`. O documento deve listar:
    - o que a IA decide;
    - a ordem do veto (IA, validador, compilador e ancoragem, motor, aprovação por hash, OMS, guardião);
    - as variantes e hipóteses;
    - as pendências a confirmar com o usuário: o padrão `expor_envelope`; `[planejador].ativo` ligado na config; aumentar `tempo_max_posicao_s` só com edição do usuário e nova aprovação por hash.

---

## 6. Resumo para revisão: pedido, contrato, veto, variantes e pendências

Esta seção resume a especificação acima para a revisão manual do critério 13 da seção 5. Se houver divergência, valem as seções 1 a 5.

### 6.1 O pedido

A IA passa a decidir o trade inteiro: entrar ou não, lado, momento (agora ou por gatilho), faixa de execução assimétrica, quantidade até o teto do envelope, stop, alvo, tempo máximo, regras de gestão, quando ser chamada de novo e `p_alvo`. Com posição aberta, ela pode encerrar, manter ou ajustar: apertar o stop, mover o alvo, mudar o tempo, fazer parcial ou trocar a gestão. A lista completa está na seção 1.2.

O risco determinístico continua com o **veto final**. A IA nunca lê `ConfigRisco`, arquivos, hashes, usuário ou conta. Ela recebe no máximo o `EnvelopeDecisao`, em pontos, unidades e segundos, calculado pela mesma função que o motor usa.

### 6.2 O contrato

- **Saída da IA.** `PlanoTradeIA`, papel `planejador`, com `extra="forbid"`. Nenhum campo chega a um limite.
- **Revisor opcional.** `RevisaoPlanoIA`, papel `revisor_plano`. Os booleanos `coerente`, `dados_ok` e `regras_ok` são obrigatórios.
- **Compilação.** O plano vira `PlanoCondicional` mais `PlanoGestaoCompilado`, com níveis ancorados em t0. Ajustes da IA viram `AjustePosicao`, em pontos de resultado a partir do preço médio.
- **Registro.** Os resultados vão para as tabelas `posicoes_episodio`, `gestao_eventos`, `previsoes_ia` e `contrafactuais`, fora do esquema congelado M0.
- **Esquemas legados.** `PropostaIA` e `RevisaoIA` estão congelados, com sha256 conferido em teste.

### 6.3 A ordem do veto

Cada etapa só restringe; nenhuma desfaz o veto da anterior.

1. **IA.** Propõe o plano ou o ajuste. `p_alvo` nunca libera nem bloqueia.
2. **Validador** (`validar_plano_trade`). Confere o esquema, números finitos, evidências com valor conferido, sanitização (`texto_seguro`) e os limites do envelope.
3. **Compilador e ancoragem** (`compilar_plano`, `ajustar_entrada_ancorada`). Arredonda para o tick e aplica a faixa assimétrica. Recusa preço fora da faixa, alvo restante abaixo do custo e plano expirado.
4. **Motor de risco** (`MotorRiscoPadrao.avaliar`). Veto final e *fail-closed*. Cobre stop mínimo e só apertando, distância mínima, perdas, operações, janelas, tempo máximo e posição presa ao ticket de t0.
5. **Aprovação por hash** dos limites do usuário+conta (`risco_para` e `resolver_aprovacao`). Sem aprovação válida, o resultado sai rotulado `risco_nao_aprovado`; ao vivo, a operação é recusada.
6. **OMS.** Envia só a ordem da intenção aprovada e registra o estado dela.
7. **Guardião do gateway** no espelho DEMO, inclusive `modificar_sltp`. A conta REAL fica sempre bloqueada e `order_send` existe só em `mt5/execucao_demo.py`.

### 6.4 As variantes e as hipóteses

As variantes estão na seção 4.1: P, PC, `sombra_sem_gestao`, `sombra_trailing_fixo`, `controle_aleatorio_plano`, `nao_operar`, `controle_aleatorio` e as linhas de base C v1.1, B, quant e, opcionalmente, `quant_v2`.

As hipóteses estão na seção 4.2: H1 P > C, H2 P > controle do plano e H3 P > sombra sem gestão, com Holm m=3 e mais a não inferioridade de risco. A métrica primária é o líquido por pregão, já descontado o custo de API.

O relatório (`avaliacao/relatorio.py`, `historico/relatorio.py`) mostra:

- aviso de acerto por braço, com a geometria realizada;
- seção "injetado (FALSO)", fora de qualquer comparação;
- tentativas contra episódios;
- gestão por posição;
- diferenças pareadas por entrada com as sombras (`comparacao.diferencas_por_entrada`), com `nao_inferioridade` disponível para as margens pré-registradas;
- dias encerrados por perdas seguidas.

O selo é **recusado** para:

- pregão com data igual ou anterior ao registro;
- hash de variante divergente;
- provedor FALSO;
- cadeia de pré-registro quebrada (`decidir_selo(..., preregistro=..., dir_preregistro=...)`).

### 6.5 Pendências

**A confirmar com o usuário:**

- **`[planejador].expor_envelope`.** O padrão é `"envelope"`. A alternativa `"nenhum"` existe para o caso de "a IA nunca lê limites" ser literal.
- **`[planejador].ativo = true`** na configuração. Isso liga P, PC, as sombras e o controle do plano nas simulações.
- **`tempo_max_posicao_s`.** O padrão é 300 s. Aumentar exige edição do usuário e **nova aprovação por hash**: nem a IA nem esta mudança alteram `config/risco.toml` ou `config/aprovacoes/`.

**Técnicas, registradas para o próximo ciclo:**

- **Prompts v1 com WIN fixo.** `config/prompts/proponente_v1.md` e os demais prompts v1 citam WIN, B3 e "R$ 0,20" (C27). Eles ficam congelados pelo pré-registro dos braços B e C. Generalizá-los cria uma variante nova (`*_v2`) com registro novo. Só os prompts do planejador já são genéricos.
- **Pareamento com as sombras.** O pareamento por entrada usa `intencao_id`. Uma sombra cujo id deriva do id de referência (prefixo seguido de separador) casa com ele. Se a fábrica de sombras usar outro esquema de id, ajuste a chave em `secao_sombras`.
- **Backtest.** O backtest recusa o braço `planejador` (`ErroConfig`). O planejador roda só no Orquestrador (`robowin simular`, `robowin historico`).