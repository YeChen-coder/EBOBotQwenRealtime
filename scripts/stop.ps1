# EBO 通义千问助手：停止全部组件（数据和配置保留，下次启动继续用）
$ErrorActionPreference = 'Continue'
$Project = Split-Path -Parent $PSScriptRoot
$Host.UI.RawUI.WindowTitle = 'EBO 助手 - 停止'

Write-Host '== EBO 通义千问助手：停止 ==' -ForegroundColor Cyan
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Docker 没有在运行，无需停止。' -ForegroundColor Green
    Start-Sleep -Seconds 5
    exit 0
}

Push-Location $Project
docker compose stop 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match 'Stopped|error|Error' } | ForEach-Object { Write-Host "  $_" }
Pop-Location
Write-Host '已停止。机器人不会再被助手控制或监听。' -ForegroundColor Green
Write-Host '这个窗口 5 秒后自动关闭。'
Start-Sleep -Seconds 5
