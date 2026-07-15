@echo off
setlocal

cd /d "%~dp0"

echo post_x_magazine manual runner
echo.
echo This runs main.py --force --schedule 07:30, which (via config.json's
echo batch_schedule modules list) executes ONLY the post_x_magazine module -
echo no other batch modules, no report/mail pipeline.
echo.
echo NOTE: This is a live run, not a preview. It will actually post to X
echo and send the result by email, the same as the scheduled 07:30/21:00 runs.
echo.

if not exist logs mkdir logs

set "LOG_DATE=%date:~0,4%%date:~5,2%%date:~8,2%"
echo %LOG_DATE%| findstr /r "^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if errorlevel 1 (
  for /f %%i in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "LOG_DATE=%%i"
)
set "TASK_LOG=%~dp0logs\task_runner_%LOG_DATE%.log"

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"

>> "%TASK_LOG%" echo [%date% %time%] post_x_magazine manual run started.

if not exist "%VENV_PYTHON%" (
  >> "%TASK_LOG%" echo [%date% %time%] Virtual environment was not found. Run setup_windows.bat first.
  echo Virtual environment was not found. Run setup_windows.bat first.
  pause
  exit /b 1
)

"%VENV_PYTHON%" main.py --force --schedule 07:30 >> "%TASK_LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
>> "%TASK_LOG%" echo [%date% %time%] post_x_magazine manual run finished. Exit code: %EXIT_CODE%
>> "%TASK_LOG%" echo.

if not "%EXIT_CODE%"=="0" (
  echo post_x_magazine run failed. Exit code: %EXIT_CODE%
  echo Check logs\task_runner_%LOG_DATE%.log
) else (
  echo post_x_magazine run completed.
  echo Check logs\task_runner_%LOG_DATE%.log
)
pause
exit /b %EXIT_CODE%
