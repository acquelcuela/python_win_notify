@echo off
setlocal

cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_scheduled_task.ps1"
set "EXIT_CODE=%errorlevel%"

echo.
if not "%EXIT_CODE%"=="0" (
  echo Failed to register scheduled task. Exit code: %EXIT_CODE%
  echo If this is a permission error, right-click this file and select "Run as administrator".
) else (
  echo Scheduled task registration completed.
)
echo.
pause
exit /b %EXIT_CODE%
