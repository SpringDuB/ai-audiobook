<#
tools/restart.ps1 — 一键重启后端（serve：接口 + 浏览器界面）与 worker。

用法：
    powershell -ExecutionPolicy Bypass -File tools\restart.ps1
    powershell -ExecutionPolicy Bypass -File tools\restart.ps1 -WithTts    # 连 TTS 服务一起拉
    powershell -ExecutionPolicy Bypass -File tools\restart.ps1 -Stop       # 只停不启
    powershell -ExecutionPolicy Bypass -File tools\restart.ps1 -Port 8301  # 换端口

做四件事（幂等，跑几遍都一样）：
    1. 停掉旧的 serve / worker 整条进程链（uv → aiab.exe → python）
    2. 轮转日志 data/logs/{serve,worker}.{log,err.log} → *.before-restart-<时间戳>
    3. 隐藏窗口后台重启两个进程，stdout/stderr 接回 data/logs/
    4. 自检：serve 的 /api/session 能通、worker 进程在，打印 PID 与入口地址

默认不动 TTS（它由设置页「一键启动」管理，吃显存，未必每次都想要）。
#>
param(
    [int]$Port = 8300,
    [switch]$WithTts,
    [switch]$Stop,
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Set-Location $repo

function Get-AiabStack {
    <# serve / worker 的整条进程链：命令行里既有 aiab、又带 serve|worker 子命令的才认。 #>
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uv.exe' OR Name='aiab.exe'" |
        Where-Object {
            $_.CommandLine -and $_.CommandLine -match 'aiab' -and $_.CommandLine -match 'aiab(\.exe)?"?\s+(serve|worker)\b'
        }
}

function Get-ShortLine([string]$CommandLine) {
    if (-not $CommandLine) { return "" }
    $line = $CommandLine -replace '^"|"$', ''
    if ($line.Length -gt 96) { return $line.Substring(0, 93) + "..." }
    return $line
}

function Get-Innermost {
    <# 进程链最里面那个：没有任何兄弟进程把它当父进程的那只。
       uv → aiab.exe → python → python，真正跑循环 / 占端口的是最内层。 #>
    param($Processes)
    $parents = @{}
    foreach ($proc in $Processes) { $parents[[int]$proc.ParentProcessId] = $true }
    $leaves = @($Processes | Where-Object { -not $parents.ContainsKey([int]$_.ProcessId) })
    if (-not $leaves) { return $null }
    return ($leaves | Sort-Object CreationDate | Select-Object -Last 1)
}

function Stop-AiabStack {
    param([int]$Port)
    $targets = @(Get-AiabStack)
    # 端口占用者兜底：万一命令行不匹配（比如手工 uvicorn），只要它确实是 aiab 也收掉
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if ($owner -and $owner.CommandLine -match 'aiab' -and -not ($targets | Where-Object { $_.ProcessId -eq $owner.ProcessId })) {
            $targets += $owner
        }
    }
    if (-not $targets) {
        Write-Host "  · 没有在跑的 serve / worker"
        return
    }
    foreach ($proc in $targets) {
        Write-Host ("  · 停止 PID {0}  {1}" -f $proc.ProcessId, (Get-ShortLine $proc.CommandLine))
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    # 等端口释放（uvicorn 收尾要一两秒）
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Milliseconds 300
    }
    $stubborn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($stubborn) {
        Write-Warning "端口 $Port 仍被 PID $($stubborn.OwningProcess) 占用，再补一刀"
        Stop-Process -Id $stubborn.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
}

function Rotate-Log([string]$Path, [string]$Stamp) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    for ($i = 0; $i -lt 20; $i++) {
        try {
            Move-Item -LiteralPath $Path -Destination "$Path.before-restart-$Stamp" -ErrorAction Stop
            return
        } catch {
            Start-Sleep -Milliseconds 500   # 旧进程可能还在收尾，文件暂时被占用
        }
    }
    Write-Warning "日志轮转失败（文件仍被占用，这次先追加）：$Path"
}

function Start-AiabProcess {
    param(
        [string]$Kind,              # serve | worker
        [string[]]$ExtraArgs,
        [string]$StdOut,
        [string]$StdErr
    )
    # 与文档一致走 uv；装了 uv 但不在 PATH 时退回 venv 里的 aiab.exe
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        $exe = $uv.Source
        $argv = @("run", "aiab", $Kind) + $ExtraArgs
    } else {
        $exe = Join-Path $repo ".venv\Scripts\aiab.exe"
        $argv = @($Kind) + $ExtraArgs
    }
    Start-Process -FilePath $exe -ArgumentList $argv -WorkingDirectory $repo `
        -WindowStyle Hidden -RedirectStandardOutput $StdOut -RedirectStandardError $StdErr -PassThru
}

function Wait-Backend {
    param([int]$Port, [int]$TimeoutSeconds)
    # /api/session 是入口密码的公开接口：开不开密码都能免登录探活
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/session" -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            # 还没起来：继续等
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

Write-Host "重启 AI 有声书：serve(端口 $Port) + worker"
Write-Host "[1/4] 停掉旧的进程"
Stop-AiabStack -Port $Port

if ($Stop) {
    Write-Host "已停止（-Stop：不重新启动）"
    exit 0
}

Write-Host "[2/4] 轮转日志"
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
foreach ($name in 'serve.log', 'serve.err.log', 'worker.log', 'worker.err.log') {
    Rotate-Log -Path (Join-Path $logDir $name) -Stamp $stamp
}

Write-Host "[3/4] 后台启动"
$serveProc = Start-AiabProcess -Kind "serve" -ExtraArgs @("--port", "$Port") `
    -StdOut (Join-Path $logDir "serve.log") -StdErr (Join-Path $logDir "serve.err.log")
$workerProc = Start-AiabProcess -Kind "worker" -ExtraArgs @() `
    -StdOut (Join-Path $logDir "worker.log") -StdErr (Join-Path $logDir "worker.err.log")
Write-Host "  · serve  启动器 PID $($serveProc.Id)"
Write-Host "  · worker 启动器 PID $($workerProc.Id)"

if ($WithTts) {
    Write-Host "  · 顺手拉起 TTS 服务（模型加载要一会儿，健康检查稍后自己看设置页）"
    $tts = Get-Command uv -ErrorAction SilentlyContinue
    if ($tts) { & $tts.Source run aiab tts start } else { & (Join-Path $repo ".venv\Scripts\aiab.exe") tts start }
}

Write-Host "[4/4] 自检"
$healthy = Wait-Backend -Port $Port -TimeoutSeconds $TimeoutSeconds

$stack = @(Get-AiabStack)
$serves = @($stack | Where-Object { $_.CommandLine -match 'aiab(\.exe)?"?\s+serve\b' })
$workers = @($stack | Where-Object { $_.CommandLine -match 'aiab(\.exe)?"?\s+worker\b' })
$servePid = $null
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) { $servePid = [int]$listener.OwningProcess }
elseif ($serves) {
    $leaf = Get-Innermost $serves
    if ($leaf) { $servePid = [int]$leaf.ProcessId }
}

$workerPid = $null
if ($workers) {
    $leaf = Get-Innermost $workers
    if ($leaf) { $workerPid = [int]$leaf.ProcessId }
}

$gateOn = $false
$envFile = Join-Path $repo ".env"
if (Test-Path -LiteralPath $envFile) {
    $gateOn = [bool](Select-String -Path $envFile -Pattern '^\s*AB_UI_PASSWORD\s*=\s*\S' -Quiet)
}
if (-not $gateOn -and $env:AB_UI_PASSWORD) { $gateOn = $true }

Write-Host ""
if ($healthy) {
    Write-Host "✓ serve   http://127.0.0.1:$Port/  （PID $servePid）"
} else {
    Write-Host "✗ serve   等了 $TimeoutSeconds 秒还是没通，看 data/logs/serve.err.log 末尾："
    Get-Content (Join-Path $logDir "serve.err.log") -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
}
if ($workerPid) {
    Write-Host "✓ worker  PID $workerPid（最内层进程）"
} else {
    Write-Host "✗ worker 没找到进程，看 data/logs/worker.err.log"
}
if ($gateOn) {
    Write-Host "· 入口密码：已开启（打开页面先在 /login 输密码）"
} else {
    Write-Host "· 入口密码：未开启（打开页面直接进）"
}
Write-Host "· 日志：data/logs/serve.log · serve.err.log · worker.log · worker.err.log"

if (-not $healthy -or -not $workerPid) { exit 1 }
exit 0
