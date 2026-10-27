# Inicia gateway, nucleo e painel (simulacao). A senha da conta demo vem da variavel ROBOWIN_MT5_SENHA.
$env:ROBOWIN_TOKEN = if ($env:ROBOWIN_TOKEN) { $env:ROBOWIN_TOKEN } else { [guid]::NewGuid().ToString() }
Start-Process uv -ArgumentList "run robowin gateway"
Start-Process uv -ArgumentList "run robowin nucleo"
Start-Process uv -ArgumentList "run robowin painel --banco dados_locais/robowin.sqlite --porta 8000"
Write-Host "Painel: http://127.0.0.1:8000  (token: $env:ROBOWIN_TOKEN)"
