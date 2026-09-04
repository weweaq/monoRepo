# start_all.ps1 - single-window launcher (langTrack + gacore)
# Usage: run in PowerShell:  .\start_all.ps1
# Both services run as background jobs; logs stream to this window with prefixes.
# Ctrl+C or closing the window stops both. ASCII-only to avoid BOM/encoding issues.
$ErrorActionPreference = "Continue"

$ROOT = "D:\AAAmyPrj\github\myrepos\WithLangGraph"
$PORT = 8000
# langTrack needs .venv (has uvicorn); gacore uses py12 (main env)
$PY_LANGTRACK = "$ROOT\.venv\Scripts\python.exe"
$PY_GACORE = "D:\softwares\miniconda\envs\py12\python.exe"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host " [gacore] single-window launch (QQ Bot + Scheduler + LangTrack)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

Write-Host "[1/3] Stopping old instances (langTrack :$PORT) ..."
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'gacore\.langTrack|start\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800

$conn = netstat -ano | Select-String ":$PORT\s" | Select-String "LISTENING"
if ($conn) {
    $conn | ForEach-Object {
        $pid_ = ($_ -split '\s+')[-1]
        if ($pid_ -match '^\d+$') { Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 800
}
Write-Host "[1/3] old instances cleaned" -ForegroundColor Green

Write-Host "[2/3] Starting langTrack (:${PORT}) and gacore ..."
$env:PYTHONPATH = "$ROOT\src"

$langTrackJob = Start-Job -Name langTrack -ScriptBlock {
    param($Root, $Py, $Port)
    Set-Location $Root
    $env:PYTHONPATH = "$Root\src"
    & $Py -m gacore.langTrack --host 0.0.0.0 --port $Port 2>&1
} -ArgumentList $ROOT, $PY_LANGTRACK, $PORT

$gacoreJob = Start-Job -Name gacore -ScriptBlock {
    param($Root, $Py)
    Set-Location $Root
    $env:PYTHONPATH = "$Root\src"
    & $Py start.py 2>&1
} -ArgumentList $ROOT, $PY_GACORE

Write-Host "[2/3] both jobs started" -ForegroundColor Green

Write-Host "[3/3] streaming logs (Ctrl+C or close window = stop all)" -ForegroundColor Green
Write-Host "--------------------------------------------" -ForegroundColor DarkGray

try {
    while ($true) {
        $alive = $false
        foreach ($job in @($langTrackJob, $gacoreJob)) {
            if ($null -ne $job -and $job.State -ne 'Completed' -and $job.State -ne 'Failed') {
                $alive = $true
                Receive-Job -Job $job -ErrorAction SilentlyContinue | ForEach-Object {
                    $prefix = if ($job.Name -eq 'langTrack') { '[langTrack]' } else { '[gacore]  ' }
                    $color = if ($job.Name -eq 'langTrack') { 'Yellow' } else { 'White' }
                    Write-Host ("{0} {1}" -f $prefix, $_) -ForegroundColor $color
                }
            }
        }
        if (-not $alive) {
            Write-Host "Both services exited." -ForegroundColor Red
            break
        }
        Start-Sleep -Milliseconds 500
    }
}
finally {
    Write-Host "Stopping all services ..." -ForegroundColor Yellow
    foreach ($job in @($langTrackJob, $gacoreJob)) {
        if ($null -ne $job) {
            Stop-Job -Job $job -ErrorAction SilentlyContinue
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
    }
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match 'gacore\.langTrack|start\.py' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "All stopped." -ForegroundColor Green
}
