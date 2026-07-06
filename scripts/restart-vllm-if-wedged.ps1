param(
    [string]$ContainerName = $env:VLLM_CONTAINER_NAME,
    [string]$MetricsUrl = "http://127.0.0.1:8101/metrics",
    [string]$ModelsUrl = "http://127.0.0.1:8101/v1/models",
    [string]$ApiKey = $env:LLM_API_KEY,
    [int]$ContainerPort = 8100,
    [int]$SampleSeconds = 30,
    [switch]$Restart,
    [switch]$StopWorkers,
    [switch]$RestartWorkers,
    [int]$WorkerCount = 1,
    [int]$WorkerConcurrency = 2,
    [int]$StartupGraceSeconds = 600
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Backend = Join-Path $Root "backend"
$ResetAgentLocks = Join-Path $Backend "scripts\reset_agent_locks.py"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$EnsureWorkers = Join-Path $Root "scripts\ensure-agent-workers.ps1"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Cyan
}

function Invoke-CurlText {
    param(
        [string]$Url,
        [string]$ApiKey
    )

    $args = @("-sS")
    if ($ApiKey) {
        $args += @("-H", "Authorization: Bearer $ApiKey")
    }
    $args += $Url
    $output = & curl.exe @args
    if ($LASTEXITCODE -ne 0) {
        throw "curl failed for $Url with exit code $LASTEXITCODE"
    }
    return ($output -join "`n")
}

function Invoke-DockerCurlText {
    param(
        [string]$Path,
        [string]$ApiKey
    )

    $url = "http://127.0.0.1:$ContainerPort$Path"
    if (-not $ContainerName) {
        throw "VLLM_CONTAINER_NAME is not set."
    }
    $args = @("exec", $ContainerName, "curl", "-sS")
    if ($ApiKey) {
        $args += @("-H", "Authorization: Bearer $ApiKey")
    }
    $args += $url
    $output = & docker @args
    if ($LASTEXITCODE -ne 0) {
        throw "docker curl failed for $url with exit code $LASTEXITCODE"
    }
    return ($output -join "`n")
}

function Get-VllmMetricValue {
    param(
        [string]$Metrics,
        [string]$Name
    )

    $line = ($Metrics -split "`n") |
        Where-Object { $_ -match "^$([regex]::Escape($Name))\{" } |
        Select-Object -First 1
    if (-not $line) {
        return $null
    }
    $parts = $line.Trim() -split "\s+"
    if ($parts.Count -lt 2) {
        return $null
    }
    $value = 0.0
    if ([double]::TryParse($parts[-1], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$value)) {
        return $value
    }
    return $null
}

function Get-VllmSnapshot {
    $metrics = $null
    $reachable = $false
    try {
        $metrics = Invoke-CurlText -Url $MetricsUrl -ApiKey $ApiKey
        $reachable = $true
    } catch {
        try {
            $metrics = Invoke-DockerCurlText -Path "/metrics" -ApiKey $ApiKey
            $reachable = $true
        } catch {
            $metrics = $null
        }
    }
    return @{
        reachable = $reachable
        running = if ($metrics) { Get-VllmMetricValue -Metrics $metrics -Name "vllm:num_requests_running" } else { $null }
        waiting = if ($metrics) { Get-VllmMetricValue -Metrics $metrics -Name "vllm:num_requests_waiting" } else { $null }
        success = if ($metrics) { Get-VllmMetricValue -Metrics $metrics -Name "vllm:request_success_total" } else { $null }
    }
}

function Test-VllmModelsReady {
    try {
        Invoke-CurlText -Url $ModelsUrl -ApiKey $ApiKey | Out-Null
        return $true
    } catch {
        try {
            Invoke-DockerCurlText -Path "/v1/models" -ApiKey $ApiKey | Out-Null
            return $true
        } catch {
            return $false
        }
    }
}

function Get-ContainerAgeSeconds {
    param([string]$Name)

    try {
        $startedAt = docker inspect --format "{{.State.StartedAt}}" $Name 2>$null
        if (-not $startedAt) {
            return $null
        }
        $started = [DateTimeOffset]::Parse($startedAt, [Globalization.CultureInfo]::InvariantCulture)
        return [Math]::Max(0, [int]([DateTimeOffset]::UtcNow - $started).TotalSeconds)
    } catch {
        return $null
    }
}

function Stop-AgentWorkers {
    $allProcesses = @(Get-CimInstance Win32_Process)
    $targets = $allProcesses | Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -and
        $_.CommandLine -like "*run_agent_worker.py*" -and
        $_.CommandLine -like "*--lane llm*"
    }
    if (-not $targets) {
        Write-Host "No Timeline LLM agent worker processes found."
        return
    }
    foreach ($target in $targets) {
        Write-Host ("Stopping LLM worker PID {0} {1}" -f $target.ProcessId, $target.Name)
        Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Wait-VllmReady {
    for ($i = 1; $i -le 150; $i++) {
        try {
            Invoke-CurlText -Url $ModelsUrl -ApiKey $ApiKey | Out-Null
            return $true
        } catch {
            try {
                Invoke-DockerCurlText -Path "/v1/models" -ApiKey $ApiKey | Out-Null
                return $true
            } catch {
                Start-Sleep -Seconds 2
            }
        }
    }
    return $false
}

Write-Host "vLLM wedged-request check" -ForegroundColor Yellow
Write-Host "Container: $ContainerName"

if (-not $ContainerName) {
    Write-Warning "VLLM_CONTAINER_NAME is not set. This script can check HTTP health, but cannot restart a container."
    if ($Restart) {
        exit 2
    }
}

$status = $null
if ($ContainerName) {
    try {
        $status = docker inspect --format "{{.State.Status}}" $ContainerName 2>$null
    } catch {
        $status = $null
    }
    if ($status -ne "running") {
        Write-Host "Container is not running: $status"
        exit 2
    }
}

$containerAge = if ($ContainerName) { Get-ContainerAgeSeconds -Name $ContainerName } else { $null }
$modelsReady = Test-VllmModelsReady
if (-not $modelsReady) {
    Write-Host "vLLM models endpoint is not ready."
    if ($null -ne $containerAge) {
        Write-Host "Container age: $containerAge second(s). Startup grace: $StartupGraceSeconds second(s)."
    }
    if ($null -ne $containerAge -and $containerAge -lt $StartupGraceSeconds) {
        Write-Host "vLLM appears to be warming up; not restarting inside startup grace."
        exit 10
    }
    if (-not $Restart) {
        Write-Host "Run with -Restart to restart an unready vLLM container after startup grace."
        exit 3
    }
    Write-Warning "vLLM is unready beyond startup grace; restarting."
    $wedged = $true
} else {
    $wedged = $false
}

if (-not $wedged) {
    $first = Get-VllmSnapshot
    Write-Host ("First sample: metrics_reachable={0}, running={1}, waiting={2}, success_total={3}" -f $first.reachable, $first.running, $first.waiting, $first.success)
    if (-not $first.reachable) {
        Write-Host "Metrics are not reachable but models endpoint is ready; treating vLLM as degraded, not wedged."
        exit 0
    }
    Start-Sleep -Seconds ([Math]::Max(1, $SampleSeconds))
    $second = Get-VllmSnapshot
    Write-Host ("Second sample: metrics_reachable={0}, running={1}, waiting={2}, success_total={3}" -f $second.reachable, $second.running, $second.waiting, $second.success)

    $runningStable = (
        $null -ne $first.running -and
        $null -ne $second.running -and
        $first.running -gt 0 -and
        $first.running -eq $second.running
    )
    $waitingIdle = ($null -eq $second.waiting -or $second.waiting -eq 0)
    $successUnchanged = ($null -eq $first.success -or $null -eq $second.success -or $first.success -eq $second.success)
    $wedged = $runningStable -and $waitingIdle -and $successUnchanged
}

if (-not $wedged) {
    Write-Host "vLLM does not look wedged."
    exit 0
}

Write-Warning "vLLM looks wedged: running requests stayed nonzero without progress."
if (-not $Restart) {
    Write-Host "Run with -Restart to restart the vLLM container."
    exit 3
}

Write-Step "Preparing for vLLM restart"
if ($StopWorkers) {
    Stop-AgentWorkers
} else {
    Write-Host "Agent workers were not stopped. Pass -StopWorkers to stop them before restart."
}

if (Test-Path -LiteralPath $Python) {
    & $Python $ResetAgentLocks
} else {
    Write-Warning "Backend Python not found; could not reset job locks: $Python"
}

Write-Step "Restarting vLLM container"
docker restart $ContainerName
if ($LASTEXITCODE -ne 0) {
    throw "docker restart failed with exit code $LASTEXITCODE"
}

Write-Step "Waiting for vLLM readiness"
if (-not (Wait-VllmReady)) {
    throw "vLLM did not become ready after restart."
}
Write-Host "vLLM restarted and is ready." -ForegroundColor Green

if ($RestartWorkers -or $StopWorkers) {
    Write-Step "Ensuring Timeline agent workers are running"
    if (-not (Test-Path -LiteralPath $EnsureWorkers)) {
        throw "Agent worker supervisor not found: $EnsureWorkers"
    }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $EnsureWorkers `
        -WorkerCount $WorkerCount `
        -WorkerConcurrency $WorkerConcurrency `
        -Lane llm
    if ($LASTEXITCODE -ne 0) {
        throw "Agent worker supervisor failed with exit code $LASTEXITCODE"
    }
}
