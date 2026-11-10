@echo off
REM RIZQ - start the site, job collection and Telegram bot (double-click this file)
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
py -3 --version >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
  goto end
)
python --version >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto end
)
echo Python is not installed. Install it from https://www.python.org/downloads/
:end
pause
