# 08 — Aprendizado controlado

- **Memória de operações:** resumo do passado por braço no dossiê; não altera parâmetros.
- **Ajuste de parâmetros:** mensal, walk-forward, gera *desafiante*; sem busca de parâmetros na v1.
- **Treinamento de modelos:** janela móvel com purga/embargo; artefatos com hash de dados e código
  (`modelos.registro`). Status: nao_treinado / em_validacao / validado_oos / reprovado.
- **Promoção:** critério OOS pré-registrado, comparação com o campeão, reversão por ponteiro; holdout usado
  uma vez por versão. Prompts versionados por hash; cada iteração conta como tentativa.
- **Regras de risco:** imutáveis em execução; mudar exige editar, testar e nova aprovação com hash.
