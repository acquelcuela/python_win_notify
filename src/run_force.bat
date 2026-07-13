@echo off
setlocal

cd /d "%~dp0"

echo NightlyBatchNotify force test run
echo.
echo Select the schedule pattern to test.
echo  1 = 07:00
echo  2 = 09:30
echo  3 = 12:15
echo  4 = 22:45
echo  5 = All enabled modules
echo.
echo This runs main.py --force and ignores time-of-day checks.
echo Enabled modules may fetch market/news data, call Gemini, and send Gmail.
echo.

choice /c 12345 /n /m "Choose a pattern: "
set "SCHEDULE_ARG="
if errorlevel 5 goto SCHED_ALL
if errorlevel 4 set "SCHEDULE_ARG=22:45"
if errorlevel 3 set "SCHEDULE_ARG=12:15"
if errorlevel 2 set "SCHEDULE_ARG=09:30"
if errorlevel 1 set "SCHEDULE_ARG=07:00"
goto SCHED_DONE

:SCHED_ALL
echo Running all currently enabled modules.
goto SCHED_DONE

:SCHED_DONE

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

if "%SCHEDULE_ARG%"=="" (
  "%VENV_PYTHON%" main.py --force >> "%TASK_LOG%" 2>&1
) else (
  "%VENV_PYTHON%" main.py --force --schedule %SCHEDULE_ARG% >> "%TASK_LOG%" 2>&1
)
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
