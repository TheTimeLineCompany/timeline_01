@echo off
setlocal
title Timeline vLLM Watchdog

set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\watch-vllm-health.ps1" -Restart -StopWorkers -RestartWorkers -EnsureWorkersWhenHealthy %*
exit /b %ERRORLEVEL%
