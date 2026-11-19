@echo off
rem Google Drive (G:) mount check + mail, run by hand.
rem The nightly run at 22:00 goes through run.bat / config.json batch_schedule.
setlocal
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" main.py --only gdrive_check
exit /b %ERRORLEVEL%
