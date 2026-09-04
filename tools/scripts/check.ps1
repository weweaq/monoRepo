# Quality gate for the mono workspace: ruff + pytest (R7 in AGENTS.md).
# Usage: powershell -File tools/scripts/check.ps1 [-Fix]

param(
    [switch]$Fix
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

if ($Fix) {
    uv run ruff check --fix apps packages tests
} else {
    uv run ruff check apps packages tests
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "[check] ruff failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

uv run pytest
if ($LASTEXITCODE -ne 0) {
    Write-Host "[check] pytest failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "[check] all green" -ForegroundColor Green
