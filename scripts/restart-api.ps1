# One-click API restart: kill port 8000 listener -> start venv uvicorn -> show health.
# Runs uvicorn in the FOREGROUND (like start-api.ps1), so it survives as long as
# your terminal is open. Press Ctrl+C to stop.
#
# Usage:   powershell -File scripts/restart-api.ps1
# Reload:  powershell -File scripts/restart-api.ps1 -WithReload
#          (note: --reload does NOT work reliably on this machine)
#
# Optional: -Detach  start uvicorn detached (only works in a real interactive shell)

param(
  [switch]$WithReload,
  [switch]$Detach,
  [int]$Port = 8000,
  [int]$HealthTimeoutSec = 40
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$api = Join-Path $root "apps\api"
$uvicorn = Join-Path $api ".venv\Scripts\uvicorn.exe"

if (-not (Test-Path $uvicorn)) {
  Write-Host "Missing $uvicorn - create venv and install deps first." -ForegroundColor Red
  exit 1
}

# 1) free the port
$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conns) {
  $procIds = $conns | Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($procId in $procIds) {
    Write-Host "Stopping PID=$procId on port $Port ..." -ForegroundColor Yellow
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Milliseconds 800
} else {
  Write-Host "Port $Port is free." -ForegroundColor DarkGray
}

# 2) env
if (-not $env:DEMO_MODE) { $env:DEMO_MODE = "false" }
if (-not $env:SSL_VERIFY) { $env:SSL_VERIFY = "false" }

# 3) build args
$args = @("main:app", "--host", "127.0.0.1", "--port", "$Port")
if ($WithReload) { $args += "--reload" }

Write-Host "Starting API (DEMO_MODE=$($env:DEMO_MODE) SSL_VERIFY=$($env:SSL_VERIFY) reload=$WithReload) ..." -ForegroundColor Cyan
Write-Host "  -> http://127.0.0.1:$Port  (Ctrl+C to stop)" -ForegroundColor DarkGray

if ($Detach) {
  Start-Process -FilePath $uvicorn -ArgumentList $args -WorkingDirectory $api -WindowStyle Hidden
  Start-Sleep -Seconds 2
} else {
  # foreground: this blocks until uvicorn exits
  & $uvicorn @args
}
