<#
.SYNOPSIS
    DocHub 企业级知识库平台 — Windows 一键停止脚本

.DESCRIPTION
    1. 优雅终止由 start.ps1 启动的 FastAPI 后端、Celery Worker 与 Vite 前端进程树；
    2. 如传入 -StopDocker，仅停止本项目的 Docker Compose 容器（绝不影响本机其他 Docker 项目）。

.PARAMETER StopDocker
    同步停止本项目 docker-compose.yml 定义的 Docker 容器（保留 Named Volumes 数据卷）。

.EXAMPLE
    .\stop.ps1
    .\stop.ps1 -StopDocker
#>

[CmdletBinding()]
param(
    [switch]$StopDocker
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$ComposeFile = Join-Path $RootDir "docker-compose.yml"
$PidFile = Join-Path $RootDir "tmp\dochub-pids.json"

Write-Host "[DocHub] 正在停止本地应用进程..." -ForegroundColor Cyan

$stoppedAny = $false
if (Test-Path $PidFile) {
    try {
        $record = Get-Content -Path $PidFile -Raw | ConvertFrom-Json
        foreach ($procName in @("frontend", "celery", "backend")) {
            $pidVal = $record.$procName
            if ($pidVal) {
                $proc = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "  [STOP] 终止 $procName 进程树 (PID=$pidVal)..." -ForegroundColor Yellow
                    & taskkill /PID $pidVal /T /F 2>$null | Out-Null
                    $stoppedAny = $true
                }
            }
        }
    } catch {
        Write-Host "  [WARN] 读取 PID 文件异常，将清理残留记录。" -ForegroundColor Yellow
    }
    Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
}

# 兜底清理仅属于 DocHub 仓库自身的残留进程（绝不误杀本机其他项目）
$docHubProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and ($_.CommandLine -like "*DocHub*") -and (
        $_.CommandLine -like "*uvicorn*" -or
        $_.CommandLine -like "*celery*" -or
        $_.CommandLine -like "*vite*"
    )
}
foreach ($dp in $docHubProcs) {
    if ($dp.ProcessId -ne $PID) {
        Write-Host "  [STOP] 清理残留 DocHub 进程 ($($dp.Name), PID=$($dp.ProcessId))..." -ForegroundColor Yellow
        & taskkill /PID $dp.ProcessId /T /F 2>$null | Out-Null
        $stoppedAny = $true
    }
}

if (-not $stoppedAny) {
    Write-Host "  [OK] 当前未发现运行中的 DocHub 本地应用进程。" -ForegroundColor Green
} else {
    Write-Host "  [OK] DocHub 本地应用进程已全部停止。" -ForegroundColor Green
}

if ($StopDocker) {
    Write-Host "`n[DocHub] 正在停止 DocHub Docker 容器（保留持久化数据卷，不影响其他项目）..." -ForegroundColor Cyan
    & docker compose -f $ComposeFile --profile full-demo down
    if ($LASTEXITCODE -ne 0) {
        throw "停止 Docker 容器失败。"
    }
    Write-Host "  [OK] DocHub Docker 容器已全部停止。" -ForegroundColor Green
} else {
    Write-Host "  [TIP] Docker 数据底座（PG/Redis/MinIO/Qdrant）仍保持运行；如需一并停止请执行 .\stop.ps1 -StopDocker" -ForegroundColor DarkGray
}
