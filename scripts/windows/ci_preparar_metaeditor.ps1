<#
.SYNOPSIS
  (CI) Instala o MetaTrader oficial, localiza o MetaEditor e monta uma pasta MQL4 com a biblioteca mql-zmq, para o
  job `mql` da CI compilar mql4\RobowinBridge.mq4 com scripts\windows\compilar_ea.ps1.

.DESCRIPTION
  Feito para o runner windows-latest do GitHub Actions (descartavel). NAO rode na sua maquina: ele instala o
  MetaTrader em modo silencioso e encerra qualquer terminal/MetaEditor aberto. No seu Windows use compilar_ea.ps1
  direto, apontando para a pasta de dados do SEU terminal.

  Passos:
    1. MT4 (melhor esforco): baixa mt4setup.exe da MetaQuotes e instala com /auto; procura metaeditor.exe.
       A MetaQuotes deixou de distribuir o MT4; o instalador pode falhar ou instalar o MT5. Tudo e registrado.
    2. MT5: baixa mt5setup.exe (download.mql5.com) e instala com /auto; procura metaeditor64.exe.
    3. Escolhe o MetaEditor: o do MT4 (metaeditor.exe) se existir; senao o do MT5 (metaeditor64.exe), que PODE NAO
       compilar .mq4 -- nesse caso o resultado e registrado como vier, sem fingir sucesso.
    4. Baixa os headers (fontes, Apache-2.0) de dingmaotu/mql-zmq e dingmaotu/mql4-lib em commits fixos e monta
       <Trabalho>\MQL4\Include\{Zmq,Mql} e MQL4\Libraries (DLLs x86 do mql-zmq). Nada disso vai para o repositorio.
    5. stdlib.mqh: usa o do MT4 instalado, se houver; senao grava um STUB minimo (so ErrorDescription, importado de
       stdlib.ex4 como no original) e registra isso no relatorio.
  Saidas (GITHUB_OUTPUT, se existir): metaeditor, plataforma (mt4|mt5|nenhuma), pasta_mql4, stdlib (real|stub), logs.
  Codigo de saida: 0 com MetaEditor encontrado; 2 sem MetaEditor (instaladores indisponiveis ou mudaram).

  *** AVISO: escrito SEM PODER SER EXECUTADO no ambiente de desenvolvimento (Linux, sem acesso aos hosts da
  MetaQuotes); so a CI o executa. ***

.PARAMETER Trabalho
  Pasta de trabalho (padrao: $env:RUNNER_TEMP\mql, ou %TEMP%\robowin-mql fora da CI).
.PARAMETER TempoLimite
  Segundos de espera por instalador (padrao 900).
.PARAMETER PularMt4
  Nao tenta o instalador do MT4.
#>
param(
    [string]$Trabalho = "",
    [int]$TempoLimite = 900,
    [switch]$PularMt4
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # a barra de progresso deixa o Invoke-WebRequest muito lento no 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$UrlMt4 = "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt4/mt4setup.exe"
$UrlMt5 = "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
$ShaZmq = "802c86c9e3647c917151ff724ffd15baa9bc152c"      # dingmaotu/mql-zmq (master em 28/10/2026)
$ShaMqlLib = "ec696fafbea4e50ab5967ba3a12cb61bb8056419"   # dingmaotu/mql4-lib (master em 28/10/2026)

if (-not $Trabalho) {
    $Trabalho = if ($env:RUNNER_TEMP) { Join-Path $env:RUNNER_TEMP "mql" } else { Join-Path $env:TEMP "robowin-mql" }
}
$pastaInst = Join-Path $Trabalho "instaladores"
$pastaTerceiros = Join-Path $Trabalho "terceiros"
$pastaLogs = Join-Path $Trabalho "logs"
$mql4 = Join-Path $Trabalho "MQL4"
foreach ($d in @($pastaInst, $pastaTerceiros, $pastaLogs, (Join-Path $mql4 "Include"), (Join-Path $mql4 "Experts"), (Join-Path $mql4 "Libraries"))) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
$relatorio = New-Object System.Collections.Generic.List[string]
function Registrar([string]$texto) {
    Write-Host $texto
    $script:relatorio.Add($texto)
}

function Baixar([string]$url, [string]$destino) {
    Write-Host "baixando $url"
    Invoke-WebRequest -Uri $url -OutFile $destino -UseBasicParsing
    $h = (Get-FileHash -LiteralPath $destino -Algorithm SHA256).Hash
    Registrar "  baixado: $(Split-Path -Leaf $destino) ($((Get-Item -LiteralPath $destino).Length) bytes, sha256 $h)"
}

function ProcurarMetaEditor([string]$nomeExe) {
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base -or -not (Test-Path -LiteralPath $base)) { continue }
        foreach ($dir in @(Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue)) {
            $c = Join-Path $dir.FullName $nomeExe
            if (Test-Path -LiteralPath $c) { return $c }
        }
    }
    return $null
}

function EncerrarMetaTrader {
    foreach ($n in @("terminal", "terminal64", "metaeditor", "metaeditor64", "liveupdate")) {
        Get-Process -Name $n -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host "  encerrando $($_.ProcessName) (pid $($_.Id))"
            try { $_.Kill() } catch { Write-Host "  AVISO: $($_.Exception.Message)" }
        }
    }
}

function InstalarMetaTrader([string]$rotulo, [string]$url, [string]$nomeExe) {
    Registrar ""
    Registrar "== $rotulo =="
    $ja = ProcurarMetaEditor $nomeExe
    if ($ja) {
        Registrar "  ja instalado: $ja"
        return $ja
    }
    $inst = Join-Path $pastaInst "$rotulo-setup.exe"
    try {
        Baixar $url $inst
    } catch {
        Registrar "  FALHOU o download: $($_.Exception.Message)"
        return $null
    }
    $vi = (Get-Item -LiteralPath $inst).VersionInfo
    Registrar "  instalador: $($vi.ProductName) $($vi.FileVersion) ($($vi.CompanyName))"
    $p = Start-Process -FilePath $inst -ArgumentList "/auto" -PassThru
    $null = $p.Handle
    if (-not $p.WaitForExit($TempoLimite * 1000)) {
        Registrar "  instalador nao terminou em $TempoLimite s; encerrando"
        try { $p.Kill() } catch { Write-Host "  AVISO: $($_.Exception.Message)" }
    } else {
        Registrar "  instalador terminou (codigo $($p.ExitCode))"
    }
    $achado = $null
    for ($i = 0; $i -lt 12 -and -not $achado; $i++) {
        $achado = ProcurarMetaEditor $nomeExe
        if (-not $achado) { Start-Sleep -Seconds 10 }
    }
    EncerrarMetaTrader
    if ($achado) {
        $ve = (Get-Item -LiteralPath $achado).VersionInfo
        Registrar "  $nomeExe encontrado: $achado (versao $($ve.FileVersion))"
    } else {
        Registrar "  $nomeExe NAO encontrado depois da instalacao"
    }
    return $achado
}

# --- 1/2. instaladores --------------------------------------------------------------------------------------------
$me4 = $null
if (-not $PularMt4) { $me4 = InstalarMetaTrader "mt4" $UrlMt4 "metaeditor.exe" }
$me5 = InstalarMetaTrader "mt5" $UrlMt5 "metaeditor64.exe"
if (-not $me4) { $me4 = ProcurarMetaEditor "metaeditor.exe" }   # o instalador do MT5 nao traz, mas confere

# --- 3. escolha ------------------------------------------------------------------------------------------------------
$plataforma = "nenhuma"
$metaeditor = ""
if ($me4) {
    $plataforma = "mt4"
    $metaeditor = $me4
} elseif ($me5) {
    $plataforma = "mt5"
    $metaeditor = $me5
}
Registrar ""
Registrar "MetaEditor escolhido: $(if ($metaeditor) { $metaeditor } else { '(nenhum)' }) (plataforma: $plataforma)"
if ($plataforma -eq "mt5") {
    Registrar "ATENCAO: so o MetaEditor do MT5 esta disponivel; ele pode nao compilar .mq4. O resultado sera registrado como vier."
}

# --- 4. headers de terceiros (so na CI; nao vao para o repositorio) --------------------------------------------------
Registrar ""
Registrar "== headers mql-zmq (Apache-2.0) e mql4-lib (Apache-2.0) =="
$zipZmq = Join-Path $pastaTerceiros "mql-zmq.zip"
$zipLib = Join-Path $pastaTerceiros "mql4-lib.zip"
Baixar "https://github.com/dingmaotu/mql-zmq/archive/$ShaZmq.zip" $zipZmq
Baixar "https://github.com/dingmaotu/mql4-lib/archive/$ShaMqlLib.zip" $zipLib
Expand-Archive -LiteralPath $zipZmq -DestinationPath $pastaTerceiros -Force
Expand-Archive -LiteralPath $zipLib -DestinationPath $pastaTerceiros -Force
$dirZmq = Join-Path $pastaTerceiros "mql-zmq-$ShaZmq"
$dirLib = Join-Path $pastaTerceiros "mql4-lib-$ShaMqlLib"
$incMql = Join-Path $mql4 "Include\Mql"
New-Item -ItemType Directory -Force -Path $incMql | Out-Null
# mql4-lib completo em Include\Mql; depois o Include do mql-zmq por cima (o subconjunto de Mql\Lang com que ele foi testado)
Copy-Item -Path (Join-Path $dirLib "*") -Destination $incMql -Recurse -Force
Copy-Item -Path (Join-Path $dirZmq "Include\*") -Destination (Join-Path $mql4 "Include") -Recurse -Force
Copy-Item -Path (Join-Path $dirZmq "Library\MT4\*.dll") -Destination (Join-Path $mql4 "Libraries") -Force
if (-not (Test-Path -LiteralPath (Join-Path $mql4 "Include\Zmq\Zmq.mqh"))) { throw "Include\Zmq\Zmq.mqh nao ficou no lugar" }
Registrar "  mql-zmq $ShaZmq e mql4-lib $ShaMqlLib em $mql4\Include"

# --- 5. stdlib.mqh ----------------------------------------------------------------------------------------------------
$stdlibDestino = Join-Path $mql4 "Include\stdlib.mqh"
$stdlibOrigem = $null
$ondeProcurar = @()
if ($me4) { $ondeProcurar += (Split-Path -Parent $me4) }
if ($env:APPDATA) { $ondeProcurar += (Join-Path $env:APPDATA "MetaQuotes\Terminal") }
foreach ($onde in $ondeProcurar) {
    if (-not (Test-Path -LiteralPath $onde)) { continue }
    $c = Get-ChildItem -LiteralPath $onde -Recurse -File -Filter "stdlib.mqh" -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\MQL4\\Include\\" } | Select-Object -First 1
    if ($c) {
        $stdlibOrigem = $c.FullName
        break
    }
}
$stdlib = "stub"
if ($stdlibOrigem) {
    Copy-Item -LiteralPath $stdlibOrigem -Destination $stdlibDestino -Force
    $stdlib = "real"
    Registrar "stdlib.mqh: o do MT4 instalado ($stdlibOrigem)"
} else {
    $stub = @(
        "// STUB gerado pela CI (scripts/windows/ci_preparar_metaeditor.ps1): o stdlib.mqh real vem com o MT4 e nao",
        "// estava disponivel neste runner. Declara so o que o RobowinBridge.mq4 usa, importado de stdlib.ex4 como no",
        "// original do MT4. NAO e o arquivo da MetaQuotes.",
        "#import ""stdlib.ex4""",
        "string ErrorDescription(int error_code);",
        "#import"
    )
    $stub | Set-Content -LiteralPath $stdlibDestino -Encoding ASCII
    Registrar "stdlib.mqh: STUB da CI (o MT4 nao estava disponivel); so declara ErrorDescription"
}

# --- saidas ------------------------------------------------------------------------------------------------------------
$relatorio | Set-Content -LiteralPath (Join-Path $pastaLogs "preparacao.txt") -Encoding UTF8
if ($env:GITHUB_OUTPUT) {
    # UTF-8 SEM BOM (um BOM no inicio estragaria o nome da primeira saida no Windows PowerShell 5.1)
    $saidas = "metaeditor=$metaeditor`nplataforma=$plataforma`npasta_mql4=$mql4`nstdlib=$stdlib`nlogs=$pastaLogs`n"
    [System.IO.File]::AppendAllText($env:GITHUB_OUTPUT, $saidas, (New-Object System.Text.UTF8Encoding($false)))
}
if (-not $metaeditor) {
    Write-Host "FALHA: nenhum MetaEditor encontrado (instaladores indisponiveis ou mudaram)." -ForegroundColor Red
    exit 2
}
exit 0
