@echo off
rem Saudi helper (see tools/saudi_helper.py). Windows Task Scheduler runs this once a day.
cd /d "%~dp0.."
set /p DATABASE_URL=<"%USERPROFILE%\.munfarid\database_url"
set PYTHONUTF8=1
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe
"%PY%" tools\saudi_helper.py >nul 2>&1
