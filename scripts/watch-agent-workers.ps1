param(
    [string]$Root = $(Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [switch]$CoreOnly,
    [int]$CpuWorkerCount = 1,
    [int]$CpuWorkerConcurrency = 2,
    [int]$LlmWorkerCount = 1,
    [int]$LlmWorkerConcurrency = 3,
    [int]$IntervalSeconds = 30,
    [string]$LogPath = $(Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "runtime_logs\agent-worker-watchdog.log")
)

$ErrorActionPreference = "Continue"

$EnsureWorkers = Join-Path $Root "scripts\ensure-agent-workers.ps1"

if (-not (Test-Path -LiteralPath $EnsureWorkers)) {
    throw "Agent worker supervisor not found: $EnsureWorkers"
}

if ($LogPath) {
    $logDir = Split-Path -Parent $LogPath
    if ($logDir -and -not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }
    try {
        Start-Transcript -Path $LogPath -Append | Out-Null
    } catch {
        Write-Warning "Could not start worker watchdog transcript at ${LogPath}: $($_.Exception.Message)"
    }
}

Write-Host "Timeline agent worker watchdog" -ForegroundColor Yellow
Write-Host "Core only: $CoreOnly"
Write-Host "CPU workers: $CpuWorkerCount, concurrency: $CpuWorkerConcurrency"
Write-Host "LLM workers: $LlmWorkerCount, concurrency: $LlmWorkerConcurrency"
Write-Host "Interval seconds: $IntervalSeconds"
Write-Host ""

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$timestamp] Ensuring CPU worker lane..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $EnsureWorkers `
        -WorkerCount $CpuWorkerCount `
        -WorkerConcurrency $CpuWorkerConcurrency `
        -Lane cpu
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[$timestamp] CPU worker supervisor exited with code $LASTEXITCODE."
    }

    if (-not $CoreOnly) {
        Write-Host "[$timestamp] Ensuring LLM worker lane..."
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $EnsureWorkers `
            -WorkerCount $LlmWorkerCount `
            -WorkerConcurrency $LlmWorkerConcurrency `
            -Lane llm
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "[$timestamp] LLM worker supervisor exited with code $LASTEXITCODE."
        }
    }

    Start-Sleep -Seconds ([Math]::Max(5, $IntervalSeconds))
}
