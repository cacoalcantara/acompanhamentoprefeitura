<#
.SYNOPSIS
  Valida o robowin no SEU Windows e empacota os relatorios (sem segredos) em um .zip para voce me enviar.

.DESCRIPTION
  Passos (cada um registra OK/FALHOU; o script continua e no fim sai com codigo != 0 se algum falhou):
    1. instala com uv (uv sync --extra mt5 --extra dev)
    2. pytest
    3. scripts\conferir_constantes_mt5.py   (so MT5: confere as constantes contra o pacote MetaTrader5 oficial)
    4. scripts\lint_mql4.py                 (linter estatico do EA; NAO e compilador)
    5. robowin verificar                    (matriz APROVADO / REPROVADO / NAO VERIFICAVEL)
    6. compilar_ea.ps1                      (so MT4: compila o EA no MetaEditor)
    7. robowin aceitacao --confirmo-conta-demo   (SO se voce passar -ConfirmoContaDemo; ordens de teste de 1 contrato na DEMO)
    8. zip da pasta de relatorios SEM segredos (.env, chaves, senhas) com MANIFESTO.txt (sha256 de cada arquivo)
    No fim imprime O QUE FALTA (robowin pendencias): aprovacao de risco pendente, perfis a confirmar e chaves de API
    ausentes, cada item com o comando exato. Nenhuma dessas decisoes e tomada pelo script.

  SOMENTE CONTA DEMO. Sem -ConfirmoContaDemo nenhuma ordem e enviada. O robowin recusa conta real automaticamente.
  A senha da conta demo NAO vai por parametro: defina antes  $env:ROBOWIN_MT5_SENHA  (ou deixe o terminal ja logado).

  *** AVISO: este script foi escrito SEM PODER SER EXECUTADO no ambiente de desenvolvimento (Linux). Nao foi testado. ***

.PARAMETER Plataforma
  mt5 (padrao) ou mt4 (ponte EA).
.PARAMETER ConfirmoContaDemo
  Confirma que a conta logada no terminal e DEMO e autoriza as ordens de teste (1 contrato) da aceitacao.
.PARAMETER Login
  Login da conta DEMO esperada (a aceitacao recusa se o terminal estiver em outra conta).
.PARAMETER Servidor
  Nome do servidor da conta DEMO esperada (precisa conter DEMO).
.PARAMETER Ativo
  simbolo_base do perfil (config\instrumentos.toml). Padrao: o de config\robowin.toml.
.PARAMETER MinutosColeta
  Duracao da coleta de ticks/livro (A4). Padrao 5.
.PARAMETER Repeticoes
  Repeticoes da medicao de latencia (A6) e de chamadas de IA (A10). Padrao 20.
.PARAMETER MetaEditor
  Caminho do metaeditor.exe (so MT4; veja compilar_ea.ps1).
.PARAMETER PastaMql4
  Pasta MQL4 da pasta de dados do terminal MT4 (so MT4).
.PARAMETER Saida
  Pasta dos relatorios (padrao: relatorios).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\validar_tudo.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\validar_tudo.ps1 -ConfirmoContaDemo -Login 12345678 -Servidor "XPMT5-DEMO"
#>
param(
    [ValidateSet("mt5", "mt4")][string]$Plataforma = "mt5",
    [switch]$ConfirmoContaDemo,
    [string]$Login = "",
    [string]$Servidor = "",
    [string]$Ativo = "",
    [double]$MinutosColeta = 5,
    [int]$Repeticoes = 20,
    [string]$MetaEditor = "",
    [string]$PastaMql4 = "",
    [string]$Saida = "relatorios"
)

$ErrorActionPreference = "Continue"
$raiz = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location -LiteralPath $raiz

Write-Host "AVISO: validar_tudo.ps1 foi escrito sem poder ser executado no ambiente de desenvolvimento (Linux); se um passo falhar por motivo do script, me envie a mensagem." -ForegroundColor Yellow
Write-Host "SOMENTE CONTA DEMO. Plataforma: $Plataforma. Ordens de teste: $(if ($ConfirmoContaDemo) { 'SIM (-ConfirmoContaDemo)' } else { 'NAO (sem a flag so ha leitura)' })"

$pasta = Join-Path $raiz $Saida
if (Test-Path -LiteralPath $pasta) { Remove-Item -LiteralPath $pasta -Recurse -Force }
New-Item -ItemType Directory -Force -Path $pasta | Out-Null

$resumo = New-Object System.Collections.Generic.List[object]

function Passo([string]$nome, [scriptblock]$bloco, [string]$arquivoLog) {
    Write-Host ""
    Write-Host "=== $nome ===" -ForegroundColor Cyan
    $cod = 1
    try {
        & $bloco 2>&1 | Tee-Object -FilePath (Join-Path $pasta $arquivoLog) | Out-Host
        $cod = $LASTEXITCODE
        if ($null -eq $cod) { $cod = 0 }
    } catch {
        Write-Host "EXCECAO: $($_.Exception.Message)" -ForegroundColor Red
        $cod = 1
    }
    $estado = if ($cod -eq 0) { "OK" } else { "FALHOU (codigo $cod)" }
    $script:resumo.Add([pscustomobject]@{ Passo = $nome; Resultado = $estado; Log = $arquivoLog })
    Write-Host "-> $estado" -ForegroundColor $(if ($cod -eq 0) { "Green" } else { "Red" })
}

# --- 1. instalar -----------------------------------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv nao encontrado; instalando (https://astral.sh/uv)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Passo "1. instalar (uv sync)" { uv sync --extra mt5 --extra dev } "01-instalar.log"

# --- 2. pytest ----------------------------------------------------------------------------------------------------
Passo "2. pytest" { uv run pytest -q } "02-pytest.log"

# --- 3. constantes do MT5 -----------------------------------------------------------------------------------------
if ($Plataforma -eq "mt5") {
    Passo "3. conferir constantes do MT5" { uv run python scripts/conferir_constantes_mt5.py } "03-constantes-mt5.log"
} else {
    Write-Host "3. conferir constantes do MT5: pulado (plataforma mt4)"
}

# --- 4. lint MQL4 -------------------------------------------------------------------------------------------------
Passo "4. lint estatico do EA (NAO e compilador)" { uv run python scripts/lint_mql4.py mql4/RobowinBridge.mq4 } "04-lint-mql4.log"

# --- 5. verificar -------------------------------------------------------------------------------------------------
Passo "5. robowin verificar" { uv run robowin verificar } "05-verificar.txt"
uv run robowin verificar --json 2>$null | Out-File -Encoding utf8 -FilePath (Join-Path $pasta "05-verificar.json")

# --- 6. compilar o EA (MT4) -----------------------------------------------------------------------------------------
if ($Plataforma -eq "mt4") {
    $args6 = @("-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "compilar_ea.ps1"), "-Log", (Join-Path $pasta "06-compilar-ea-metaeditor.log"))
    if ($MetaEditor) { $args6 += @("-MetaEditor", $MetaEditor) }
    if ($PastaMql4) { $args6 += @("-PastaMql4", $PastaMql4) }
    Passo "6. compilar o EA no MetaEditor" { powershell @args6 } "06-compilar-ea.log"
} else {
    Write-Host "6. compilar o EA: pulado (plataforma mt5)"
}

# --- 7. aceitacao -----------------------------------------------------------------------------------------------------
if ($ConfirmoContaDemo) {
    if (-not $Login -or -not $Servidor) {
        Write-Host "AVISO: -Login e -Servidor nao informados; se config\robowin.toml [mt5] tambem nao os tiver, a aceitacao vai recusar (guarda de conta)." -ForegroundColor Yellow
    }
    $args7 = @("run", "robowin", "aceitacao", "--confirmo-conta-demo", "--plataforma", $Plataforma,
               "--minutos-coleta", "$MinutosColeta", "--repeticoes", "$Repeticoes", "--saida", (Join-Path $pasta "aceitacao"))
    if ($Login) { $args7 += @("--login", $Login) }
    if ($Servidor) { $args7 += @("--servidor", $Servidor) }
    if ($Ativo) { $args7 += @("--ativo", $Ativo) }
    Passo "7. robowin aceitacao --confirmo-conta-demo" { uv @args7 } "07-aceitacao.txt"
} else {
    Write-Host ""
    Write-Host "7. aceitacao: pulada (nenhuma ordem). Para rodar: acrescente -ConfirmoContaDemo -Login <login> -Servidor <servidor DEMO>." -ForegroundColor Yellow
}

# --- pendencias do usuario (so leitura; nao conta como falha) ---------------------------------------------------------------
# Decisoes que SO voce pode tomar: aprovacao da politica de risco, perfis a confirmar e chaves de API ausentes,
# cada uma com o comando exato. O texto vai para o zip (09-pendencias.txt) e e repetido no fim do script.
$pendencias = @(uv run robowin pendencias 2>&1 | ForEach-Object { "$_" })
$pendencias | Out-File -Encoding utf8 -FilePath (Join-Path $pasta "09-pendencias.txt")

# --- 8. empacotar SEM segredos ------------------------------------------------------------------------------------------
Write-Host ""
Write-Host "=== 8. empacotar relatorios (sem segredos) ===" -ForegroundColor Cyan
$segredos = @()
foreach ($v in @("ROBOWIN_MT5_SENHA", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "ROBOWIN_TOKEN",
                 "ROBOWIN_CMD_TOKEN", "ROBOWIN_MT4_TOKEN")) {
    $val = [Environment]::GetEnvironmentVariable($v)
    if ($val -and $val.Length -ge 6) { $segredos += $val }
}
$padroes = @("sk-[A-Za-z0-9_\-]{8,}", "AIza[0-9A-Za-z_\-]{10,}", "(?i)bearer\s+[A-Za-z0-9._\-]{8,}",
             "(?i)(api[_-]?key|x-api-key|authorization|password|senha)\s*[=:]\s*\S{8,}")
$permitidas = @(".json", ".md", ".txt", ".log")
$nomesProibidos = @(".env", "*.env", "*.key", "*.pem", "*.pfx", "*senha*", "*secret*", "*token*")
$stage = Join-Path ([System.IO.Path]::GetTempPath()) ("robowin-zip-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$excluidos = New-Object System.Collections.Generic.List[string]
$manifesto = New-Object System.Collections.Generic.List[string]
Get-ChildItem -LiteralPath $pasta -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($pasta.Length).TrimStart("\")
    $motivo = ""
    if ($permitidas -notcontains $_.Extension.ToLower()) { $motivo = "extensao nao permitida" }
    foreach ($n in $nomesProibidos) { if ($_.Name -like $n) { $motivo = "nome sugere segredo" } }
    if (-not $motivo) {
        $conteudo = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
        if ($conteudo) {
            foreach ($s in $segredos) { if ($conteudo.Contains($s)) { $motivo = "contem o valor de uma variavel secreta" } }
            foreach ($p in $padroes) { if ($conteudo -match $p) { $motivo = "contem padrao de chave/senha ($p)" } }
        }
    }
    if ($motivo) {
        $excluidos.Add("$rel -> $motivo")
    } else {
        $dest = Join-Path $stage $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $dest
        $manifesto.Add("$((Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash)  $rel")
    }
}
$linhas = @("MANIFESTO do pacote de aceitacao do robowin", "Gerado em: $(Get-Date -Format s)", "Plataforma: $Plataforma", "")
$linhas += "Passos:"
$resumo | ForEach-Object { $linhas += "  $($_.Passo): $($_.Resultado)" }
$linhas += ""
$linhas += "Arquivos (sha256):"
$linhas += $manifesto
if ($excluidos.Count -gt 0) {
    $linhas += ""
    $linhas += "EXCLUIDOS do pacote por seguranca:"
    $linhas += $excluidos
}
$linhas | Out-File -Encoding utf8 -FilePath (Join-Path $stage "MANIFESTO.txt")
$carimbo = Get-Date -Format "yyyyMMdd-HHmmss"
$zip = Join-Path $raiz "robowin-aceitacao-$carimbo.zip"
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -Force
Remove-Item -LiteralPath $stage -Recurse -Force

# --- resumo --------------------------------------------------------------------------------------------------------------
Write-Host ""
Write-Host "================ RESUMO ================" -ForegroundColor Cyan
$resumo | Format-Table -AutoSize | Out-Host
if ($excluidos.Count -gt 0) {
    Write-Host "Arquivos EXCLUIDOS do zip por parecerem conter segredo:" -ForegroundColor Yellow
    $excluidos | ForEach-Object { Write-Host "  $_" }
}
Write-Host "Pacote para me enviar: $zip" -ForegroundColor Green
Write-Host "O zip contem so .json/.md/.txt/.log desta pasta ($Saida). NUNCA contem .env, chaves, senha nem o login completo."
Write-Host "Antes de enviar, abra o MANIFESTO.txt dentro do zip e confira a lista de arquivos."

Write-Host ""
Write-Host "================ O QUE FALTA (so voce pode fazer) ================" -ForegroundColor Cyan
if ($pendencias.Count -gt 0) {
    $pendencias | ForEach-Object { Write-Host $_ }
} else {
    Write-Host "Nao foi possivel listar as pendencias; rode: uv run robowin pendencias" -ForegroundColor Yellow
}
Write-Host "Aprovacao de risco:   uv run robowin risco aprovar --responsavel ""<seu nome>"" --motivo ""<motivo>""  (digite APROVO)"
Write-Host "Perfis (estado):      uv run robowin perfil listar"
Write-Host "Confirmar um perfil:  uv run robowin perfil confirmar <ATIVO|bolsa:CODIGO> --responsavel ""<seu nome>"" --fonte ""<fonte>"" --comparar-terminal  (digite o simbolo)"
Write-Host "Chave de API:         setx ANTHROPIC_API_KEY ""<sua chave>""  /  setx OPENAI_API_KEY ""<sua chave>""  (abra um novo terminal depois)"

$falhas = @($resumo | Where-Object { $_.Resultado -ne "OK" })
if ($falhas.Count -gt 0) {
    Write-Host "$($falhas.Count) passo(s) FALHARAM: envie o zip mesmo assim (os logs dizem o que houve)." -ForegroundColor Red
    exit 1
}
exit 0
