@echo off
setlocal

cd /d "%~dp0"

echo post_x_note runner
echo.
echo This runner is disabled by config.json until you turn it on.
echo If X posting is enabled later, the following keys are required:
echo   X_API_KEY
echo   X_API_SECRET
echo   X_ACCESS_TOKEN
echo   X_ACCESS_TOKEN_SECRET
echo.

if not exist logs mkdir logs

set "LOG_DATE=%date:~0,4%%date:~5,2%%date:~8,2%"
echo %LOG_DATE%| findstr /r "^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if errorlevel 1 (
  for /f %%i in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "LOG_DATE=%%i"
)
set "TASK_LOG=%~dp0logs\task_post_x_note_%LOG_DATE%.log"

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"

>> "%TASK_LOG%" echo [%date% %time%] post_x_note run started.

if not exist "%VENV_PYTHON%" (
  >> "%TASK_LOG%" echo [%date% %time%] Virtual environment was not found. Run setup_windows.bat first.
  echo Virtual environment was not found. Run setup_windows.bat first.
  pause
  exit /b 1
)

"%VENV_PYTHON%" -m modules.post_x_note >> "%TASK_LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
>> "%TASK_LOG%" echo [%date% %time%] post_x_note run finished. Exit code: %EXIT_CODE%
>> "%TASK_LOG%" echo.

if not "%EXIT_CODE%"=="0" (
  echo post_x_note run failed. Exit code: %EXIT_CODE%
  echo Check logs\task_post_x_note_%LOG_DATE%.log
) else (
  echo post_x_note run completed.
  echo Check logs\task_post_x_note_%LOG_DATE%.log
)
pause
exit /b %EXIT_CODE%
