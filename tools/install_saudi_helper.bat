@echo off
rem Double-click once on the old PC: installs everything the Saudi helper needs (see install_saudi_helper.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_saudi_helper.ps1"
pause
