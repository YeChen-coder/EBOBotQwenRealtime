# EBO 通义千问助手：一键启动（Docker、Engine、Frigate、MQTT、助手），然后打开看板
$ErrorActionPreference = 'Continue'
$Project = Split-Path -Parent $PSScriptRoot
$Dashboard = 'http://127.0.0.1:8099/'
$Panel = 'http://127.0.0.1:8101/'
$DockerDesktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
$Host.UI.RawUI.WindowTitle = 'EBO 助手 - 启动'

function Say($Message, $Color = 'Gray') { Write-Host $Message -ForegroundColor $Color }
function Fail($Message) {
    Say $Message Red
    Read-Host '按回车键关闭'
    exit 1
}
function DockerReady { docker info *> $null; return $LASTEXITCODE -eq 0 }

Say '== EBO 通义千问助手：启动 ==' Cyan

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail '没有找到 Docker。请先安装 Docker Desktop。'
}

if (-not (DockerReady)) {
    if (-not (Test-Path $DockerDesktop)) { Fail "没有找到 Docker Desktop：$DockerDesktop" }
    Say '正在启动 Docker Desktop（第一次可能要 1-2 分钟）…'
    Start-Process $DockerDesktop
    $deadline = (Get-Date).AddMinutes(4)
    while (-not (DockerReady)) {
        if ((Get-Date) -gt $deadline) { Fail 'Docker Desktop 4 分钟内没有就绪。请手动打开 Docker Desktop 看看提示。' }
        Write-Host '.' -NoNewline
        Start-Sleep -Seconds 3
    }
    Write-Host ''
}
Say 'Docker 已就绪' Green

# 旧项目 ebo-ai-home 的 Engine 会和本项目抢同一台机器人，必须停掉
$old = @(docker ps --format '{{.Names}}' | Where-Object { $_ -like 'ebo-ai-home-*' })
if ($old.Count -gt 0) {
    Say ("停止旧项目 ebo-ai-home 的容器：" + ($old -join ', ')) Yellow
    docker stop $old | Out-Null
}

Say '正在启动 Engine、Frigate、MQTT 和助手…'
Push-Location $Project
docker compose up -d 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match 'Started|Running|Created|Built|error|Error' } | ForEach-Object { Say "  $_" }
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { Fail '启动失败，请看上面的错误信息。' }

Say '等待助手就绪…'
$health = $null
$deadline = (Get-Date).AddMinutes(3)
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-RestMethod -Uri ($Dashboard + 'health') -TimeoutSec 3
        if ($health.ok) { break }
    } catch { $health = $null }
    Write-Host '.' -NoNewline
    Start-Sleep -Seconds 3
}
Write-Host ''

if ($null -eq $health) {
    Say '助手程序没有响应，仍然打开看板，请在看板和日志里查看原因。' Yellow
} elseif ($health.ok) {
    Say '全部就绪：机器人麦克风正常，正在待机监听。' Green
} else {
    Say '助手已启动，但还有部分没就绪（比如机器人在休眠），看板里会显示具体原因。' Yellow
}

Start-Process $Dashboard
Say "看板：$Dashboard"
Say "机器人控制面板：$Panel"
Say '这个窗口 10 秒后自动关闭。'
Start-Sleep -Seconds 10
