param(
    [string]$ContainerName = $env:VLLM_CONTAINER_NAME,
    [int]$SampleSeconds = 20,
    [int]$IntervalSeconds = 60,
    [switch]$Restart,
    [switch]$StopWorkers,
    [switch]$RestartWorkers,
    [switch]$EnsureWorkersWhenHealthy,
    [int]$WorkerCount = 1,
    [int]$WorkerConcurrency = 2,
    [int]$StartupGraceSeconds = 600,
    [string]$LogPath = $(Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "runtime_logs\vllm-watchdog.log")
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WedgedCheck = Join-Path $ScriptDir "restart-vllm-if-wedged.ps1"
$EnsureWorkersScript = Join-Path $ScriptDir "ensure-agent-workers.ps1"

if (-not (Test-Path -LiteralPath $WedgedCheck)) {
    throw "Wedged-check script not found: $WedgedCheck"
}

if ($LogPath) {
    $logDir = Split-Path -Parent $LogPath
    if ($logDir -and -not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }
    try {
        Start-Transcript -Path $LogPath -Append | Out-Null
    } catch {
        Write-Warning "Could not start watchdog transcript at ${LogPath}: $($_.Exception.Message)"
    }
}

Write-Host "Timeline vLLM watchdog" -ForegroundColor Yellow
Write-Host "Container: $ContainerName"
Write-Host "Sample seconds: $SampleSeconds"
Write-Host "Interval seconds: $IntervalSeconds"
Write-Host "Restart enabled: $Restart"
Write-Host "Stop workers before restart: $StopWorkers"
Write-Host "Restart workers after recovery: $RestartWorkers"
Write-Host "Ensure workers when healthy: $EnsureWorkersWhenHealthy"
Write-Host "Worker count: $WorkerCount"
Write-Host "Worker concurrency: $WorkerConcurrency"
Write-Host "Startup grace seconds: $StartupGraceSeconds"
Write-Host ""

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$timestamp] Checking vLLM..."

    $argsList = @(
        "-NoProfile",
        "-ExecutionPolicy", "BYPASS",
        "-File", $WedgedCheck,
        "-ContainerName", $ContainerName,
        "-SampleSeconds", $SampleSeconds,
        "-WorkerCount", $WorkerCount,
        "-WorkerConcurrency", $WorkerConcurrency,
        "-StartupGraceSeconds", $StartupGraceSeconds
    )
    if ($Restart) {
        $argsList += "-Restart"
    }
    if ($StopWorkers) {
        $argsList += "-StopWorkers"
    }
    if ($RestartWorkers) {
        $argsList += "-RestartWorkers"
    }

    & powershell.exe @argsList
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        Write-Host "[$timestamp] vLLM healthy." -ForegroundColor Green
        if ($EnsureWorkersWhenHealthy) {
            if (-not (Test-Path -LiteralPath $EnsureWorkersScript)) {
                Write-Warning "[$timestamp] Agent worker supervisor not found: $EnsureWorkersScript"
            } else {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $EnsureWorkersScript `
                    -WorkerCount $WorkerCount `
                    -WorkerConcurrency $WorkerConcurrency `
                    -Lane llm
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "[$timestamp] Agent worker supervisor exited with code $LASTEXITCODE."
                }
            }
        }
    } elseif ($exitCode -eq 10) {
        Write-Host "[$timestamp] vLLM is warming up; leaving workers unchanged." -ForegroundColor Yellow
    } elseif ($exitCode -eq 3) {
        Write-Warning "[$timestamp] vLLM looked wedged. Restart flag was not enabled."
    } else {
        Write-Warning "[$timestamp] vLLM health check exited with code $exitCode."
    }

    Start-Sleep -Seconds ([Math]::Max(5, $IntervalSeconds))
}
