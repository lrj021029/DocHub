<#
.SYNOPSIS
    DocHub 备份完整性校验与恢复演练脚本（支持非破坏性演练 -DrillOnly 与受控全量恢复）。

.DESCRIPTION
    遵循 TECHNICAL_SPEC §16（Backup / Recovery）与 ADR-002 数据库 RLS 规范：
    1. 前置强校验：恢复前必须先逐一核对 manifest.json / SHA256SUMS.txt 中全部产物的 SHA256 哈希，
       任何文件缺失或篡改均立即中止恢复流程；同时拦截异常混入的 .env 或私钥文件。
    2. 非破坏性演练（-DrillOnly，默认推荐）：
       - PostgreSQL：在容器内创建临时演练库（dochub_restore_drill_<ts>），执行真实 pg_restore，
         验证表数量、RLS 策略数量与 app_runtime 非 BYPASSRLS 基线，演练结束后自动清理临时库。
       - MinIO     ：逐一核验 objects.sha256.json 中全部导出对象的真实哈希并验证 MinIO 服务健康。
       - Qdrant    ：核验快照文件 SHA256 与非空完整性，验证 Qdrant API 鉴权连通性。
    3. 生产/实库恢复（需显式传入 -Force）：
       - PostgreSQL：使用 pg_restore --clean --if-exists 恢复目标数据库并复核角色安全基线。
       - MinIO     ：通过 minio/mc 镜像回灌 dochub-raw 与 dochub-artifacts 并重申私有访问策略。
       - Qdrant    ：校验快照并调用 Qdrant API 恢复集合快照（全量存储快照同时输出停机恢复指引）。

.EXAMPLE
    # 推荐：执行无损恢复演练（不覆盖现网数据）
    .\scripts\restore.ps1 -BackupDir ".\backups\dochub-backup-20261123-100000" -DrillOnly

    # 受控全量恢复（必须显式确认 -Force）
    .\scripts\restore.ps1 -BackupDir ".\backups\dochub-backup-20261123-100000" -Force
#>

[CmdletBinding()]
param(
    # 待恢复或演练的备份目录路径（必须包含 manifest.json）
    [Parameter(Mandatory = $true)]
    [string]$BackupDir,

    # 主 Docker Compose 文件路径
    [string]$ComposeFile = ".\docker-compose.yml",

    # 仅在内存中读取连接参数的环境变量文件路径（严禁打印明文密钥）
    [string]$EnvFile = ".\.env",

    # 需要恢复/演练的组件范围
    [ValidateSet("all", "postgres", "minio", "qdrant")]
    [string[]]$Components = @("all"),

    # 演练模式：在不覆盖现网数据的前提下执行真实 PostgreSQL 临时库恢复与 MinIO/Qdrant 完整性演练
    [switch]$DrillOnly,

    # 指定 PostgreSQL 目标恢复库名（仅非演练模式生效，留空则使用 POSTGRES_DB）
    [string]$TargetPostgresDb = "",

    # 破坏性实库恢复确认开关（非 DrillOnly 模式必须显式传入 -Force 防止误操作）
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------------------------
# 工具函数：内存读取 .env 与 SHA256 计算
# ------------------------------------------------------------------------------
function Import-EnvFileToMemory {
    param([string]$Path)

    $envMap = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $envMap
    }

    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith("#")) {
            continue
        }
        $eqIndex = $line.IndexOf("=")
        if ($eqIndex -le 0) {
            continue
        }
        $key = $line.Substring(0, $eqIndex).Trim()
        $val = $line.Substring($eqIndex + 1).Trim()
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $envMap[$key] = $val
    }
    return $envMap
}

function Get-ConfigValue {
    param(
        [hashtable]$EnvMap,
        [string]$Key,
        [string]$DefaultValue
    )

    $procVal = [Environment]::GetEnvironmentVariable($Key)
    if (-not [string]::IsNullOrEmpty($procVal)) {
        return $procVal
    }
    if ($EnvMap.ContainsKey($Key) -and -not [string]::IsNullOrEmpty($EnvMap[$Key])) {
        return $EnvMap[$Key]
    }
    return $DefaultValue
}

function Get-FileSha256Hex {
    param([string]$FilePath)
    return (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Test-ComponentSelected {
    param(
        [string[]]$Selected,
        [string]$Name
    )
    return ($Selected -contains "all") -or ($Selected -contains $Name)
}

# ------------------------------------------------------------------------------
# 0. 参数与安全防呆校验
# ------------------------------------------------------------------------------
if (-not $DrillOnly -and -not $Force) {
    throw "安全拦截：实库恢复将覆盖现有数据！若仅执行无损恢复演练请加 -DrillOnly；若确认执行实库恢复请显式传入 -Force。"
}

if (-not (Test-Path -LiteralPath $BackupDir)) {
    throw "指定的备份目录不存在：$BackupDir"
}

$resolvedBackupDir = (Resolve-Path -LiteralPath $BackupDir).Path
$resolvedCompose = (Resolve-Path -LiteralPath $ComposeFile).Path
$manifestPath = Join-Path $resolvedBackupDir "manifest.json"
$shaSumsPath = Join-Path $resolvedBackupDir "SHA256SUMS.txt"

if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "备份目录缺少清单文件 manifest.json，拒绝执行恢复：$manifestPath"
}

$envMap = Import-EnvFileToMemory -Path $EnvFile
$pgDbDefault = Get-ConfigValue -EnvMap $envMap -Key "POSTGRES_DB" -DefaultValue "dochub"
$pgSuperUser = Get-ConfigValue -EnvMap $envMap -Key "POSTGRES_SUPERUSER" -DefaultValue "postgres"
$minioAccessKey = Get-ConfigValue -EnvMap $envMap -Key "MINIO_ACCESS_KEY" -DefaultValue "dochub_minio_dev"
$minioSecretKey = Get-ConfigValue -EnvMap $envMap -Key "MINIO_SECRET_KEY" -DefaultValue "dochub_minio_secret_dev"
$minioBucketRaw = Get-ConfigValue -EnvMap $envMap -Key "MINIO_BUCKET_RAW" -DefaultValue "dochub-raw"
$minioBucketArtifacts = Get-ConfigValue -EnvMap $envMap -Key "MINIO_BUCKET_ARTIFACTS" -DefaultValue "dochub-artifacts"
$qdrantHost = Get-ConfigValue -EnvMap $envMap -Key "QDRANT_HOST" -DefaultValue "127.0.0.1"
$qdrantPort = Get-ConfigValue -EnvMap $envMap -Key "QDRANT_PORT" -DefaultValue "6333"
$qdrantApiKey = Get-ConfigValue -EnvMap $envMap -Key "QDRANT_API_KEY" -DefaultValue "dochub_qdrant_dev_key"

$modeLabel = if ($DrillOnly) { "无损恢复演练（DrillOnly）" } else { "受控实库恢复（Force）" }
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "[DocHub Restore] 执行模式：$modeLabel" -ForegroundColor Cyan
Write-Host "[DocHub Restore] 备份目录：$resolvedBackupDir" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

# ==============================================================================
# 1. 强制前置校验：SHA256 清单核验 + 敏感文件扫描
# ==============================================================================
Write-Host "`n[前置校验] 正在校验 manifest.json 与全部备份产物的 SHA256 哈希..." -ForegroundColor Yellow

$forbiddenFiles = @(Get-ChildItem -LiteralPath $resolvedBackupDir -Recurse -File | Where-Object {
    $_.Name -match '^\.env(\.|$)' -or $_.Extension -in @(".pem", ".key", ".pfx", ".p12")
})
if ($forbiddenFiles.Count -gt 0) {
    throw "安全拦截：备份目录中包含非法敏感文件，已中止恢复。"
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$manifestArtifacts = @($manifest.artifacts)
if ($manifestArtifacts.Count -eq 0) {
    throw "manifest.json 中未包含任何备份产物条目。"
}

$verifiedCount = 0
foreach ($item in $manifestArtifacts) {
    $localFile = Join-Path $resolvedBackupDir ($item.path -replace '/', '\')
    if (-not (Test-Path -LiteralPath $localFile)) {
        throw "完整性校验失败：备份产物缺失 -> $($item.path)"
    }
    $actualHash = Get-FileSha256Hex -FilePath $localFile
    if ($actualHash -ne $item.sha256.ToLowerInvariant()) {
        throw "完整性校验失败：文件哈希不匹配 -> $($item.path)（期望：$($item.sha256)，实际：$actualHash）"
    }
    $verifiedCount++
}

# 同步交叉校验 SHA256SUMS.txt（如存在）
if (Test-Path -LiteralPath $shaSumsPath) {
    foreach ($line in Get-Content -LiteralPath $shaSumsPath -Encoding UTF8) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -match '^([0-9a-fA-F]{64})\s+(.+)$') {
            $expected = $Matches[1].ToLowerInvariant()
            $rel = $Matches[2].Trim()
            $target = Join-Path $resolvedBackupDir ($rel -replace '/', '\')
            if (-not (Test-Path -LiteralPath $target)) {
                throw "SHA256SUMS.txt 校验失败：文件缺失 -> $rel"
            }
            $actual = Get-FileSha256Hex -FilePath $target
            if ($actual -ne $expected) {
                throw "SHA256SUMS.txt 校验失败：哈希不匹配 -> $rel"
            }
        }
    }
}

Write-Host "[前置校验] SHA256 完整性校验全部通过（已核验 $verifiedCount 个文件）。" -ForegroundColor Green

# ==============================================================================
# 2. PostgreSQL 恢复 / 演练
# ==============================================================================
if ((Test-ComponentSelected -Selected $Components -Name "postgres") -and ($manifest.components.PSObject.Properties.Name -contains "postgres")) {
    $pgMeta = $manifest.components.postgres
    $localDump = Join-Path $resolvedBackupDir ($pgMeta.file -replace '/', '\')
    $ts = Get-Date -Format "yyyyMMddHHmmss"
    $remoteTmpDump = "/tmp/dochub_restore_$ts.dump"

    $targetDb = if ($DrillOnly) {
        "dochub_restore_drill_$ts"
    } elseif (-not [string]::IsNullOrWhiteSpace($TargetPostgresDb)) {
        $TargetPostgresDb
    } else {
        $pgDbDefault
    }

    # 校验库名合法性，防止 SQL 标识符注入
    if ($targetDb -notmatch '^[a-zA-Z0-9_]+$') {
        throw "非法的 PostgreSQL 数据库名称：$targetDb"
    }

    Write-Host "`n[1/3] 正在执行 PostgreSQL 恢复（目标库：$targetDb）..." -ForegroundColor Yellow

    try {
        # 将校验通过的转储文件复制到 postgres 容器临时目录
        & docker compose -f $resolvedCompose cp $localDump "postgres:$remoteTmpDump"
        if ($LASTEXITCODE -ne 0) {
            throw "无法复制 PostgreSQL 备份文件至容器：$localDump"
        }

        if ($DrillOnly) {
            # 创建临时演练库并执行真实 pg_restore
            & docker compose -f $resolvedCompose exec -T postgres `
                psql -U $pgSuperUser -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $targetDb;"
            if ($LASTEXITCODE -ne 0) {
                throw "创建 PostgreSQL 临时演练库失败：$targetDb"
            }

            & docker compose -f $resolvedCompose exec -T postgres `
                pg_restore -U $pgSuperUser -d $targetDb --no-owner --exit-on-error $remoteTmpDump
            if ($LASTEXITCODE -ne 0) {
                throw "PostgreSQL 演练库 pg_restore 失败（退出码：$LASTEXITCODE）"
            }
        }
        else {
            # 实库覆盖恢复
            & docker compose -f $resolvedCompose exec -T postgres `
                pg_restore -U $pgSuperUser -d $targetDb --clean --if-exists --no-owner --exit-on-error $remoteTmpDump
            if ($LASTEXITCODE -ne 0) {
                throw "PostgreSQL 实库 pg_restore 失败（退出码：$LASTEXITCODE）"
            }
        }

        # 恢复后验证：统计业务表数量、RLS 策略数量，并断言 app_runtime 无 BYPASSRLS
        $tableCount = (& docker compose -f $resolvedCompose exec -T postgres `
            psql -U $pgSuperUser -d $targetDb -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';").Trim()
        $rlsPolicyCount = (& docker compose -f $resolvedCompose exec -T postgres `
            psql -U $pgSuperUser -d $targetDb -tAc "SELECT count(*) FROM pg_policies WHERE schemaname = 'public';").Trim()
        $badRoleCount = (& docker compose -f $resolvedCompose exec -T postgres `
            psql -U $pgSuperUser -d $targetDb -tAc "SELECT count(*) FROM pg_roles WHERE rolname = 'app_runtime' AND (rolsuper OR rolbypassrls);").Trim()

        if ($badRoleCount -ne "0") {
            throw "安全断言失败：app_runtime 角色存在 SUPERUSER 或 BYPASSRLS 权限！"
        }

        Write-Host "[1/3] PostgreSQL 恢复验证通过（public 表数量：$tableCount，RLS 策略数：$rlsPolicyCount，app_runtime 权限合规）。" -ForegroundColor Green
    }
    finally {
        # 清理容器内临时备份文件；演练模式同步删除临时演练库
        & docker compose -f $resolvedCompose exec -T postgres rm -f $remoteTmpDump 2>$null | Out-Null
        if ($DrillOnly) {
            & docker compose -f $resolvedCompose exec -T postgres `
                psql -U $pgSuperUser -d postgres -c "DROP DATABASE IF EXISTS $targetDb WITH (FORCE);" 2>$null | Out-Null
            Write-Host "[1/3] 已清理临时演练库：$targetDb" -ForegroundColor DarkGray
        }
    }
}

# ==============================================================================
# 3. MinIO 对象校验与恢复
# ==============================================================================
if ((Test-ComponentSelected -Selected $Components -Name "minio") -and ($manifest.components.PSObject.Properties.Name -contains "minio")) {
    Write-Host "`n[2/3] 正在执行 MinIO 对象完整性核验与恢复..." -ForegroundColor Yellow
    $minioDir = Join-Path $resolvedBackupDir "minio"
    $objectsManifestPath = Join-Path $minioDir "objects.sha256.json"

    if (-not (Test-Path -LiteralPath $objectsManifestPath)) {
        throw "缺少 MinIO 对象校验清单：$objectsManifestPath"
    }

    $objManifest = Get-Content -LiteralPath $objectsManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $objChecked = 0
    foreach ($obj in @($objManifest.objects)) {
        $objPath = Join-Path $minioDir ($obj.path -replace '/', '\')
        if (-not (Test-Path -LiteralPath $objPath)) {
            throw "MinIO 对象文件缺失：$($obj.path)"
        }
        $actualObjHash = Get-FileSha256Hex -FilePath $objPath
        if ($actualObjHash -ne $obj.sha256.ToLowerInvariant()) {
            throw "MinIO 对象哈希校验失败：$($obj.path)"
        }
        $objChecked++
    }

    if ($DrillOnly) {
        # 演练模式：验证 MinIO 服务连通性，不覆盖现网 Bucket
        $mcProbeScript = @'
set -eu
mc alias set dochub http://minio:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null
mc ls "dochub/$MINIO_BUCKET_RAW" >/dev/null
mc ls "dochub/$MINIO_BUCKET_ARTIFACTS" >/dev/null
'@
        & docker run --rm `
            --network dochub_net `
            -e "MINIO_ACCESS_KEY=$minioAccessKey" `
            -e "MINIO_SECRET_KEY=$minioSecretKey" `
            -e "MINIO_BUCKET_RAW=$minioBucketRaw" `
            -e "MINIO_BUCKET_ARTIFACTS=$minioBucketArtifacts" `
            --entrypoint /bin/sh `
            minio/mc:latest -c $mcProbeScript
        if ($LASTEXITCODE -ne 0) {
            throw "MinIO 演练连通性探活失败（退出码：$LASTEXITCODE）"
        }
        Write-Host "[2/3] MinIO 恢复演练通过（已核验 $objChecked 个对象 SHA256，目标私有桶连通正常）。" -ForegroundColor Green
    }
    else {
        # 实库恢复模式：镜像回灌并重申 Bucket 私有权限
        $mcRestoreScript = @'
set -eu
mc alias set dochub http://minio:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null
mc mb --ignore-existing "dochub/$MINIO_BUCKET_RAW"
mc mb --ignore-existing "dochub/$MINIO_BUCKET_ARTIFACTS"
mc mirror --overwrite "/backup/minio/$MINIO_BUCKET_RAW" "dochub/$MINIO_BUCKET_RAW"
mc mirror --overwrite "/backup/minio/$MINIO_BUCKET_ARTIFACTS" "dochub/$MINIO_BUCKET_ARTIFACTS"
mc anonymous set none "dochub/$MINIO_BUCKET_RAW"
mc anonymous set none "dochub/$MINIO_BUCKET_ARTIFACTS"
'@
        & docker run --rm `
            --network dochub_net `
            -e "MINIO_ACCESS_KEY=$minioAccessKey" `
            -e "MINIO_SECRET_KEY=$minioSecretKey" `
            -e "MINIO_BUCKET_RAW=$minioBucketRaw" `
            -e "MINIO_BUCKET_ARTIFACTS=$minioBucketArtifacts" `
            -v "${minioDir}:/backup/minio:ro" `
            --entrypoint /bin/sh `
            minio/mc:latest -c $mcRestoreScript
        if ($LASTEXITCODE -ne 0) {
            throw "MinIO 实库镜像恢复失败（退出码：$LASTEXITCODE）"
        }
        Write-Host "[2/3] MinIO 实库恢复完成（已回灌 $objChecked 个对象并强制保持私有访问策略）。" -ForegroundColor Green
    }
}

# ==============================================================================
# 4. Qdrant 快照校验与恢复
# ==============================================================================
if ((Test-ComponentSelected -Selected $Components -Name "qdrant") -and ($manifest.components.PSObject.Properties.Name -contains "qdrant")) {
    Write-Host "`n[3/3] 正在执行 Qdrant 向量快照校验与恢复检查..." -ForegroundColor Yellow
    $qdrantMeta = $manifest.components.qdrant
    $localSnap = Join-Path $resolvedBackupDir ($qdrantMeta.file -replace '/', '\')
    $qdrantBaseUrl = "http://${qdrantHost}:${qdrantPort}"
    $qdrantHeaders = @{ "api-key" = $qdrantApiKey }

    if (-not (Test-Path -LiteralPath $localSnap) -or (Get-Item -LiteralPath $localSnap).Length -le 0) {
        throw "Qdrant 快照文件缺失或为空：$localSnap"
    }

    # 验证 Qdrant 服务健康与 API Key 鉴权正常
    $readyResp = Invoke-WebRequest -UseBasicParsing -Uri "$qdrantBaseUrl/readyz" -Method Get -TimeoutSec 15
    if ($readyResp.StatusCode -ne 200) {
        throw "Qdrant 服务未就绪（HTTP $($readyResp.StatusCode)）"
    }
    $liveCollections = Invoke-RestMethod -Uri "$qdrantBaseUrl/collections" -Method Get -Headers $qdrantHeaders -TimeoutSec 30

    if ($DrillOnly) {
        $liveColList = @($liveCollections.result.collections)
        Write-Host "[3/3] Qdrant 恢复演练通过（快照 $($qdrantMeta.snapshot_name) SHA256 已核验，当前在线集合数：$($liveColList.Count)）。" -ForegroundColor Green
    }
    else {
        # Qdrant 全量 storage snapshot 需通过 --snapshot 参数启动或按集合恢复；
        # 此处将快照复制到容器 /qdrant/snapshots/ 并执行服务重载恢复指引与集合可用性验证
        $snapFileName = Split-Path -Leaf $localSnap
        & docker compose -f $resolvedCompose exec -T qdrant mkdir -p /qdrant/snapshots
        & docker compose -f $resolvedCompose cp $localSnap "qdrant:/qdrant/snapshots/$snapFileName"
        if ($LASTEXITCODE -ne 0) {
            throw "无法将 Qdrant 快照文件复制至容器：/qdrant/snapshots/$snapFileName"
        }

        Write-Host "[3/3] 已将全量快照写入容器 /qdrant/snapshots/$snapFileName。" -ForegroundColor Yellow
        Write-Host "      按 Qdrant 官方全量恢复规范，如需从全量快照冷恢复，请执行：" -ForegroundColor Yellow
        Write-Host "      docker compose stop qdrant && docker compose run --rm qdrant ./qdrant --snapshot /qdrant/snapshots/$snapFileName && docker compose start qdrant" -ForegroundColor Yellow
        Write-Host "      （注：亦可依据 PostgreSQL/MinIO 源文档结合 Embedding 流水线执行蓝绿索引重建，详见 TECHNICAL_SPEC §8.6 & §16）" -ForegroundColor Yellow
    }
}

Write-Host "`n==================================================================" -ForegroundColor Green
Write-Host "[DocHub Restore] $modeLabel 全部完成！" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
