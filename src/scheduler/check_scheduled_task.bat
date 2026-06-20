@echo off
setlocal

cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_scheduled_task.ps1"
set "EXIT_CODE=%errorlevel%"

echo.
pause
exit /b %EXIT_CODE%
