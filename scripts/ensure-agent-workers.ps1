param(
    [string]$Root = $(Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)),
    [int]$WorkerCount = 1,
    [int]$WorkerConcurrency = 2,
    [switch]$CoreOnly,
    [ValidateSet("cpu", "llm", "full")]
    [string]$Lane = "full",
    [switch]$Restart
)

$ErrorActionPreference = "Stop"

$Backend = Join-Path $Root "backend"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"
$WorkerScript = Join-Path $Backend "scripts\run_agent_worker.py"
$ResetAgentLocks = Join-Path $Backend "scripts\reset_agent_locks.py"

function Get-AgentWorkerPythonProcesses {
    param([string]$WorkerLane)
    $allProcesses = @(Get-CimInstance Win32_Process)
    $candidates = @($allProcesses | Where-Object {
        $_.Name -like "python*" -and
        $_.CommandLine -and
        $_.CommandLine -like "*run_agent_worker.py*" -and
        (
            ($WorkerLane -eq "full" -and $_.CommandLine -like "* --lane full*") -or
            ($WorkerLane -eq "cpu" -and (($_.CommandLine -like "* --lane cpu*") -or ($_.CommandLine -like "* --core-only*"))) -or
            ($WorkerLane -eq "llm" -and $_.CommandLine -like "* --lane llm*")
        )
    })
    @($candidates | Where-Object {
        $candidate = $_
        -not ($candidates | Where-Object { $_.ParentProcessId -eq $candidate.ProcessId } | Select-Object -First 1)
    })
}

function Get-AgentWorkerWrappers {
    param([string]$WorkerLane = "")
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "cmd.exe" -and
        $_.CommandLine -and
        $_.CommandLine -like "*Timeline Agent Worker*" -and
        $_.CommandLine -like "*run_agent_worker.py*" -and
        (
            [string]::IsNullOrWhiteSpace($WorkerLane) -or
            ($WorkerLane -eq "full" -and $_.CommandLine -like "* --lane full*") -or
            ($WorkerLane -eq "cpu" -and (($_.CommandLine -like "* --lane cpu*") -or ($_.CommandLine -like "* --core-only*"))) -or
            ($WorkerLane -eq "llm" -and $_.CommandLine -like "* --lane llm*")
        )
    })
}

function Stop-AgentWorkers {
    $workers = @(Get-AgentWorkerPythonProcesses -WorkerLane $Lane)
    $wrappers = @(Get-AgentWorkerWrappers -WorkerLane $Lane)
    if (-not $workers -and -not $wrappers) {
        Write-Host "No Timeline agent worker processes found."
        return 0
    }
    foreach ($worker in $workers) {
        Write-Host ("Stopping worker PID {0} {1}" -f $worker.ProcessId, $worker.Name)
        Stop-Process -Id $worker.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($wrapper in $wrappers) {
        Write-Host ("Stopping worker wrapper PID {0}" -f $wrapper.ProcessId)
        Stop-Process -Id $wrapper.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    return ($workers.Count + $wrappers.Count)
}

function Remove-OrphanWorkerWrappers {
    param([string]$WorkerLane)
    $livePython = @(Get-AgentWorkerPythonProcesses -WorkerLane $WorkerLane)
    $wrappers = @(Get-AgentWorkerWrappers -WorkerLane $WorkerLane)
    if ($livePython.Count -gt 0) {
        return
    }
    foreach ($wrapper in $wrappers) {
        Write-Host ("Stopping orphan worker wrapper PID {0}; no live Python worker for lane {1}." -f $wrapper.ProcessId, $WorkerLane)
        Stop-Process -Id $wrapper.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Backend virtualenv Python not found: $Python"
}
if (-not (Test-Path -LiteralPath $WorkerScript)) {
    throw "Agent worker script not found: $WorkerScript"
}

$WorkerCount = [Math]::Max(1, [Math]::Min(8, $WorkerCount))
$WorkerConcurrency = [Math]::Max(1, [Math]::Min(8, $WorkerConcurrency))
if ($CoreOnly) {
    $Lane = "cpu"
}

if ($Restart) {
    Stop-AgentWorkers | Out-Null
    if (Test-Path -LiteralPath $ResetAgentLocks) {
        & $Python $ResetAgentLocks
    }
}

Remove-OrphanWorkerWrappers -WorkerLane $Lane
$existing = @(Get-AgentWorkerPythonProcesses -WorkerLane $Lane)
$missing = [Math]::Max(0, $WorkerCount - $existing.Count)
if ($missing -eq 0) {
    Write-Host "Agent worker target already satisfied: $($existing.Count)/$WorkerCount live Python process(es) for lane $Lane."
    exit 0
}

for ($index = 1; $index -le $missing; $index++) {
    $workerNumber = $existing.Count + $index
    $laneArg = " --lane $Lane"
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/k", "title Timeline Agent Worker $Lane $workerNumber && cd /d `"$Backend`" && .\.venv\Scripts\python.exe scripts\run_agent_worker.py --poll-seconds 1 --concurrency $WorkerConcurrency$laneArg" `
        -WorkingDirectory $Backend `
        -WindowStyle Hidden
}

Start-Sleep -Seconds 1
$after = @(Get-AgentWorkerPythonProcesses -WorkerLane $Lane)
$afterWrappers = @(Get-AgentWorkerWrappers -WorkerLane $Lane)
$mode = $Lane
Write-Host "Agent worker live Python process(es): $($after.Count). Wrapper process(es): $($afterWrappers.Count). Target: $WorkerCount. Concurrency per process: $WorkerConcurrency. Mode: $mode."

if ($after.Count -lt $WorkerCount) {
    exit 4
}
exit 0
