param(
    [string]$MetricsUrl = "http://127.0.0.1:8101/metrics",
    [string]$ModelsUrl = "http://127.0.0.1:8101/v1/models",
    [string]$ApiKey = $env:LLM_API_KEY,
    [string]$ContainerName = $env:VLLM_CONTAINER_NAME,
    [int]$ContainerPort = 8100,
    [int]$RunningWarnThreshold = 0
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$BackendEnvPath = Join-Path $Root "backend\.env"

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

if (-not $ApiKey) {
    $ApiKey = Read-DotEnvValue -Path $BackendEnvPath -Name "LLM_API_KEY"
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

function Get-MetricNamesLike {
    param(
        [string]$Metrics,
        [string]$Pattern
    )

    @(
        ($Metrics -split "`n") |
            Where-Object { $_ -match "^vllm:" -and $_ -match $Pattern } |
            ForEach-Object { ($_ -split "[\{\s]")[0] } |
            Sort-Object -Unique
    )
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

$modelsOk = $false
$transport = "host"
try {
    Invoke-CurlText -Url $ModelsUrl -ApiKey $ApiKey | Out-Null
    $modelsOk = $true
} catch {
    try {
        Invoke-DockerCurlText -Path "/v1/models" -ApiKey $ApiKey | Out-Null
        $modelsOk = $true
        $transport = "container"
    } catch {
        $modelsOk = $false
    }
}

$metrics = $null
try {
    $metrics = Invoke-CurlText -Url $MetricsUrl -ApiKey $ApiKey
    $transport = "host"
} catch {
    try {
        $metrics = Invoke-DockerCurlText -Path "/metrics" -ApiKey $ApiKey
        $transport = "container"
    } catch {
        $metrics = $null
    }
}
$running = if ($metrics) { Get-VllmMetricValue -Metrics $metrics -Name "vllm:num_requests_running" } else { $null }
$waiting = if ($metrics) { Get-VllmMetricValue -Metrics $metrics -Name "vllm:num_requests_waiting" } else { $null }
$gpuCache = if ($metrics) { Get-VllmMetricValue -Metrics $metrics -Name "vllm:gpu_cache_usage_perc" } else { $null }
if ($null -eq $gpuCache -and $metrics) {
    $gpuCache = Get-VllmMetricValue -Metrics $metrics -Name "vllm:kv_cache_usage_perc"
}

$status = if ($modelsOk) { "reachable" } else { "unreachable" }
$runningText = if ($null -eq $running) { "unknown" } else { $running.ToString("0.###", [Globalization.CultureInfo]::InvariantCulture) }
$waitingText = if ($null -eq $waiting) { "unknown" } else { $waiting.ToString("0.###", [Globalization.CultureInfo]::InvariantCulture) }
$cacheText = if ($null -eq $gpuCache) { "unknown" } else { $gpuCache.ToString("0.###", [Globalization.CultureInfo]::InvariantCulture) }
$cacheMetricHints = @()
if ($null -eq $gpuCache -and $metrics) {
    $cacheMetricHints = Get-MetricNamesLike -Metrics $metrics -Pattern "cache|gpu"
}

Write-Host "vLLM status: $status"
Write-Host "transport: $transport"
Write-Host "metrics reachable: $([bool]$metrics)"
Write-Host "running requests: $runningText"
Write-Host "waiting requests: $waitingText"
Write-Host "gpu cache usage: $cacheText"
if ($cacheMetricHints.Count -gt 0) {
    Write-Host ("available cache/gpu metric names: {0}" -f ($cacheMetricHints -join ", "))
}

if (-not $modelsOk) {
    exit 2
}
if ($null -ne $running -and $running -gt $RunningWarnThreshold) {
    exit 3
}
exit 0
