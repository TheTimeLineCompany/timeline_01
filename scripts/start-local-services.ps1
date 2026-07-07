$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Cleanup = Join-Path $ScriptDir "cleanup-local-processes.ps1"
$EnsureWorkers = Join-Path $ScriptDir "ensure-agent-workers.ps1"
$WorkerWatchdog = Join-Path $ScriptDir "watch-agent-workers.ps1"
$VllmWatchdog = Join-Path $ScriptDir "watch-vllm-health.ps1"
$ResetAgentLocks = Join-Path $Backend "scripts\reset_agent_locks.py"
$PauseInsightJobs = Join-Path $Backend "scripts\pause_insight_jobs.py"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"

function Read-DotEnvValue {
    param(
        [string]$Path,
        [string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $line = Get-Content -LiteralPath $Path |
        Where-Object { $_ -match "^\s*$([regex]::Escape($Name))\s*=" } |
        Select-Object -First 1
    if (-not $line) {
        return $null
    }
    $value = ($line -split "=", 2)[1].Trim()
    return $value.Trim('"').Trim("'")
}

$BackendEnvPath = Join-Path $Backend ".env"
$LlmApiKey = if ($env:LLM_API_KEY) { $env:LLM_API_KEY } else { Read-DotEnvValue -Path $BackendEnvPath -Name "LLM_API_KEY" }
$LlmBaseUrlValue = if ($env:LLM_BASE_URL) { $env:LLM_BASE_URL } else { Read-DotEnvValue -Path $BackendEnvPath -Name "LLM_BASE_URL" }
$LlmBaseUrl = if ($LlmBaseUrlValue) { $LlmBaseUrlValue.TrimEnd("/") } else { "http://127.0.0.1:8101/v1" }
$VllmUrl = "$LlmBaseUrl/models"
$VllmHeaders = @{}
if ($LlmApiKey) {
    $VllmHeaders.Authorization = "Bearer $LlmApiKey"
}

$AgentWorkerCount = 1
$AgentWorkerConcurrency = 2
$LlmWorkerCount = 1
$LlmWorkerConcurrency = 3
$CoreMode = $false
if ($env:TIMELINE_V4_CORE_MODE -eq "1" -or $env:TIMELINE_V4_CORE_MODE -eq "true") {
    $CoreMode = $true
}

if ($env:TIMELINE_V4_AGENT_WORKERS) {
    $parsedWorkerCount = 0
    if ([int]::TryParse($env:TIMELINE_V4_AGENT_WORKERS, [ref]$parsedWorkerCount) -and $parsedWorkerCount -gt 0) {
        $AgentWorkerCount = [Math]::Min($parsedWorkerCount, 8)
    }
}
if ($env:TIMELINE_V4_WORKER_CONCURRENCY) {
    $parsedConcurrency = 0
    if ([int]::TryParse($env:TIMELINE_V4_WORKER_CONCURRENCY, [ref]$parsedConcurrency) -and $parsedConcurrency -gt 0) {
        $AgentWorkerConcurrency = [Math]::Min($parsedConcurrency, 8)
    }
}
if ($env:TIMELINE_V4_LLM_WORKERS) {
    $parsedLlmWorkerCount = 0
    if ([int]::TryParse($env:TIMELINE_V4_LLM_WORKERS, [ref]$parsedLlmWorkerCount) -and $parsedLlmWorkerCount -gt 0) {
        $LlmWorkerCount = [Math]::Min($parsedLlmWorkerCount, 4)
    }
}
if ($env:TIMELINE_V4_LLM_WORKER_CONCURRENCY) {
    $parsedLlmConcurrency = 0
    if ([int]::TryParse($env:TIMELINE_V4_LLM_WORKER_CONCURRENCY, [ref]$parsedLlmConcurrency) -and $parsedLlmConcurrency -gt 0) {
        $LlmWorkerConcurrency = [Math]::Min($parsedLlmConcurrency, 4)
    }
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Cyan
}

function Wait-Http {
    param(
        [string]$Url,
        [hashtable]$Headers = @{},
        [int]$Attempts = 60,
        [int]$DelaySeconds = 2
    )

    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            Invoke-RestMethod -Uri $Url -Headers $Headers -TimeoutSec 4 | Out-Null
            return $true
        } catch {
            if (($i % 10) -eq 0) {
                Write-Host "  waiting for $Url ($i/$Attempts)..."
            }
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    return $false
}

function Wait-Tcp {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$Attempts = 40,
        [int]$DelayMilliseconds = 500
    )

    for ($i = 1; $i -le $Attempts; $i++) {
        $client = $null
        try {
            $client = [System.Net.Sockets.TcpClient]::new()
            $async = $client.BeginConnect($HostName, $Port, $null, $null)
            if ($async.AsyncWaitHandle.WaitOne($DelayMilliseconds) -and $client.Connected) {
                $client.EndConnect($async)
                return $true
            }
        } catch {
            # Retry until the service comes up.
        } finally {
            if ($client) {
                $client.Close()
            }
        }
        Start-Sleep -Milliseconds $DelayMilliseconds
    }
    return $false
}

Write-Host "Timeline startup" -ForegroundColor Green
Write-Host "Root: $Root"

Write-Step "[0/7] Killing stale local app processes"
& $Cleanup
if (Test-Path -LiteralPath $Python) {
    & $Python $ResetAgentLocks
    if (Test-Path -LiteralPath $PauseInsightJobs) {
        if ($CoreMode) {
            & $Python $PauseInsightJobs pause
        } else {
            & $Python $PauseInsightJobs resume
        }
    }
}

Write-Step "[1/7] Checking model-serving mode"
if ($CoreMode) {
    Write-Host "Core mode is active. Skipping vLLM health wait."
    Write-Host "Unset TIMELINE_V4_CORE_MODE, or set it to 0, to run the LLM insight stack."
} else {
    Write-Host "Insight mode is active. Expecting an OpenAI-compatible vLLM endpoint at $LlmBaseUrl."
    if (-not (Wait-Http -Url $VllmUrl -Headers $VllmHeaders -Attempts 60 -DelaySeconds 2)) {
        throw "vLLM did not respond at $VllmUrl. Start your model server or set TIMELINE_V4_CORE_MODE=1."
    }
    Write-Host "vLLM endpoint is reachable."
}

Write-Step "[2/7] Checking Neo4j bolt port"
if (Wait-Tcp -HostName "127.0.0.1" -Port 7687 -Attempts 4 -DelayMilliseconds 500) {
    Write-Host "Neo4j bolt port is reachable."
} else {
    Write-Warning "Neo4j bolt port 127.0.0.1:7687 is not reachable. Backend will start, but graph calls may fail."
}

Write-Step "[3/7] Starting backend API"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Backend virtualenv Python not found. Expected: $Python"
}

$env:TIMELINE_V4_LLM_LANE_ENABLED = if ($CoreMode) { "0" } else { "1" }

Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/k", "cd /d `"$Backend`" && .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000" `
    -WorkingDirectory $Backend `
    -WindowStyle Normal

if (-not (Wait-Http -Url "http://127.0.0.1:8000/health" -Attempts 60 -DelaySeconds 1)) {
    throw "Backend did not become healthy at http://127.0.0.1:8000/health"
}
Write-Host "Backend is healthy."

Write-Step "[4/7] Starting workers"
if (-not (Test-Path -LiteralPath $EnsureWorkers)) {
    throw "Agent worker supervisor not found: $EnsureWorkers"
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $EnsureWorkers `
    -WorkerCount $AgentWorkerCount `
    -WorkerConcurrency $AgentWorkerConcurrency `
    -Lane cpu
if ($LASTEXITCODE -ne 0) {
    throw "CPU worker supervisor failed with exit code $LASTEXITCODE"
}
if (-not $CoreMode) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $EnsureWorkers `
        -WorkerCount $LlmWorkerCount `
        -WorkerConcurrency $LlmWorkerConcurrency `
        -Lane llm
    if ($LASTEXITCODE -ne 0) {
        throw "LLM worker supervisor failed with exit code $LASTEXITCODE"
    }
}

Write-Step "[5/7] Starting worker watchdog"
$watchdogCoreArg = if ($CoreMode) { " -CoreOnly" } else { "" }
Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/k", "title Timeline Worker Watchdog && powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$WorkerWatchdog`"$watchdogCoreArg -CpuWorkerCount $AgentWorkerCount -CpuWorkerConcurrency $AgentWorkerConcurrency -LlmWorkerCount $LlmWorkerCount -LlmWorkerConcurrency $LlmWorkerConcurrency" `
    -WorkingDirectory $Root `
    -WindowStyle Hidden

if (-not $CoreMode -and $env:TIMELINE_V4_VLLM_WATCHDOG -eq "1") {
    Write-Host "Starting vLLM watchdog."
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/k", "title Timeline vLLM Watchdog && powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$VllmWatchdog`" -Restart -StopWorkers -RestartWorkers -EnsureWorkersWhenHealthy -WorkerCount $LlmWorkerCount -WorkerConcurrency $LlmWorkerConcurrency" `
        -WorkingDirectory $Root `
        -WindowStyle Normal
}

Write-Step "[6/7] Starting frontend"
if (-not (Test-Path -LiteralPath (Join-Path $Frontend "package.json"))) {
    throw "Frontend package.json not found: $(Join-Path $Frontend "package.json")"
}

Start-Process -FilePath "cmd.exe" `
    -ArgumentList "/k", "cd /d `"$Frontend`" && npm.cmd run dev -- --host 127.0.0.1 --port 5174" `
    -WorkingDirectory $Frontend `
    -WindowStyle Normal

if (-not (Wait-Tcp -HostName "127.0.0.1" -Port 5174 -Attempts 40 -DelayMilliseconds 500)) {
    throw "Frontend did not open port 5174"
}

Write-Step "[7/7] Ready"
Write-Host "Timeline is running." -ForegroundColor Green
Write-Host "Mode:      $(if ($CoreMode) { 'Core CPU worker only' } else { 'Split CPU + LLM workers' })"
Write-Host "Frontend: http://127.0.0.1:5174"
Write-Host "Backend:  http://127.0.0.1:8000"
Write-Host "vLLM:     $(if ($CoreMode) { 'not required in core mode' } else { $LlmBaseUrl })"
