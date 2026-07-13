@echo off
setlocal

cd /d "%~dp0"

echo post_x_note preview runner
echo.
echo This runs note fetch preview logic without posting to X.
echo Place a preview article at:
echo   state\post_x_note_preview_article.json
echo.
echo The runner forces local text generation and does not use Gemini.
echo.

if not exist logs mkdir logs

set "LOG_DATE=%date:~0,4%%date:~5,2%%date:~8,2%"
echo %LOG_DATE%| findstr /r "^[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]$" >nul
if errorlevel 1 (
  for /f %%i in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "LOG_DATE=%%i"
)
set "TASK_LOG=%~dp0logs\task_post_x_note_preview_%LOG_DATE%.log"

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
set "PREVIEW_FILE=%~dp0state\post_x_note_preview_article.json"

>> "%TASK_LOG%" echo [%date% %time%] post_x_note preview run started.

if not exist "%VENV_PYTHON%" (
  >> "%TASK_LOG%" echo [%date% %time%] Virtual environment was not found. Run setup_windows.bat first.
  echo Virtual environment was not found. Run setup_windows.bat first.
  pause
  exit /b 1
)

if not exist "%PREVIEW_FILE%" (
  >> "%TASK_LOG%" echo [%date% %time%] Preview article file was not found: %PREVIEW_FILE%
  echo Preview article file was not found:
  echo   %PREVIEW_FILE%
  pause
  exit /b 1
)

set "POST_X_NOTE_PREVIEW=1"
set "POST_X_NOTE_FORCE_LOCAL=1"
set "POST_X_NOTE_PREVIEW_ARTICLE_FILE=%PREVIEW_FILE%"

"%VENV_PYTHON%" -m modules.post_x_note >> "%TASK_LOG%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
>> "%TASK_LOG%" echo [%date% %time%] post_x_note preview run finished. Exit code: %EXIT_CODE%
>> "%TASK_LOG%" echo.

if not "%EXIT_CODE%"=="0" (
  echo post_x_note preview run failed. Exit code: %EXIT_CODE%
  echo Check logs\task_post_x_note_preview_%LOG_DATE%.log
) else (
  echo post_x_note preview run completed.
  echo Check output\post_x_note.json
  echo Check logs\task_post_x_note_preview_%LOG_DATE%.log
)
pause
exit /b %EXIT_CODE%
