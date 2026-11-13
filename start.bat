@echo off
REM RIZQ - start the site, job collection and Telegram bot (double-click this file)
REM Runs from the project's own .venv; creates it on first run if it is missing.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo [start.bat] .venv not found, creating it...
  py -3.11 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Python is not installed. Install Python 3.11 from https://www.python.org/downloads/
    goto end
  )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-collect.txt
)
".venv\Scripts\python.exe" server.py
:end
pause
