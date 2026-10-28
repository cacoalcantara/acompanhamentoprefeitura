# Avisos de terceiros (THIRD_PARTY_NOTICES)

## day-trader-skill (Jake Schincariol) — licença MIT

A **ideia e o método** do módulo "Máquina do Tempo" (`src/robowin/avaliacao/maquina_do_tempo.py`) foram
**inspirados no "Time Machine"** do projeto open source **day-trader-skill**, de Jake Schincariol
(<https://github.com/Jakeschincariol/day-trader-skill>, componente `trader-timemachine`), distribuído sob a licença
MIT: reamostragem de barras de 5 minutos do próprio ativo, na mesma hora do dia (± 2 barras), de sessões completas
anteriores, reescaladas ao preço atual; verificação do stop antes do alvo (barra que toca os dois conta como stop);
reaplicação da mesma regra nas sessões passadas reais; comparação do resultado com o ponto de equilíbrio 1/(1+R);
seed padrão 19 e recusa com menos de 20 sessões completas.

**Ideia e método do Time Machine inspirados em day-trader-skill (Jake Schincariol, MIT); reimplementação
independente.** Nenhum código do day-trader-skill foi copiado: o robowin reescreveu o método do zero, em Python com
numpy, sobre os tipos do próprio projeto (perfil de instrumento, grade de sessão da bolsa, ticks do armazém, custos
de execução, moeda do instrumento), com custos líquidos, slippage de stop opcional e integração como evidência
anonimizada para o revisor de IA. O day-trader-skill é um pacote de skills do Claude para ações dos EUA com dados do
Yahoo; não é um motor de execução e não tem relação com MT5. Ver `docs/13-inspiracoes.md`.

Por transparência (e porque a licença exige que o aviso de copyright e o texto da permissão acompanhem cópias ou
partes substanciais do software), o texto original da licença segue abaixo, sem alterações:

```
MIT License

Copyright (c) 2026 Jake Schincariol

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
