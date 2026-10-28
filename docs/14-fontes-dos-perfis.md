# 14 — Fontes dos perfis de ativo (pesquisa de 28/10/2026)

Pesquisa feita por busca na web a partir do sandbox. O site da B3 (b3.com.br) está **bloqueado** para
leitura direta neste ambiente; por isso os PDFs oficiais encontrados (ofícios circulares) não puderam ser lidos
e **nenhum perfil foi liberado** (`a_confirmar` continua `true` em todos, exceto WIN). A liberação é feita por
você com `robowin perfil confirmar <ATIVO>` depois de conferir a ficha técnica oficial.

| Ativo | Campo | Valor no perfil | O que as fontes dizem | Situação |
|---|---|---|---|---|
| IND | valor do ponto | R$ 1,00 | Página da B3 "Futuro de Ibovespa": cada ponto = R$ 1,00 | **Confere** (fonte oficial) |
| IND | lote | 5 (corrigido) | **B3 oficial: lote padrão 5 contratos** | Corrigido (ponto por lote = R$ 5) |
| IND | vencimento | quarta mais próxima do dia 15, meses pares | Meses pares confirmados (InvestNews); dia não confirmado | Parcial |
| WDO | valor do ponto | R$ 10,00 | Toro/Eu Quero Investir: ponto = R$ 10; contrato US$ 10.000 | Confere (fontes secundárias) |
| WDO | tick | 0,5 ponto (R$ 5) | Variação mínima BRL 0,50 por USD 1.000 = R$ 5 por contrato | Confere (fontes secundárias) |
| WDO | vencimento | 1º dia útil do mês | **Conflito**: Bora Investir (B3) diz "quarta-feira mais próxima do dia 15"; C6 diz "último dia útil"; existe o OC 015/2025-VPC da B3 que **alterou** último dia de negociação/data de vencimento dos futuros de moedas | **NÃO confirmado — provável mudança em 2025** |
| DOL | valor do ponto / tick | R$ 50 / 0,5 ponto (R$ 25) | Contrato US$ 50.000; variação mínima BRL 0,5 por USD 1.000 (o R$ 50/ponto é cálculo, não citação) | Confere por cálculo |
| DOL | vencimento | 1º dia útil do mês | Mesmo conflito do WDO (OC 015/2025) | **NÃO confirmado** |
| PETR4, BOVA11 | tarifas | placeholder | Ações/ETF pagam emolumentos percentuais, não por contrato | Placeholder, não usar |

Consequência prática: **WDO e DOL não devem ser liberados** até você ler o OC 015/2025-VPC e a ficha atual
do contrato na B3 (a regra de vencimento no código pode estar desatualizada). IND precisa conferir o lote.

## Fontes
- B3 — Futuro de Ibovespa: https://b3.com.br/pt_br/produtos-e-servicos/negociacao/renda-variavel/futuro-de-ibovespa.htm
- B3 — OC 015/2025-VPC (alteração de último dia de negociação/data de vencimento dos futuros de moedas):
  https://www.b3.com.br/data/files/10/B1/42/9E/677379106B8BCB69AC094EA8/OC%20015-2025-VPC_ALTERACAO%20CONTRATOS%20FUTUROS%20MOEDAS%20ESTRANGEIRAS%20%C3%9ALTIMO%20DIA%20NEG%20DATA%20FIXING%20%20DATA%20DE%20VENC_PORT.pdf
- B3 — OC 022/2025-VPC (alteração nos contratos futuros de moedas): https://www.b3.com.br/data/files/AB/B6/22/F5/67F77910AE23A679AC094EA8/OC%20022-2025-VPC%20ALTERACAO%20NOS%20CONTRATOS%20FUTUROS%20DE%20MOEDAS%20ESTRANGEIRAS%20PARES%20EM%20BRL_PORT.pdf
- Bora Investir (B3) — Mini dólar: https://borainvestir.b3.com.br/glossario/mini-dolar/
- Toro — Mini dólar: https://blog.toroinvestimentos.com.br/trading/mini-dolar-o-que-e-como-funciona-wdo/
- Eu Quero Investir — Mini dólar: https://euqueroinvestir.com/educacao-financeira/mini-dolar
- C6 Bank — Contratos futuros: https://www.c6bank.com.br/blog/contratos-futuros
- InvestNews — Ibovespa futuro: https://investnews.com.br/investimentos/ibovespa-futuro-o-que-e/
- Suno — Índice futuro: https://www.suno.com.br/artigos/indice-futuro

## Atualização — fichas oficiais da B3 lidas pela CI (job `fontes-b3`, 28/10/2026)

| Ativo | Ficha oficial B3 | Perfil |
|---|---|---|
| WIN | R$ 0,20/ponto; tick 5 pontos; lote 1; vence na quarta mais próxima do dia 15, meses pares | Confere (já confirmado) |
| IND | R$ 1,00/ponto; tick 5 pontos; **lote padrão 5**; mesma regra de vencimento | Lote corrigido para 5 (R$ 5/ponto por lote) |
| WDO | USD 10.000; tick BRL 0,50 por USD 1.000 (R$ 5); lote 1; **vence no 1º dia útil do mês**, todos os meses | Confere |
| DOL | USD 50.000; tick BRL 0,5 por USD 1.000; **lote padrão 5**; vence no 1º dia útil | Lote corrigido para 5 (R$ 250/ponto por lote) |

O OC 015/2025 e o OC 022/2025 alteraram datas dos futuros de **outras moedas** (pares em USD e em BRL); o próprio
OC 022/2025 diz: "Os contratos Futuros de Taxa de Câmbio de Reais por Dólar Comercial (BRL/USD) permanecem
inalterados". A regra do WDO/DOL no perfil está correta. Continuam a confirmar: tarifas, margem (corretora), limites
em ticks e sessão.
