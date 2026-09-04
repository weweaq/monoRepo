# start_one.ps1 - single-service launcher (mirrors start_all.ps1, one service only)
# Usage:
#   .\start_one.ps1 langTrack    # start only the langTrack receiving service (:8000)
#   .\start_one.ps1 gacore       # start only QQ Bot + Scheduler (start.py)
# Before starting, kills the OTHER service plus any stale instance of the target,
# so only the chosen service keeps running. Logs stream to this window with a prefix.
# Ctrl+C or closing the window stops the service. ASCII-only to avoid BOM/encoding issues.
param([Parameter(Position=0)][string]$Service)

$ErrorActionPreference = "Continue"

if ($Service -notmatch '^(langTrack|gacore)$') {
    Write-Host "Usage: .\start_one.ps1 <langTrack|gacore>" -ForegroundColor Yellow
    exit 1
}

$ROOT = "D:\AAAmyPrj\github\myrepos\WithLangGraph"
$PORT = 8000
# langTrack needs .venv (has uvicorn); gacore uses py12 (main env)
$PY_LANGTRACK = "$ROOT\.venv\Scripts\python.exe"
$PY_GACORE   = "D:\softwares\miniconda\envs\py12\python.exe"

$label = if ($Service -eq 'langTrack') { 'langTrack' } else { 'gacore' }
Write-Host "============================================" -ForegroundColor Cyan
Write-Host " [gacore] single-service launch: $label (others will be stopped)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# 1. kill BOTH services (the other + any stale instance of the target) so only the chosen one survives
Write-Host "[1/3] Stopping other / stale instances ..." -ForegroundColor DarkGray
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'gacore\.langTrack|gacore\.weitrack|start\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800

if ($Service -eq 'langTrack') {
    $conn = netstat -ano | Select-String ":$PORT\s" | Select-String "LISTENING"
    if ($conn) {
        $conn | ForEach-Object {
            $pid_ = ($_ -split '\s+')[-1]
            if ($pid_ -match '^\d+$') { Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue }
        }
        Start-Sleep -Milliseconds 800
    }
}
Write-Host "[1/3] cleaned" -ForegroundColor Green

# 2. start the chosen service as a background job
Write-Host "[2/3] Starting $label ..." -ForegroundColor DarkGray
$env:PYTHONPATH = "$ROOT\src"

if ($Service -eq 'langTrack') {
    $job = Start-Job -Name langTrack -ScriptBlock {
        param($Root, $Py, $Port)
        Set-Location $Root
        $env:PYTHONPATH = "$Root\src"
        & $Py -m gacore.langTrack --host 0.0.0.0 --port $Port 2>&1
    } -ArgumentList $ROOT, $PY_LANGTRACK, $PORT
} else {
    $job = Start-Job -Name gacore -ScriptBlock {
        param($Root, $Py)
        Set-Location $Root
        $env:PYTHONPATH = "$Root\src"
        & $Py start.py 2>&1
    } -ArgumentList $ROOT, $PY_GACORE
}
Write-Host "[2/3] $label started" -ForegroundColor Green

# 3. stream logs (Ctrl+C or close window = stop all)
Write-Host "[3/3] streaming logs (Ctrl+C or close window = stop)" -ForegroundColor Green
Write-Host "--------------------------------------------" -ForegroundColor DarkGray
try {
    while ($true) {
        if ($null -ne $job -and $job.State -ne 'Completed' -and $job.State -ne 'Failed') {
            Receive-Job -Job $job -ErrorAction SilentlyContinue | ForEach-Object {
                $prefix = if ($Service -eq 'langTrack') { '[langTrack]' } else { '[gacore]  ' }
                $color  = if ($Service -eq 'langTrack') { 'Yellow' } else { 'White' }
                Write-Host ("{0} {1}" -f $prefix, $_) -ForegroundColor $color
            }
        } else {
            Write-Host "$label exited." -ForegroundColor Red
            break
        }
        Start-Sleep -Milliseconds 500
    }
}
finally {
    Write-Host "Stopping $label ..." -ForegroundColor Yellow
    if ($null -ne $job) {
        Stop-Job -Job $job -ErrorAction SilentlyContinue
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match 'gacore\.langTrack|gacore\.weitrack|start\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "Stopped." -ForegroundColor Green
}
