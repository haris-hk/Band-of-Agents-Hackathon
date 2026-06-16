#!/usr/bin/env pwsh
# Start backend + frontend for hackathon demo (Windows).
param(
    [switch]$RunE2E
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

if (-not (Test-Path "$Root\.env")) {
    Copy-Item "$Root\.env.example" "$Root\.env"
    Write-Host "Created .env from .env.example (demo defaults: LIVE_LLM_ENABLED=false)"
}

$python = if (Test-Path "$Root\venv\Scripts\python.exe") {
    "$Root\venv\Scripts\python.exe"
} else {
    "python"
}

Write-Host "Checking Docker..."
docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Docker is not running. Start Docker Desktop before running the pipeline."
}

Write-Host "Installing backend (editable) if needed..."
Push-Location $Root
& $python -m pip install -q -e ".[dev]" 2>$null
Pop-Location

Write-Host "Starting backend on http://127.0.0.1:8000 ..."
$backend = Start-Process -FilePath $python `
    -ArgumentList "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $Root -PassThru -WindowStyle Minimized

$health = $null
for ($i = 0; $i -lt 45; $i++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($health.status -eq "ok") { break }
    } catch {
        Start-Sleep -Seconds 1
    }
}

if (-not $health) {
    Write-Error "Backend did not become healthy in time."
    exit 1
}

Write-Host "Backend healthy. docker_available=$($health.docker_available)"

if (-not (Test-Path "$Root\frontend\node_modules")) {
    Write-Host "Installing frontend dependencies..."
    Push-Location "$Root\frontend"
    npm ci
    Pop-Location
}

Write-Host "Starting frontend on http://localhost:3000 ..."
$frontend = Start-Process -FilePath "cmd" -ArgumentList "/c", "npm", "run", "dev" `
    -WorkingDirectory "$Root\frontend" -PassThru

Write-Host ""
Write-Host "=== Band Incident Response ==="
Write-Host "  UI:      http://localhost:3000  (use 'Local demo' tab)"
Write-Host "  API:     http://127.0.0.1:8000/health"
Write-Host "  Backend PID: $($backend.Id)  Frontend PID: $($frontend.Id)"
Write-Host ""

if ($RunE2E) {
    Write-Host "Running WebSocket E2E demo..."
    & $python "$Root\scripts\e2e_ws_pipeline.py"
    exit $LASTEXITCODE
}

Write-Host "Press Ctrl+C in this window to stop (child processes keep running)."
Write-Host "To run headless demo: .\scripts\start.ps1 -RunE2E"
