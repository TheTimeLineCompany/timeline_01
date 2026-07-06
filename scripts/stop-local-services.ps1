$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Backend = Join-Path $Root "backend"
$Cleanup = Join-Path $ScriptDir "cleanup-local-processes.ps1"
$ResetAgentLocks = Join-Path $Backend "scripts\reset_agent_locks.py"
$Python = Join-Path $Backend ".venv\Scripts\python.exe"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host $Message -ForegroundColor Cyan
}

Write-Host "Timeline stop" -ForegroundColor Yellow
Write-Host "This stops only Timeline app processes: backend, frontend, and workers."
Write-Host "It intentionally leaves external vLLM/model-serving processes running."

Write-Step "[1/2] Stopping local app processes"
if (-not (Test-Path -LiteralPath $Cleanup)) {
    throw "Cleanup script not found: $Cleanup"
}
& $Cleanup

Write-Step "[2/2] Releasing running job locks"
if (Test-Path -LiteralPath $Python) {
    & $Python $ResetAgentLocks
} else {
    Write-Warning "Backend virtualenv Python not found. Could not reset running job locks: $Python"
}

Write-Host ""
Write-Host "Timeline app stopped." -ForegroundColor Green
Write-Host "The model server was not stopped."
