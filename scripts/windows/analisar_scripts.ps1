<#
.SYNOPSIS
  Analisa todos os scripts PowerShell de scripts\windows: sintaxe (parser oficial), ASCII puro e PSScriptAnalyzer.

.DESCRIPTION
  Para cada scripts\windows\*.ps1:
    1. [System.Management.Automation.Language.Parser]::ParseFile  -> qualquer erro de sintaxe FALHA;
    2. bytes fora de ASCII                                          -> FALHA (PowerShell 5.1 le arquivo sem BOM como ANSI);
    3. Invoke-ScriptAnalyzer (se o modulo PSScriptAnalyzer existir) -> achado de severidade Error (ou ParseError) FALHA;
       Warning/Information so sao listados.
  Sai com 0 se tudo passou, 1 se algo falhou. Roda no Windows PowerShell 5.1 e no PowerShell 7 (pwsh).
  Usado pela CI (job windows-scripts); voce tambem pode rodar no seu Windows antes de usar os outros scripts.

  *** AVISO: escrito SEM PODER SER EXECUTADO no ambiente de desenvolvimento (Linux); quem executa de verdade e a CI
  do GitHub (windows-latest). ***

.PARAMETER Pasta
  Pasta com os .ps1 (padrao: a pasta deste script).
.PARAMETER InstalarAnalisador
  Se o PSScriptAnalyzer nao estiver instalado, tenta Install-Module -Scope CurrentUser (precisa de internet).
.PARAMETER ExigirAnalisador
  Falha se o PSScriptAnalyzer nao estiver disponivel (sem esta flag, a ausencia so e avisada).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\analisar_scripts.ps1
.EXAMPLE
  pwsh -File scripts\windows\analisar_scripts.ps1 -InstalarAnalisador -ExigirAnalisador
#>
param(
    [string]$Pasta = "",
    [switch]$InstalarAnalisador,
    [switch]$ExigirAnalisador
)

$ErrorActionPreference = "Stop"
if (-not $Pasta) { $Pasta = $PSScriptRoot }
Write-Host "PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition)); pasta: $Pasta"

$arquivos = @(Get-ChildItem -LiteralPath $Pasta -Filter "*.ps1" -File | Sort-Object Name)
if ($arquivos.Count -eq 0) {
    Write-Host "FALHA: nenhum .ps1 em $Pasta" -ForegroundColor Red
    exit 1
}

$falhas = New-Object System.Collections.Generic.List[string]

# --- 1 e 2: parser oficial e ASCII ------------------------------------------------------------------------------
foreach ($a in $arquivos) {
    $tokens = $null
    $erros = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($a.FullName, [ref]$tokens, [ref]$erros)
    if ($erros -and $erros.Count -gt 0) {
        foreach ($e in $erros) {
            $msg = "$($a.Name):$($e.Extent.StartLineNumber):$($e.Extent.StartColumnNumber): erro de sintaxe: $($e.Message)"
            $falhas.Add($msg)
            Write-Host "  $msg" -ForegroundColor Red
        }
    } else {
        Write-Host "  sintaxe OK: $($a.Name) ($($tokens.Count) tokens)"
    }
    $bytes = [System.IO.File]::ReadAllBytes($a.FullName)
    $linha = 1
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        if ($bytes[$i] -eq 10) { $linha++ }
        if ($bytes[$i] -gt 127) {
            $msg = "$($a.Name):$($linha): byte nao ASCII (0x$('{0:X2}' -f $bytes[$i])); use so ASCII nos .ps1"
            $falhas.Add($msg)
            Write-Host "  $msg" -ForegroundColor Red
            break
        }
    }
}

# --- 3: PSScriptAnalyzer ------------------------------------------------------------------------------------------
$temAnalisador = [bool](Get-Module -ListAvailable -Name PSScriptAnalyzer)
if (-not $temAnalisador -and $InstalarAnalisador) {
    Write-Host "PSScriptAnalyzer ausente; tentando Install-Module -Scope CurrentUser..."
    try {
        Install-Module -Name PSScriptAnalyzer -Scope CurrentUser -Force -AllowClobber -ErrorAction Stop
        $temAnalisador = [bool](Get-Module -ListAvailable -Name PSScriptAnalyzer)
    } catch {
        Write-Host "AVISO: nao foi possivel instalar o PSScriptAnalyzer: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
if ($temAnalisador) {
    Import-Module PSScriptAnalyzer
    $versao = (Get-Module PSScriptAnalyzer).Version
    Write-Host ""
    Write-Host "PSScriptAnalyzer $versao"
    foreach ($a in $arquivos) {
        $achados = @(Invoke-ScriptAnalyzer -Path $a.FullName -ErrorAction Continue)
        $graves = @($achados | Where-Object { "$($_.Severity)" -in @("Error", "ParseError") })
        $leves = @($achados | Where-Object { "$($_.Severity)" -notin @("Error", "ParseError") })
        Write-Host "  $($a.Name): $($graves.Count) erro(s), $($leves.Count) aviso(s)/informacao(oes)"
        foreach ($g in $graves) {
            $msg = "$($a.Name):$($g.Line): [$($g.Severity)] $($g.RuleName): $($g.Message)"
            $falhas.Add($msg)
            Write-Host "    $msg" -ForegroundColor Red
        }
        foreach ($l in $leves) {
            Write-Host "    $($a.Name):$($l.Line): [$($l.Severity)] $($l.RuleName): $($l.Message)" -ForegroundColor DarkYellow
        }
    }
} elseif ($ExigirAnalisador) {
    $falhas.Add("PSScriptAnalyzer indisponivel e -ExigirAnalisador foi pedido")
} else {
    Write-Host "AVISO: PSScriptAnalyzer nao instalado; so a analise sintatica foi feita." -ForegroundColor Yellow
}

Write-Host ""
if ($falhas.Count -gt 0) {
    Write-Host "FALHOU: $($falhas.Count) problema(s) em $($arquivos.Count) script(s):" -ForegroundColor Red
    $falhas | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    exit 1
}
Write-Host "OK: $($arquivos.Count) script(s) sem erro de sintaxe$(if ($temAnalisador) { ' nem erro do PSScriptAnalyzer' })." -ForegroundColor Green
exit 0
