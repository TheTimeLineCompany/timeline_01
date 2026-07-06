@echo off
setlocal
title Timeline Startup
color 0A

set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\start-local-services.ps1"
if errorlevel 1 (
  echo.
  echo Timeline startup failed.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:5174"
exit /b 0
