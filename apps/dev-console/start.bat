@echo off
setlocal
cd /d D:\AAAmyPrj\github\myrepos\mono\apps\dev-console
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|pythonw)\.exe$' -and $_.CommandLine -match 'server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul
start "" "D:\AAAmyPrj\github\myrepos\mono\.venv\Scripts\pythonw.exe" server.py
exit /b
