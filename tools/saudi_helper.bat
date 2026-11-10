@echo off
rem Saudi helper (see tools/saudi_helper.py). Windows Task Scheduler runs this every 2 hours.
cd /d "%~dp0.."
set /p DATABASE_URL=<"%USERPROFILE%\.munfarid\database_url"
set PYTHONUTF8=1
"%LOCALAPPDATA%\Programs\Python\Python311\python.exe" tools\saudi_helper.py >nul 2>&1
