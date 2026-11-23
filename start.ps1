<#
.SYNOPSIS
    DocHub 企业级知识库平台 — Windows 一键启动脚本

.DESCRIPTION
    一键完成以下初始化与启动流程：
    1. 校验 Docker Desktop 运行状态并确保 .env 已就绪（严禁输出或泄露任何密钥）；
    2. 按需启动 Docker Compose 数据底座（PostgreSQL 17 / Redis 7 / MinIO / Qdrant）及可选可观测性栈；
    3. 自动解析或创建后端 Python 虚拟环境，执行 Alembic 单头数据库迁移；
    4. 检测数据库是否为空并自动执行基础种子数据初始化（支持 -SeedDemo 灌入全场景演示数据）；
    5. 探测本机可选 GPU 本地 BGE 模型服务（8081 / 8082）状态；
    6. 启动 FastAPI 后端（8000）、Celery Worker（5 队列）与 Vite 前端（5173），等待探针就绪并汇总访问入口。

.PARAMETER Mode
    运行模式：dev（默认：Docker 底座 + 本机开发热启）或 docker（全栈 Docker 容器化启动）。

.PARAMETER WithObservability
    在 dev 模式下额外拉起 Prometheus / Grafana / Loki / Tempo / OTel 可观测性栈（Grafana: 13000）。

.PARAMETER SeedDemo
    幂等执行基础种子与全场景演示数据灌入（文档、Qdrant 向量、FAQ、Gap、审计日志）。

.PARAMETER NoBrowser
    启动就绪后不自动打开默认浏览器。

.EXAMPLE
    .\start.ps1
    .\start.ps1 -SeedDemo
    .\start.ps1 -WithObservability -NoBrowser
    .\start.ps1 -Mode docker
#>

[CmdletBinding()]
param(
    [ValidateSet("dev", "docker")]
    [string]$Mode = "dev",
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    [switch]$WithObservability,
    [switch]$SeedDemo,
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$BackendDir = Join-Path $RootDir "backend"
$FrontendDir = Join-Path $RootDir "frontend"
$ComposeFile = Join-Path $RootDir "docker-compose.yml"
$EnvFile = Join-Path $RootDir ".env"
$EnvExampleFile = Join-Path $RootDir ".env.example"
$TmpDir = Join-Path $RootDir "tmp"
$LogDir = Join-Path $TmpDir "logs"
$PidFile = Join-Path $TmpDir "dochub-pids.json"

function Write-Step {
    param([string]$Message)
    Write-Host "`n[DocHub] $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  [WARN] $Message" -ForegroundColor Yellow
}

function Test-HttpOk {
    param(
        [string]$Url,
        [int]$TimeoutSec = 3
    )
    try {
        $resp = Invoke-WebRequest -Uri $Url -Method Get -TimeoutSec $TimeoutSec -UseBasicParsing
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400)
    } catch {
        return $false
    }
}

function Wait-ForHttpReady {
    param(
        [string]$Name,
        [string]$Url,
        [int]$MaxWaitSeconds = 60
    )
    $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpOk -Url $Url -TimeoutSec 3) {
            Write-Ok "$Name 已就绪 -> $Url"
            return $true
        }
        Start-Sleep -Seconds 1
    }
    Write-Warn "$Name 在 ${MaxWaitSeconds}s 内尚未响应就绪探针 ($Url)，请检查 $LogDir 下日志。"
    return $false
}

function Resolve-BackendPython {
    # 优先使用仓库内 backend\.venv，兼容历史 worktree 虚拟环境缓存，否则自动创建并安装依赖
    $candidates = @(
        (Join-Path $BackendDir ".venv\Scripts\python.exe"),
        "D:\DocHub-wt\backend-core\backend\.venv\Scripts\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    Write-Step "未检测到后端虚拟环境，正在创建 backend\.venv 并安装依赖..."
    $sysPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sysPython) {
        throw "未在 PATH 中找到 Python 3.12+，请先安装 Python 3.12 并加入环境变量。"
    }
    & $sysPython.Source -m venv (Join-Path $BackendDir ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "创建 Python 虚拟环境失败。"
    }
    $venvPython = Join-Path $BackendDir ".venv\Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r (Join-Path $BackendDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "安装后端 Python 依赖失败。"
    }
    return $venvPython
}

function Test-PortInUse {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $conn)
}

function Stop-ExistingDevProcesses {
    if (Test-Path $PidFile) {
        try {
            $record = Get-Content -Path $PidFile -Raw | ConvertFrom-Json
            foreach ($procName in @("backend", "celery", "frontend")) {
                $pidVal = $record.$procName
                if ($pidVal) {
                    $existing = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
                    if ($existing) {
                        Write-Host "  停止旧进程 $procName (PID=$pidVal)..." -ForegroundColor DarkGray
                        & taskkill /PID $pidVal /T /F 2>$null | Out-Null
                    }
                }
            }
        } catch {
            # 忽略损坏或过期的 PID 文件
        }
        Remove-Item -Path $PidFile -Force -ErrorAction SilentlyContinue
    }

    # 仅清理属于 DocHub 仓库自身的残留本地开发进程（绝不误杀本机其他项目的端口服务）
    $docHubProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and ($_.CommandLine -like "*DocHub*") -and (
            $_.CommandLine -like "*uvicorn*" -or
            $_.CommandLine -like "*celery*" -or
            $_.CommandLine -like "*vite*"
        )
    }
    foreach ($dp in $docHubProcs) {
        if ($dp.ProcessId -ne $PID) {
            Write-Host "  清理残留 DocHub 进程 ($($dp.Name), PID=$($dp.ProcessId))..." -ForegroundColor DarkGray
            & taskkill /PID $dp.ProcessId /T /F 2>$null | Out-Null
        }
    }
    Start-Sleep -Milliseconds 500
}

# ==============================================================================
# 主启动流程
# ==============================================================================
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  DocHub Enterprise Knowledge Base Platform 一键启动 ($Mode)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. 检查 Docker Desktop
Write-Step "1/6 检查 Docker Desktop 与环境配置文件..."
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop 未启动或不可用，请先打开 Docker Desktop 后重试。"
}
Write-Ok "Docker Desktop 运行正常"

if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExampleFile) {
        Copy-Item -Path $EnvExampleFile -Destination $EnvFile
        Write-Warn "未找到 .env，已自动从 .env.example 复制生成。请按需配置 LLM / MinerU / Embedding API Key。"
    } else {
        throw "缺少 .env 与 .env.example 文件，无法启动。"
    }
} else {
    Write-Ok "已检测到本地 .env 配置文件（密钥已受保护，不输出明文）"
}

# 2. Docker 全栈容器模式分支
if ($Mode -eq "docker") {
    Write-Step "2/6 启动全栈 Docker Compose (profile: full-demo)..."
    & docker compose -f $ComposeFile --profile full-demo up -d --build
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose full-demo 启动失败。"
    }
    Wait-ForHttpReady -Name "FastAPI Backend" -Url "http://127.0.0.1:8000/api/v1/readyz" -MaxWaitSeconds 90 | Out-Null
    Wait-ForHttpReady -Name "Frontend Web" -Url "http://127.0.0.1:5173/" -MaxWaitSeconds 60 | Out-Null
    Write-Host "`n全部容器已就绪：前端 http://127.0.0.1:5173 | API 文档 http://127.0.0.1:8000/docs | Grafana http://127.0.0.1:13000" -ForegroundColor Green
    if (-not $NoBrowser) {
        Start-Process "http://127.0.0.1:5173"
    }
    exit 0
}

# 3. dev 模式：启动核心数据底座（PostgreSQL 17 / Redis 7 / MinIO / Qdrant）
Write-Step "2/6 启动核心基础设施容器 (PostgreSQL 17 / Redis 7 / MinIO / Qdrant)..."
$composeArgs = @("-f", $ComposeFile, "--profile", "core")
if ($WithObservability) {
    $composeArgs += @("--profile", "observability")
}
$composeArgs += @("up", "-d")
& docker compose @composeArgs
if ($LASTEXITCODE -ne 0) {
    throw "Docker 核心容器启动失败，请执行 docker compose -f docker-compose.yml --profile core ps 排查。"
}

# 等待 4 个常驻核心容器全部 Healthy（避免 dochub-minio-init 一次性初始化退出 0 触发 compose --wait 误报）
$coreContainers = @("dochub-postgres", "dochub-redis", "dochub-minio", "dochub-qdrant")
$healthDeadline = (Get-Date).AddSeconds(60)
$allHealthy = $false
while ((Get-Date) -lt $healthDeadline) {
    $unhealthy = @()
    foreach ($cname in $coreContainers) {
        $status = (& docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $cname 2>$null)
        if ($status -notin @("healthy", "running")) {
            $unhealthy += "${cname}:${status}"
        }
    }
    if ($unhealthy.Count -eq 0) {
        $allHealthy = $true
        break
    }
    Start-Sleep -Seconds 1
}
if (-not $allHealthy) {
    throw "部分核心容器未在 60s 内就绪：$($unhealthy -join ', ')。请执行 docker compose -f docker-compose.yml --profile core ps 排查。"
}
Write-Ok "核心基础设施容器全部 Healthy"

# 4. 解析 Python 环境并执行 Alembic 单头迁移与种子检查
Write-Step "3/6 校验后端环境并执行 Alembic 数据库迁移..."
$PythonExe = Resolve-BackendPython
Write-Ok "后端 Python 解释器：$PythonExe"

Push-Location $BackendDir
try {
    & $PythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        throw "Alembic 数据库迁移失败。"
    }
    Write-Ok "Alembic 迁移已同步至最新单头版本"

    # 检查 users 表是否为空；为空则自动执行基础幂等种子，指定 -SeedDemo 则同步灌入演示数据
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $userCountRaw = & docker exec dochub-postgres psql -U app_migrator -d dochub -tAc "SELECT count(*) FROM users;" 2>$null
    $ErrorActionPreference = $prevEap
    $userCount = 0
    if ($userCountRaw) {
        [int]::TryParse($userCountRaw.Trim(), [ref]$userCount) | Out-Null
    }

    if ($userCount -eq 0 -or $SeedDemo) {
        Write-Step "4/6 初始化基础组织、角色与演示账号数据..."
        & $PythonExe -m app.db.seed
        if ($LASTEXITCODE -ne 0) {
            throw "基础种子数据初始化失败。"
        }
        Write-Ok "基础种子数据初始化完成（acme-corp / beta-corp 与 6 个演示账号）"

        if ($SeedDemo) {
            Write-Host "  正在幂等灌入全场景演示文档、向量、FAQ、Gap 与审计日志..." -ForegroundColor Cyan
            & $PythonExe -m app.db.seed_demo
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "seed_demo 灌入遇到异常（请检查本地 BGE 或 Qwen Embedding 服务是否可达），主应用仍可继续启动。"
            } else {
                Write-Ok "全场景演示数据灌入完成"
            }
        }
    } else {
        Write-Step "4/6 检测到数据库已存在 $userCount 个账号，跳过重复基础初始化（如需重置演示数据可加 -SeedDemo）"
    }
} finally {
    Pop-Location
}

# 5. 检查前端依赖与可选本地 BGE 服务状态
Write-Step "5/6 校验前端依赖并探测可选本地模型服务..."
if (-not (Test-Path (Join-Path $FrontendDir "node_modules"))) {
    Write-Host "  正在安装前端 npm 依赖..." -ForegroundColor Cyan
    Push-Location $FrontendDir
    try {
        & cmd /c "npm install"
        if ($LASTEXITCODE -ne 0) {
            throw "前端 npm install 失败。"
        }
    } finally {
        Pop-Location
    }
}
Write-Ok "前端 node_modules 已就绪"

$bgeEmbedUp = (Test-HttpOk -Url "http://127.0.0.1:8081/health" -TimeoutSec 2) -or (Test-HttpOk -Url "http://127.0.0.1:8081/v1/models" -TimeoutSec 2)
$bgeRerankUp = (Test-HttpOk -Url "http://127.0.0.1:8082/health" -TimeoutSec 2) -or (Test-HttpOk -Url "http://127.0.0.1:8082/v1/models" -TimeoutSec 2)
if ($bgeEmbedUp) {
    Write-Ok "本地 BGE Embedding (127.0.0.1:8081) 在线"
} else {
    Write-Warn "本地 BGE Embedding (127.0.0.1:8081) 未检测到响应（若使用 Qwen API 请忽略）"
}
if ($bgeRerankUp) {
    Write-Ok "本地 BGE Reranker  (127.0.0.1:8082) 在线"
} else {
    Write-Warn "本地 BGE Reranker  (127.0.0.1:8082) 未检测到响应（若使用 Qwen API 请忽略）"
}

# 6. 启动 FastAPI 后端、Celery Worker 与 Vite 前端
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Stop-ExistingDevProcesses

# 若默认端口已被本机其他项目占用，自动回退到无冲突端口并同步前端 Vite 代理
if (Test-PortInUse -Port $BackendPort) {
    $fallbackBackend = 8010
    while (Test-PortInUse -Port $fallbackBackend) {
        $fallbackBackend++
    }
    Write-Warn "检测到 127.0.0.1:$BackendPort 已被其他本机服务占用，DocHub 后端自动切换至无冲突端口 $fallbackBackend（不干扰其他项目）"
    $BackendPort = $fallbackBackend
}
if (Test-PortInUse -Port $FrontendPort) {
    $fallbackFrontend = 5174
    while (Test-PortInUse -Port $fallbackFrontend) {
        $fallbackFrontend++
    }
    Write-Warn "检测到 127.0.0.1:$FrontendPort 已被其他本机服务占用，DocHub 前端自动切换至无冲突端口 $fallbackFrontend"
    $FrontendPort = $fallbackFrontend
}

Write-Step "6/6 启动 FastAPI ($BackendPort)、Celery Worker 与 Vite 前端 ($FrontendPort)..."

$backendProc = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput (Join-Path $LogDir "backend.out.log") `
    -RedirectStandardError (Join-Path $LogDir "backend.err.log") `
    -WindowStyle Hidden `
    -PassThru

$celeryProc = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList @(
        "-m", "celery", "-A", "app.workers.celery_app:celery_app",
        "worker", "--loglevel=info", "--pool=solo",
        "-Q", "default,ingestion,indexing,faq,outbox"
    ) `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput (Join-Path $LogDir "celery.out.log") `
    -RedirectStandardError (Join-Path $LogDir "celery.err.log") `
    -WindowStyle Hidden `
    -PassThru

# 同步前端代理目标至当前选定的 DocHub 后端端口
$env:VITE_API_PROXY_TARGET = "http://127.0.0.1:$BackendPort"
$frontendProc = Start-Process `
    -FilePath "cmd.exe" `
    -ArgumentList @("/c", "npm run dev -- --host 127.0.0.1 --port $FrontendPort") `
    -WorkingDirectory $FrontendDir `
    -RedirectStandardOutput (Join-Path $LogDir "frontend.out.log") `
    -RedirectStandardError (Join-Path $LogDir "frontend.err.log") `
    -WindowStyle Hidden `
    -PassThru

$pidPayload = [ordered]@{
    started_at    = (Get-Date).ToString("o")
    mode          = $Mode
    backend_port  = $BackendPort
    frontend_port = $FrontendPort
    backend       = $backendProc.Id
    celery        = $celeryProc.Id
    frontend      = $frontendProc.Id
}
$pidPayload | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

$backendReady = Wait-ForHttpReady -Name "FastAPI Backend" -Url "http://127.0.0.1:$BackendPort/api/v1/readyz" -MaxWaitSeconds 60
$frontendReady = Wait-ForHttpReady -Name "Vite Frontend" -Url "http://127.0.0.1:$FrontendPort/" -MaxWaitSeconds 45

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host "  DocHub 平台启动完成！" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  前端工作台  : http://127.0.0.1:$FrontendPort"
Write-Host "  后端 Swagger: http://127.0.0.1:$BackendPort/docs"
Write-Host "  就绪探针    : http://127.0.0.1:$BackendPort/api/v1/readyz"
Write-Host "  MinIO 控制台: http://127.0.0.1:19001"
Write-Host "  Qdrant 面板 : http://127.0.0.1:6333/dashboard"
if ($WithObservability) {
    Write-Host "  Grafana 看板: http://127.0.0.1:13000"
}
Write-Host "  运行日志目录: $LogDir"
Write-Host "  一键停止脚本: .\stop.ps1  (或双击 stop.bat)"
Write-Host "------------------------------------------------------------" -ForegroundColor DarkGray
Write-Host "  内置演示账号（默认密码 DocHubDemo#2026!，支持登录页自助注册）：" -ForegroundColor Gray
Write-Host "    系统管理员 : admin@acme.local   (平台账号/组织/空间管理)" -ForegroundColor Gray
Write-Host "    知识管理员 : km@acme.local      (文档/ACL/FAQ/Gap/业务审计)" -ForegroundColor Gray
Write-Host "    普通员工   : employee@acme.local (受 ACL 保护的 AI 问答)" -ForegroundColor Gray
Write-Host "    安全审计员 : auditor@acme.local  (业务 + 安全双审计中心)" -ForegroundColor Gray
Write-Host "============================================================`n" -ForegroundColor Green

if (-not $backendReady -or -not $frontendReady) {
    Write-Warn "部分服务未在超时内就绪，请查看 $LogDir\*.err.log 定位详情。"
    exit 1
}

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:$FrontendPort"
}
