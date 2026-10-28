# Instala o robowin no Windows (requer Python 3.13 x64 e o terminal MetaTrader 5 da corretora).
#   powershell -ExecutionPolicy Bypass -File scripts\windows\instalar.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\instalar.ps1 -SemTerminal   (execucao seca: instala e
#       confere o pacote e a CLI sem precisar de terminal MT5; e o modo usado pela CI)
# AVISO: escrito SEM PODER SER EXECUTADO no ambiente de desenvolvimento (Linux); a CI do GitHub o executa com -SemTerminal.
param(
    [switch]$SemTerminal
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $raiz

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
uv sync --extra mt5 --extra dev
if ($LASTEXITCODE -ne 0) {
    Write-Host "FALHA: uv sync terminou com codigo $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

if ($SemTerminal) {
    # Execucao seca: nada aqui fala com terminal MT5 nem envia ordem.
    uv run robowin --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FALHA: 'uv run robowin --help' terminou com codigo $LASTEXITCODE" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    uv run python -c "import MetaTrader5 as m; print('pacote MetaTrader5', m.__version__)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FALHA: o pacote MetaTrader5 nao importou (Windows x64 + Python x64?)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
    Write-Host "Pronto (execucao seca, -SemTerminal): pacote instalado e CLI ok; terminal MT5 nao foi usado."
    exit 0
}

Write-Host "Pronto. Abra o MT5, entre na conta DEMO, ative Algo Trading e rode: uv run robowin diagnostico"
