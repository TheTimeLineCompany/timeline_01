param(
    [string]$BackendJobsUrl = "http://127.0.0.1:8000/api/reader/agent/jobs"
)

$ErrorActionPreference = "Continue"

function Get-AgentWorkerProcesses {
    param([string]$Lane)
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -like "python*" -and
        $_.CommandLine -and
        $_.CommandLine -like "*run_agent_worker.py*" -and
        $_.CommandLine -like "* --lane $Lane*"
    })
}

function Show-Lane {
    param([string]$Lane)
    $workers = @(Get-AgentWorkerProcesses -Lane $Lane)
    Write-Host ""
    Write-Host ("{0} worker lane" -f $Lane.ToUpperInvariant()) -ForegroundColor Cyan
    if (-not $workers) {
        Write-Host "  no live worker process found"
        return
    }
    foreach ($worker in $workers) {
        Write-Host ("  PID {0}: {1}" -f $worker.ProcessId, $worker.CommandLine)
    }
}

Write-Host "Timeline agent lane diagnostic" -ForegroundColor Green
Show-Lane -Lane "cpu"
Show-Lane -Lane "llm"
Show-Lane -Lane "full"

Write-Host ""
Write-Host "Job queue by type/status" -ForegroundColor Cyan
try {
    $jobs = Invoke-RestMethod -Uri $BackendJobsUrl -TimeoutSec 5
    if (-not $jobs) {
        Write-Host "  no job rows returned"
        exit 0
    }
    $jobs |
        Sort-Object job_type, status |
        ForEach-Object {
            Write-Host ("  {0,-32} {1,-10} {2,5}" -f $_.job_type, $_.status, $_.count)
        }
} catch {
    Write-Warning "Could not read backend job summary at ${BackendJobsUrl}: $($_.Exception.Message)"
}
