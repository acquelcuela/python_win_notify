@echo off
setlocal

cd /d "%~dp0"

if not exist logs mkdir logs

set "LOG_DATE=%date:~0,4%%date:~5,2%%date:~8,2%"
echo %LOG_DATE%| findstr /r "^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if errorlevel 1 (
  for /f %%i in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "LOG_DATE=%%i"
)
set "TASK_LOG=%~dp0logs\task_runner_%LOG_DATE%.log"

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"

>> "%TASK_LOG%" echo [%date% %time%] Force test run started.

if not exist "%VENV_PYTHON%" (
  >> "%TASK_LOG%" echo [%date% %time%] Virtual environment was not found. Run setup_windows.bat first.
  echo Virtual environment was not found. Run setup_windows.bat first.
  pause
  exit /b 1
)

"%VENV_PYTHON%" main.py --force >> "%TASK_LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
>> "%TASK_LOG%" echo [%date% %time%] Force test run finished. Exit code: %EXIT_CODE%
>> "%TASK_LOG%" echo.

if not "%EXIT_CODE%"=="0" (
  echo Force test run failed. Exit code: %EXIT_CODE%
  echo Check logs\task_runner_%LOG_DATE%.log
) else (
  echo Force test run completed.
  echo Check logs\task_runner_%LOG_DATE%.log
)
pause
exit /b %EXIT_CODE%
