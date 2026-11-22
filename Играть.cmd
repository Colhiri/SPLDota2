@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0offline_league\Launch-League.ps1" -Action Play %*
if errorlevel 1 pause
