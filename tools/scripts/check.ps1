# Quality gate for the mono workspace: ruff + pytest (R7 in AGENTS.md).
# Usage: powershell -File tools/scripts/check.ps1 [-Fix]
# --all-packages installs every workspace member so member tests can import
# their own package (uv's default sync only installs the root project).

param(
    [switch]$Fix
)

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

uv sync --all-packages
if ($LASTEXITCODE -ne 0) {
    Write-Host "[check] uv sync failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

if ($Fix) {
    uv run ruff check --fix apps packages tests
} else {
    uv run ruff check apps packages tests
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "[check] ruff failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

uv run --all-packages pytest
if ($LASTEXITCODE -ne 0) {
    Write-Host "[check] pytest failed" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "[check] all green" -ForegroundColor Green
