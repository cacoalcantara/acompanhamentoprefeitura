# Pre-registros de experimento

Diretorio **so de acrescimo** (docs/16, secao 4.3). Cada experimento confirmatorio e registrado ANTES da coleta com

    uv run robowin experimento registrar --id exp-planejador-AAAAMMDD --n-pregoes <n do piloto> \
        --margem pior_dia=<margem> --margem drawdown_max=<margem>

que grava `<id>.toml` e acrescenta uma linha encadeada (sha256) em `registro.jsonl`. Para conferir a cadeia:

    uv run robowin experimento verificar

* Nunca edite nem apague um `<id>.toml` ou uma linha de `registro.jsonl`: `verificar` termina com codigo != 0.
* Qualquer mudanca de prompt ou parametro cria uma variante nova (ex.: `planejador_v2`) com registro novo; o total
  de variantes da cadeia entra no DSR como numero de tentativas.
* Pregoes com data igual ou anterior ao registro, hash de variante divergente ou provedor FALSO nao recebem selo.
