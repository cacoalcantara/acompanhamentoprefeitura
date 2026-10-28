# 04 — Plano de testes e critérios de aprovação

Testes: `uv run pytest` (unidade, propriedades, integração com dublê, falhas, E2E multiprocesso, estáticos).
Etapas e portões (pré-registrados por manifesto):

| Etapa | Avança se |
|---|---|
| 0 Infra e dados | ≥ 10 pregões coletados, reconciliação 100%, auditoria íntegra, latências medidas |
| 1 Backtest | IC95% da expectativa > 0 (bootstrap por pregão), FL > 1,2, DD p95 < 50% do limite, DSR > 0,95, robusto a ±20% |
| 2 Fora da amostra | IC > 0 ou dentro das faixas da etapa 1 (período intocado, uma vez por versão) |
| 3 Simulação em tempo real | n pelo MDE (≈ 385 operações para E=R$5; ≈ 1.070 para E=R$3), p99 nos prazos, zero incidente crítico, LLM só se ganho incremental líquido do custo tiver IC inferior > 0 (Holm) |
| 4 Real com 1 contrato | autorização expressa + checklist (`07`) |
| 5 Aumento gradual | +1 contrato por degrau; ≥ 2 meses e o n do MDE; sequência curta de ganhos nunca autoriza aumento |

Amostras: ±5 p.p. em p=0,85 → 196; ±3 p.p. → 545. Zona cinzenta: estender até n máximo e então rejeitar.
Implementação: `robowin.avaliacao`.

## Simulação histórica causal com dados reais da B3 (`robowin historico`)
Replay walk-forward dos últimos pregões públicos da B3 (negócios; `docs/03-dados-integracoes-infra.md`). Para cada
pregão D, em ordem: o modelo quant é treinado **só com pregões < D** (mesmos parâmetros do `simular`, sem busca), a
Máquina do Tempo usa **só sessões < D** e D é reproduzido por **instante de disponibilidade** (`t_evento` + atraso de
entrega) no mesmo orquestrador do `simular` (custos do perfil, latência de execução injetada, motor de risco com veto,
controles aleatórios, IA pelo provedor configurado e dossiê anonimizado). Pregão posterior ao corte de conhecimento dos
LLMs do catálogo = **limpo**; anterior = **contaminado** (o selo `validado` fica recusado). Relatório por dia e consolidado
(expectativa com IC por bootstrap por pregão, acerto com IC de Wilson ao lado do controle, Holm, drawdown, operações,
vetos/bloqueios, latências, tempos).

Testes (sem rede e sem chave; `tests/unit/test_dados_b3.py`, `tests/unit/test_historico.py`): parser no formato exato,
cancelamentos, fuso, lado inferido, agregação, leitura em blocos = leitura inteira, download com quedas/ZIP truncado/503
e cache, contrato vigente por pregão, recusa de mistura real/sintético; compactação conserva volume, extremos e o
desequilíbrio por lado e **perturbar a partir de T não muda nenhum evento anterior a T**; o modelo de D nunca vê D nem o
futuro (amostra de D escondida é detectada); **perturbar o futuro de D não muda as decisões anteriores ao instante
perturbado**; fluxo ponta a ponta com 3 pregões de fixture.

Desempenho medido (sandbox de 4 núcleos, 28/10/2026; pregões SINTÉTICOS densos no formato B3 com 6,5 milhões de
negócios cada, ZIP de ~42 MB): importação ~14 s por pregão (6,5 M linhas → 4,4 M ticks com a agregação ms+preço);
`historico --dias 1` com 2 pregões no armazém (o anterior só para treino) levou 97 s no total: carga ~7 s, compactação
causal de 1 s (4,4 M → ~470 mil eventos), treino 0,2 s, replay ~74 s; pico de memória ~2,4 GB. Ordem de grandeza na CI:
~1,5 min por pregão simulado (10 pregões + 5 de treino: ~20 min). Sem compactação o replay de um pregão passa de 7 min.
O workflow imprime os tempos reais de cada pregão (`tempos carga/treino/replay`) e o pico de memória no consolidado.

**Limites honestos:** lado agressor inferido (não observado), cotação inferida (spread de 1 tick), sem livro (features
de livro/OFI/microprice e market making desligados); só ~20-30 pregões disponíveis = amostra muito abaixo do n
pré-registrado e dos 20 pregões do selo; a Máquina do Tempo exige 20 sessões anteriores e fica **indisponível** até o
cache acumular pregões. Nada disso prova lucratividade.

