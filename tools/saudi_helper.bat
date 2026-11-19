@echo off
rem Saudi helper by hand (the daily task runs .venv\Scripts\pythonw.exe tools\saudi_helper.py directly).
rem The script finds its key in %USERPROFILE%\.munfarid by itself. Add --minutes 5 for a short test.
cd /d "%~dp0.."
set PYTHONUTF8=1
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python
"%PY%" tools\saudi_helper.py %*
