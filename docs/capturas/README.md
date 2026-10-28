# Capturas do painel (SOMENTE simulacao)

Geradas por `scripts/capturas_painel.mjs` (Node + Playwright/Chromium real, 1280x900 e celular 390x844) contra
`robowin simular --seed 5` + `robowin painel` com `ROBOWIN_TOKEN` definido. Dados sinteticos.

Como repetir:
`PAINEL_URL=http://127.0.0.1:8799 ROBOWIN_TOKEN=... DIR_DADOS=<dir passado em --dados> node scripts/capturas_painel.mjs`

## Verificado (84 verificacoes, 0 falhas)
- As 6 paginas (Visao geral, Decisoes, Operacoes, Desempenho, Modelos, Auditoria) carregam em desktop e celular, com PNG por pagina.
- Faixas "SIMULAÇÃO — sem ordens reais" e "DADOS SINTÉTICOS" presentes em todas; `lang=pt-BR`; sem textos tipicos em ingles/placeholder.
- Nenhum erro/aviso de console, `pageerror` ou resposta HTTP >= 400.
- Grafico uPlot da curva de capital renderiza `<canvas>`.
- htmx: `/fragmentos/resumo` e requisitado (a cada 5 s) e `#resumo` e substituido.
- Kill switch: sem token => 401 e flag nao gravada; token sem a frase => 400 e flag nao gravada; token + digitar EMERGENCIA (confirmando o dialogo) =>
  flag gravada e UI mostra "aguardando confirmação do gateway", sem "Confirmado:" (nenhum gateway reportando); permanece assim apos recarregar.
- Celular: sem scroll horizontal da pagina (tabelas largas rolam dentro do proprio cartao).

## Bug encontrado e corrigido
- Eixo/legenda do grafico de capital saiam em ingles (am/pm, "3/11/26") e no fuso do navegador, destoando do resto do painel (UTC, pt-BR).
  Corrigido em `src/robowin/painel/static/painel.js` (Intl pt-BR, UTC, 24 h); teste `tests/unit/test_painel_grafico_ptbr.py`.

## Nao verificado / limites
- Somente Chromium; nao testados Firefox/WebKit nem modo escuro visual.
- Confirmacao do gateway (estado "Confirmado") nao exercida de ponta a ponta no navegador (nao ha gateway; coberta por testes unitarios).
- Tabelas largas no celular exigem rolagem horizontal dentro do cartao (colunas finais ficam cortadas ate rolar).
- Sem teste de acessibilidade, desempenho ou carga; rate limit de token invalido nao exercitado no navegador.
