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
