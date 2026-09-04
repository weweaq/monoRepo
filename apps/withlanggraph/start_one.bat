@echo off
rem Single-service launcher: delegates to start_one.ps1 (langTrack or gacore)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_one.ps1" %*
