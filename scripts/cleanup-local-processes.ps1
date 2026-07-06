$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$workspacePattern = "*$Root*"
$allProcesses = @(Get-CimInstance Win32_Process)
$allowedNames = @(
    "cmd.exe",
    "conhost.exe",
    "python.exe",
    "node.exe",
    "esbuild.exe"
)

function Get-DescendantProcessIds {
    param(
        [int]$ParentId,
        [object[]]$Processes
    )

    $children = @($Processes | Where-Object { $_.ParentProcessId -eq $ParentId })
    foreach ($child in $children) {
        $child.ProcessId
        Get-DescendantProcessIds -ParentId $child.ProcessId -Processes $Processes
    }
}

$targets = $allProcesses | Where-Object {
    $_.ProcessId -ne $PID -and
    $_.CommandLine -and
    (
        $_.CommandLine -like "*uvicorn app.main:app*" -or
        $_.CommandLine -like "*run_agent_worker.py*" -or
        $_.CommandLine -like "*watch-agent-workers.ps1*" -or
        $_.CommandLine -like "*watch-vllm-health.ps1*" -or
        (
            (
                $_.CommandLine -like $workspacePattern -or
                $_.CommandLine -like "*backend*" -or
                $_.CommandLine -like "*frontend*"
            ) -and
            (
                $_.CommandLine -like "*npm*run dev*" -or
                $_.CommandLine -like "*vite*--port 5174*"
            )
        )
    )
}

$targetIds = [System.Collections.Generic.HashSet[int]]::new()
foreach ($target in $targets) {
    [void]$targetIds.Add([int]$target.ProcessId)
    foreach ($childId in (Get-DescendantProcessIds -ParentId $target.ProcessId -Processes $allProcesses)) {
        $child = $allProcesses | Where-Object { $_.ProcessId -eq $childId } | Select-Object -First 1
        if ($childId -ne $PID -and $child -and $allowedNames -contains $child.Name) {
            [void]$targetIds.Add([int]$childId)
        }
    }
}

$stopList = $allProcesses |
    Where-Object { $targetIds.Contains([int]$_.ProcessId) } |
    Sort-Object ProcessId -Descending

if (-not $stopList) {
    Write-Host "       No stale app processes found."
    exit 0
}

foreach ($target in $stopList) {
    Write-Host ("       Stopping PID {0}  {1}" -f $target.ProcessId, $target.Name)
    $killCommand = "taskkill /PID $($target.ProcessId) /F >NUL 2>NUL"
    cmd.exe /d /c $killCommand | Out-Null
    Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1
Write-Host ("       Stopped {0} stale process(es)." -f $stopList.Count)
