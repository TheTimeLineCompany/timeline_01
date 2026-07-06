param(
    [string]$PostgresHost = $env:PG_HOST,
    [int]$PostgresPort = $(if ($env:PG_PORT) { [int]$env:PG_PORT } else { 5432 }),
    [string]$Neo4jHost = "127.0.0.1",
    [int]$Neo4jPort = 7687,
    [string]$LlmBaseUrl = $(if ($env:LLM_BASE_URL) { $env:LLM_BASE_URL.TrimEnd("/") } else { "http://127.0.0.1:8101/v1" })
)

$ErrorActionPreference = "Continue"

Write-Host "Timeline environment check"
Write-Host "=========================="
Write-Host ""

Write-Host "Python launchers:"
py -0p
Write-Host ""

Write-Host "Node:"
node --version
npm.cmd --version
Write-Host ""

if ($PostgresHost) {
    Write-Host "Postgres ${PostgresHost}:${PostgresPort}:"
    Test-NetConnection $PostgresHost -Port $PostgresPort
    Write-Host ""
} else {
    Write-Host "Postgres: set PG_HOST to check connectivity."
    Write-Host ""
}

Write-Host "Neo4j Bolt ${Neo4jHost}:${Neo4jPort}:"
Test-NetConnection $Neo4jHost -Port $Neo4jPort
Write-Host ""

Write-Host "vLLM models endpoint:"
$headers = @{}
if ($env:LLM_API_KEY) {
    $headers.Authorization = "Bearer $($env:LLM_API_KEY)"
}
try {
    Invoke-RestMethod -Uri "$LlmBaseUrl/models" -Headers $headers -TimeoutSec 5 | Out-Null
    Write-Host "Reachable: $LlmBaseUrl/models" -ForegroundColor Green
} catch {
    Write-Warning "Not reachable: $LlmBaseUrl/models"
}
