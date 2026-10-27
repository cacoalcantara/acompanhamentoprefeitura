# Instala o robowin no Windows (requer Python 3.13 x64 e o terminal MetaTrader 5 da corretora).
$ErrorActionPreference = "Stop"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { irm https://astral.sh/uv/install.ps1 | iex }
uv sync --extra mt5 --extra dev
Write-Host "Pronto. Abra o MT5, entre na conta DEMO, ative Algo Trading e rode: uv run robowin diagnostico"
