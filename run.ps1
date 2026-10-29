# 一键启动（Windows）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path "data/demo.sqlite")) {
    Write-Host "首次运行，生成演示数据…" -ForegroundColor Cyan
    python -m server.app.seed
}

Write-Host "启动 http://127.0.0.1:8765" -ForegroundColor Green
python -m uvicorn server.app.main:app --host 127.0.0.1 --port 8765 --reload
