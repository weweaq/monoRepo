@echo off
set PYTHONPATH=D:\AAAmyPrj\github\myrepos\WithLangGraph\src

REM Kill any leftover gacore qq.py instance (frees the single-instance port 19528).
REM Matches only this project's qq.py, so other python apps (e.g. GenericAgent) survive.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*WithLangGraph*frontends*qq.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"

D:\softwares\miniconda\envs\py12\python.exe D:\AAAmyPrj\github\myrepos\WithLangGraph\src\gacore\frontends\qq.py
if errorlevel 1 (
    echo.
    echo [qq.bat] gacore exited with code %errorlevel%. See message above.
    pause
)
