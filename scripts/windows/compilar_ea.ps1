<#
.SYNOPSIS
  Compila mql4/RobowinBridge.mq4 com o MetaEditor do MetaTrader 4, le o log e informa o .ex4 gerado.

.DESCRIPTION
  Usa a linha de comando do MetaEditor:  metaeditor.exe /compile:<arquivo.mq4> /log:<arquivo.log>
  Falha (codigo de saida 1) se o log tiver erros ou se o .ex4 nao for gerado; codigo 2 se faltar pre-requisito.

  *** AVISO: este script foi escrito SEM PODER SER EXECUTADO no ambiente de desenvolvimento (Linux, sem Windows e sem
  MetaEditor). Ele NAO foi testado. Se algo falhar aqui (caminho do MetaEditor, codificacao do log, include da
  biblioteca mql-zmq), a causa mais provavel e o script, nao o EA: compile tambem pelo MetaEditor (F7) e me envie o log. ***

  O EA inclui <Zmq/Zmq.mqh> (biblioteca mql-zmq + libzmq.dll). Sem ela o MetaEditor reporta erro de include, e este
  script mostra essa mensagem. Instale a biblioteca na pasta de dados do terminal (MQL4\Include\Zmq e MQL4\Libraries).

.PARAMETER MetaEditor
  Caminho do metaeditor.exe do MT4. Se omitido: variavel ROBOWIN_METAEDITOR e locais padrao do Windows
  (Program Files\*MetaTrader 4*, Program Files (x86), C:\MT4, C:\MetaTrader 4, Terminal portatil...).

.PARAMETER PastaMql4
  Pasta MQL4 da pasta de dados do terminal (File > Open Data Folder > MQL4). Se informada, o .mq4 e copiado para
  <PastaMql4>\Experts e compilado la (assim os includes e o .ex4 ficam no lugar certo). Se omitida, o script procura
  em %APPDATA%\MetaQuotes\Terminal\*\MQL4 uma pasta que tenha Include\Zmq\Zmq.mqh.

.PARAMETER Fonte
  Arquivo .mq4 (padrao: mql4\RobowinBridge.mq4 do repositorio).

.PARAMETER Log
  Arquivo de log do MetaEditor (padrao: %TEMP%\robowin-compilar-ea.log).

.PARAMETER TempoLimite
  Segundos de espera pelo MetaEditor (padrao 600). Se estourar, o processo e encerrado e o script falha (codigo 1).

.NOTES
  Com -PastaMql4 o MetaEditor recebe tambem /inc:<PastaMql4> (pasta onde ficam Include\Zmq e Include\Mql).
  O metaeditor64.exe do MT5 PODE NAO compilar .mq4 (o compilador MQL4 e o do MT4); a CI (job mql) tenta mesmo assim
  quando so o MT5 esta disponivel e registra o resultado como ele vier, sem tratar isso como prova de nada.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\compilar_ea.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\compilar_ea.ps1 -MetaEditor "C:\MT4\metaeditor.exe" -PastaMql4 "C:\MT4\MQL4"
#>
param(
    [string]$MetaEditor = "",
    [string]$PastaMql4 = "",
    [string]$Fonte = "",
    [string]$Log = "",
    [int]$TempoLimite = 600
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Falhar([string]$msg, [int]$codigo) {
    Write-Host ""
    Write-Host "FALHA: $msg" -ForegroundColor Red
    exit $codigo
}

Write-Host "AVISO: compilar_ea.ps1 foi escrito sem poder ser executado no ambiente de desenvolvimento; confirme o resultado no MetaEditor (F7)." -ForegroundColor Yellow

# --- fonte -----------------------------------------------------------------------------------------------
if (-not $Fonte) { $Fonte = Join-Path $raiz "mql4\RobowinBridge.mq4" }
if (-not (Test-Path -LiteralPath $Fonte)) { Falhar "arquivo .mq4 nao encontrado: $Fonte" 2 }
$Fonte = (Resolve-Path -LiteralPath $Fonte).Path

# --- MetaEditor ------------------------------------------------------------------------------------------
if (-not $MetaEditor -and $env:ROBOWIN_METAEDITOR) { $MetaEditor = $env:ROBOWIN_METAEDITOR }
if (-not $MetaEditor) {
    $candidatos = New-Object System.Collections.Generic.List[string]
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, "C:\", "D:\")) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        foreach ($padrao in @("*MetaTrader 4*", "*MetaTrader4*", "MT4*", "*MT4*")) {
            Get-ChildItem -LiteralPath $base -Directory -Filter $padrao -ErrorAction SilentlyContinue | ForEach-Object {
                $c = Join-Path $_.FullName "metaeditor.exe"
                if (Test-Path -LiteralPath $c) { $candidatos.Add($c) }
            }
        }
    }
    # terminais da corretora instalados em pasta propria: <Program Files>\<Corretora> MetaTrader 4\metaeditor.exe
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $c = Join-Path $_.FullName "metaeditor.exe"
            if ((Test-Path -LiteralPath $c) -and (Test-Path -LiteralPath (Join-Path $_.FullName "terminal.exe"))) { $candidatos.Add($c) }
        }
    }
    $candidatos = @($candidatos | Select-Object -Unique)
    if ($candidatos.Count -eq 0) {
        Falhar "metaeditor.exe do MT4 nao encontrado nos locais padrao. Informe -MetaEditor <caminho> (ou defina ROBOWIN_METAEDITOR)." 2
    }
    if ($candidatos.Count -gt 1) {
        Write-Host "Varios MetaEditor encontrados; usando o primeiro. Use -MetaEditor para escolher:" -ForegroundColor Yellow
        $candidatos | ForEach-Object { Write-Host "  $_" }
    }
    $MetaEditor = $candidatos[0]
}
if (-not (Test-Path -LiteralPath $MetaEditor)) { Falhar "MetaEditor nao existe: $MetaEditor" 2 }
if ((Split-Path -Leaf $MetaEditor) -ieq "metaeditor64.exe") {
    Write-Host "AVISO: metaeditor64.exe e do MT5 e pode nao compilar .mq4; o resultado valido e o do metaeditor.exe do MT4." -ForegroundColor Yellow
}
Write-Host "MetaEditor: $MetaEditor"

# --- pasta de dados (includes e destino do .ex4) -----------------------------------------------------------
if (-not $PastaMql4) {
    $achadas = @()
    $raizTerminais = Join-Path $env:APPDATA "MetaQuotes\Terminal"
    if (Test-Path -LiteralPath $raizTerminais) {
        $achadas = @(Get-ChildItem -LiteralPath $raizTerminais -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $m = Join-Path $_.FullName "MQL4"
            if (Test-Path -LiteralPath (Join-Path $m "Include\Zmq\Zmq.mqh")) { $m }
        })
    }
    if ($achadas.Count -eq 1) {
        $PastaMql4 = $achadas[0]
    } elseif ($achadas.Count -gt 1) {
        Write-Host "Varias pastas MQL4 com a biblioteca Zmq; informe -PastaMql4:" -ForegroundColor Yellow
        $achadas | ForEach-Object { Write-Host "  $_" }
        Falhar "ambiguo: mais de uma pasta MQL4 com Include\Zmq\Zmq.mqh" 2
    }
}

$alvo = $Fonte
if ($PastaMql4) {
    if (-not (Test-Path -LiteralPath $PastaMql4)) { Falhar "-PastaMql4 nao existe: $PastaMql4" 2 }
    if (-not (Test-Path -LiteralPath (Join-Path $PastaMql4 "Include\Zmq\Zmq.mqh"))) {
        Falhar "biblioteca mql-zmq ausente: falta $PastaMql4\Include\Zmq\Zmq.mqh (e Libraries\libzmq.dll). O EA faz #include <Zmq/Zmq.mqh>." 2
    }
    $experts = Join-Path $PastaMql4 "Experts"
    New-Item -ItemType Directory -Force -Path $experts | Out-Null
    $alvo = Join-Path $experts "RobowinBridge.mq4"
    Copy-Item -LiteralPath $Fonte -Destination $alvo -Force
    Write-Host "Fonte copiado para: $alvo"
} else {
    Write-Host "AVISO: pasta MQL4 do terminal nao encontrada; compilando o arquivo no repositorio. Se o include <Zmq/Zmq.mqh> falhar, passe -PastaMql4." -ForegroundColor Yellow
}

# --- compila -----------------------------------------------------------------------------------------------
if (-not $Log) { $Log = Join-Path $env:TEMP "robowin-compilar-ea.log" }
if (Test-Path -LiteralPath $Log) { Remove-Item -LiteralPath $Log -Force }
$ex4 = [System.IO.Path]::ChangeExtension($alvo, ".ex4")
if (Test-Path -LiteralPath $ex4) { Remove-Item -LiteralPath $ex4 -Force }   # so um .ex4 NOVO conta como sucesso
$inicio = Get-Date

$argumentos = @("/compile:`"$alvo`"", "/log:`"$Log`"")
if ($PastaMql4) { $argumentos += "/inc:`"$PastaMql4`"" }
Write-Host "Executando: `"$MetaEditor`" $($argumentos -join ' ')"
$proc = Start-Process -FilePath $MetaEditor -ArgumentList $argumentos -PassThru
$null = $proc.Handle   # guarda o handle para o ExitCode ficar disponivel depois do WaitForExit
if (-not $proc.WaitForExit($TempoLimite * 1000)) {
    try { $proc.Kill() } catch { Write-Host "AVISO: nao consegui encerrar o MetaEditor: $($_.Exception.Message)" -ForegroundColor Yellow }
    Falhar "o MetaEditor nao terminou em $TempoLimite s (travou ou abriu janela esperando resposta)" 1
}
Write-Host "MetaEditor terminou (codigo $($proc.ExitCode); o codigo do MetaEditor nao e confiavel, vale o log)."

# --- le o log (o MetaEditor grava em UTF-16; tenta UTF-16 e, se nao houver resultado, o padrao) -----------------
if (-not (Test-Path -LiteralPath $Log)) { Falhar "o MetaEditor nao gerou o log: $Log (caminho do MetaEditor ou do .mq4 incorreto?)" 1 }
$texto = Get-Content -LiteralPath $Log -Encoding Unicode -Raw
if (-not $texto -or $texto -notmatch "(?i)\d+\s+errors?\b") { $texto = Get-Content -LiteralPath $Log -Raw }
if (-not $texto) { Falhar "log vazio: $Log" 1 }

Write-Host ""
Write-Host "---- log do MetaEditor ----"
Write-Host $texto
Write-Host "---------------------------"

# MT5: "Result: 0 errors, 1 warnings, 120 msec elapsed"; MT4 antigo: "0 error(s), 0 warning(s)"
$m = [regex]::Match($texto, "(?i)(\d+)\s+errors?(?:\(s\))?\s*,\s*(\d+)\s+warnings?")
if (-not $m.Success) { Falhar "nao achei a linha de resultado ('N errors, M warnings') no log: $Log" 1 }
$erros = [int]$m.Groups[1].Value
$avisos = [int]$m.Groups[2].Value
if ($erros -gt 0) {
    $linhas = ($texto -split "`r?`n") | Where-Object { $_ -match "(?i)\berror\b" -and $_ -notmatch "(?i)^\s*result" }
    Write-Host "ERROS de compilacao ($erros):" -ForegroundColor Red
    $linhas | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    if ($texto -match "(?i)Zmq") { Write-Host "Dica: o erro cita Zmq; confira a instalacao da biblioteca mql-zmq em MQL4\Include\Zmq." -ForegroundColor Yellow }
    Falhar "o EA tem $erros erro(s) de compilacao (veja o log acima; guarde-o para me enviar)." 1
}

if (-not (Test-Path -LiteralPath $ex4)) { Falhar "0 erros no log, mas o .ex4 nao foi gerado: $ex4" 1 }
$arq = Get-Item -LiteralPath $ex4
if ($arq.LastWriteTime -lt $inicio.AddSeconds(-2)) { Falhar "o .ex4 existente e antigo (nao foi regerado): $ex4" 1 }

Write-Host ""
Write-Host "COMPILADO: 0 erros, $avisos aviso(s)." -ForegroundColor Green
Write-Host "Arquivo gerado (.ex4): $($arq.FullName)  ($($arq.Length) bytes)"
Write-Host "sha256 do .ex4: $((Get-FileHash -LiteralPath $ex4 -Algorithm SHA256).Hash)"
Write-Host "Proximo passo: reinicie/atualize o Navegador do MT4, anexe o EA a UM grafico da conta DEMO e habilite Algo Trading."
exit 0
