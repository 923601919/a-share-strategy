$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$api = Join-Path $root "apps\api"
Set-Location $api
if (-not (Test-Path ".\.venv\Scripts\uvicorn.exe")) {
  Write-Host "请先创建 venv 并 pip install -r requirements.txt"
  exit 1
}
# 默认真实行情；需要演示数据时：$env:DEMO_MODE="true"
if (-not $env:DEMO_MODE) { $env:DEMO_MODE = "false" }
Write-Host "DEMO_MODE=$($env:DEMO_MODE)  API -> http://127.0.0.1:8000"
& .\.venv\Scripts\uvicorn.exe main:app --reload --host 127.0.0.1 --port 8000
