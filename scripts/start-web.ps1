$ErrorActionPreference = "Stop"
$env:Path = "C:\Program Files\nodejs;" + $env:Path
$root = Split-Path -Parent $PSScriptRoot
$web = Join-Path $root "apps\web"
Set-Location $web
if (-not $env:NEXT_PUBLIC_API_BASE) {
  $env:NEXT_PUBLIC_API_BASE = "http://127.0.0.1:8000"
}
Write-Host "Web -> http://localhost:3000  API=$($env:NEXT_PUBLIC_API_BASE)"
npm run dev -- --port 3000
