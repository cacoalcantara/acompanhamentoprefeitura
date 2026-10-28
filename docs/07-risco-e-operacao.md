# 07 — Risco e operação (runbook)

Política: `config/risco.toml` travada por hash (`config/aprovacoes/`). Padrões: 1 contrato; R$ 60/operação;
R$ 200/dia; R$ 500/semana; drawdown R$ 1.000 alerta / R$ 1.500 máximo; entradas 09:15–17:30; zeragem 17:45.
**Responsável precisa aprovar** o registro antes de qualquer etapa real.

- **Emergência:** botão do painel (token), `robowin emergencia` ou arquivo `EMERGENCIA`; cancela, zera,
  bloqueia. No modo dividido, use também o "fechar tudo" manual do MT5.
- **Desconexão:** feed parado bloqueia entradas; sem heartbeat 15 s bloqueia, 60 s zera a posição demo.
- **Reconciliação:** a cada 10 s; deals manuais (CLIENT/MOBILE/WEB) bloqueiam.
- **Fim do dia:** zeragem pelo robô antes da zeragem compulsória da corretora.
- **Gap:** ver `robowin.risco.estresse`. Um movimento até o limite pode custar ≈ R$ 3,5 mil.
- **Checklist antes da etapa real:** confirmação escrita da corretora (robôs/API, zeragem, margem,
  mensagens), políticas da Anthropic e da OpenAI revisadas, capital e limites definidos, ciência do risco de
  gap, autorização expressa. **O envio real não existe nesta versão.**
- **Teste na demo:** saída com ticket velho deve ser rejeitada (não inverter a posição).

## Troca de ativo: risco
Limites em pontos são derivados do perfil (`stop_max_ticks` x tick). O `config/risco.toml` aprovado vale só para o
ativo com `risco_aprovado_padrao = true` (WIN). Outro ativo usa limites mínimos (limites em moeda do `risco.toml`,
menos operações/dia, stop/folga do perfil) e o rótulo `risco_nao_aprovado`, até existir `config/risco-<ativo>.toml`
com registro de aprovação por hash; ao vivo, sem aprovação, a sessão é recusada. Perfil `a_confirmar` bloqueia
qualquer operação (`PerfilNaoConfirmado`). Trocar o ativo exige posição zerada e abre novo experimento; modelo,
orçamento e limites de um ativo nunca são reutilizados em outro.
