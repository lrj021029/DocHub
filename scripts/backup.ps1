<#
.SYNOPSIS
    DocHub 生产级全量备份脚本（PostgreSQL + MinIO + Qdrant + SHA256 完整性清单）。

.DESCRIPTION
    遵循 TECHNICAL_SPEC §16（Backup / Recovery）与 ADR-001 安全红线：
    1. PostgreSQL：通过 docker compose 容器内 pg_dump（custom 格式）真实导出数据库，
       完整保留 ACL、文档版本、审计日志、Outbox 与索引指针，避免 Windows PowerShell 管道编码破坏二进制流。
    2. MinIO     ：通过 minio/mc 容器镜像导出 dochub-raw 与 dochub-artifacts 两个私有桶，
       并对全部导出对象逐一计算 SHA256 校验和。
    3. Qdrant    ：调用 Qdrant 官方 Snapshot API（携带 api-key）触发真实全量快照并下载归档。
    4. 安全红线  ：严禁在普通备份产物中复制或导出 .env、私钥或任何明文 Secret，备份结束自动执行敏感文件巡检。
    5. 完整性清单：生成 SHA256SUMS.txt 与 manifest.json，并在备份完成时立即复核所有文件哈希。

.EXAMPLE
    .\scripts\backup.ps1
    .\scripts\backup.ps1 -OutputRoot ".\backups" -Components postgres,minio,qdrant
#>

[CmdletBinding()]
param(
    # 备份输出根目录（默认位于 .gitignore 忽略的 backups/ 目录）
    [string]$OutputRoot = ".\backups",

    # 主 Docker Compose 文件路径
    [string]$ComposeFile = ".\docker-compose.yml",

    # 仅在内存中读取连接参数的环境变量文件路径（严禁写入备份产物）
    [string]$EnvFile = ".\.env",

    # 需要备份的组件列表（默认全部：postgres、minio、qdrant）
    [ValidateSet("all", "postgres", "minio", "qdrant")]
    [string[]]$Components = @("all"),

    # 是否保留 Qdrant 容器内生成的远端临时快照文件（默认下载校验后自动清理以节省空间）
    [switch]$KeepRemoteQdrantSnapshot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------------------------
# 工具函数：仅在内存中解析 .env 文件（不向控制台或磁盘输出任何密钥）
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
        # 去除首尾成对引号
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $envMap[$key] = $val
    }
    return $envMap
}

# 读取配置项优先级：进程环境变量 > .env 文件 > 本地开发默认值
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

# 计算文件小写 SHA256 哈希
function Get-FileSha256Hex {
    param([string]$FilePath)
    return (Get-FileHash -LiteralPath $FilePath -Algorithm SHA256).Hash.ToLowerInvariant()
}

# 将绝对路径转换为相对备份目录的标准正斜杠路径
function Get-NormalizedRelativePath {
    param(
        [string]$BaseDir,
        [string]$FullPath
    )
    $baseUri = New-Object System.Uri(($BaseDir.TrimEnd('\', '/') + '\'))
    $fileUri = New-Object System.Uri($FullPath)
    return [System.Uri]::UnescapeDataString($baseUri.MakeRelativeUri($fileUri).ToString())
}

# 检查组件是否在本次备份范围内
function Test-ComponentSelected {
    param(
        [string[]]$Selected,
        [string]$Name
    )
    return ($Selected -contains "all") -or ($Selected -contains $Name)
}

# ------------------------------------------------------------------------------
# 主流程初始化
# ------------------------------------------------------------------------------
$repoRoot = (Resolve-Path ".").Path
$resolvedCompose = (Resolve-Path -LiteralPath $ComposeFile).Path
$envMap = Import-EnvFileToMemory -Path $EnvFile

# 解析核心连接配置（密钥仅驻留当前 PowerShell 进程内存，绝不打印或落盘）
$pgDb = Get-ConfigValue -EnvMap $envMap -Key "POSTGRES_DB" -DefaultValue "dochub"
$pgSuperUser = Get-ConfigValue -EnvMap $envMap -Key "POSTGRES_SUPERUSER" -DefaultValue "postgres"

$minioAccessKey = Get-ConfigValue -EnvMap $envMap -Key "MINIO_ACCESS_KEY" -DefaultValue "dochub_minio_dev"
$minioSecretKey = Get-ConfigValue -EnvMap $envMap -Key "MINIO_SECRET_KEY" -DefaultValue "dochub_minio_secret_dev"
$minioBucketRaw = Get-ConfigValue -EnvMap $envMap -Key "MINIO_BUCKET_RAW" -DefaultValue "dochub-raw"
$minioBucketArtifacts = Get-ConfigValue -EnvMap $envMap -Key "MINIO_BUCKET_ARTIFACTS" -DefaultValue "dochub-artifacts"

$qdrantHost = Get-ConfigValue -EnvMap $envMap -Key "QDRANT_HOST" -DefaultValue "127.0.0.1"
$qdrantPort = Get-ConfigValue -EnvMap $envMap -Key "QDRANT_PORT" -DefaultValue "6333"
$qdrantApiKey = Get-ConfigValue -EnvMap $envMap -Key "QDRANT_API_KEY" -DefaultValue "dochub_qdrant_dev_key"

# 确保备份目录存在
if (-not (Test-Path -LiteralPath $OutputRoot)) {
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
}
$resolvedOutputRoot = (Resolve-Path -LiteralPath $OutputRoot).Path
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupId = "dochub-backup-$timestamp"
$backupDir = Join-Path $resolvedOutputRoot $backupId
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "[DocHub Backup] 开始执行备份任务：$backupId" -ForegroundColor Cyan
Write-Host "[DocHub Backup] 目标目录：$backupDir" -ForegroundColor Cyan
Write-Host "[DocHub Backup] 安全策略：严禁导出 .env 或明文 Secret" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

$componentSummary = [ordered]@{}

# ==============================================================================
# 1. PostgreSQL 备份（pg_dump custom 格式，二进制安全导出）
# ==============================================================================
if (Test-ComponentSelected -Selected $Components -Name "postgres") {
    Write-Host "`n[1/3] 正在执行 PostgreSQL 数据库全量备份（数据库：$pgDb）..." -ForegroundColor Yellow
    $pgDir = Join-Path $backupDir "postgres"
    New-Item -ItemType Directory -Path $pgDir -Force | Out-Null

    $remoteTmpDump = "/tmp/dochub_backup_$timestamp.dump"
    $localDumpPath = Join-Path $pgDir "$pgDb.dump"

    try {
        # 在容器内直接落盘至临时文件，彻底规避 Windows 管道重定向破坏二进制格式
        & docker compose -f $resolvedCompose exec -T postgres `
            pg_dump -U $pgSuperUser -d $pgDb --format=custom --compress=6 --file=$remoteTmpDump
        if ($LASTEXITCODE -ne 0) {
            throw "PostgreSQL pg_dump 执行失败（退出码：$LASTEXITCODE）"
        }

        # 立即用 pg_restore --list 验证归档目录结构可读
        & docker compose -f $resolvedCompose exec -T postgres pg_restore --list $remoteTmpDump | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "PostgreSQL 备份归档校验失败（pg_restore --list 返回非零退出码：$LASTEXITCODE）"
        }

        # 从容器拷贝到本地备份目录
        & docker compose -f $resolvedCompose cp "postgres:$remoteTmpDump" $localDumpPath
        if ($LASTEXITCODE -ne 0) {
            throw "无法从 postgres 容器复制备份文件到本地：$localDumpPath"
        }
    }
    finally {
        # 无论成功与否均清理容器内临时转储文件
        & docker compose -f $resolvedCompose exec -T postgres rm -f $remoteTmpDump 2>$null | Out-Null
    }

    $dumpInfo = Get-Item -LiteralPath $localDumpPath
    if ($dumpInfo.Length -le 0) {
        throw "PostgreSQL 备份文件为空：$localDumpPath"
    }

    $componentSummary["postgres"] = [ordered]@{
        status     = "success"
        database   = $pgDb
        format     = "pg_dump_custom"
        file       = "postgres/$pgDb.dump"
        size_bytes = $dumpInfo.Length
        sha256     = (Get-FileSha256Hex -FilePath $localDumpPath)
    }
    Write-Host "[1/3] PostgreSQL 备份完成（大小：$($dumpInfo.Length) 字节）。" -ForegroundColor Green
}

# ==============================================================================
# 2. MinIO 对象存储备份（导出私有 Bucket 并计算逐对象 SHA256）
# ==============================================================================
if (Test-ComponentSelected -Selected $Components -Name "minio") {
    Write-Host "`n[2/3] 正在执行 MinIO 私有 Bucket 对象备份（$minioBucketRaw, $minioBucketArtifacts）..." -ForegroundColor Yellow
    $minioDir = Join-Path $backupDir "minio"
    $rawDir = Join-Path $minioDir $minioBucketRaw
    $artifactsDir = Join-Path $minioDir $minioBucketArtifacts
    New-Item -ItemType Directory -Path $rawDir -Force | Out-Null
    New-Item -ItemType Directory -Path $artifactsDir -Force | Out-Null

    # 通过官方 minio/mc 容器接入 dochub_net 内网执行镜像导出（凭据仅经由容器环境变量传入）
    $mcScript = @'
set -eu
mc alias set dochub http://minio:9000 "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null
mc mirror --overwrite "dochub/$MINIO_BUCKET_RAW" "/backup/minio/$MINIO_BUCKET_RAW"
mc mirror --overwrite "dochub/$MINIO_BUCKET_ARTIFACTS" "/backup/minio/$MINIO_BUCKET_ARTIFACTS"
'@

    & docker run --rm `
        --network dochub_net `
        -e "MINIO_ACCESS_KEY=$minioAccessKey" `
        -e "MINIO_SECRET_KEY=$minioSecretKey" `
        -e "MINIO_BUCKET_RAW=$minioBucketRaw" `
        -e "MINIO_BUCKET_ARTIFACTS=$minioBucketArtifacts" `
        -v "${minioDir}:/backup/minio" `
        --entrypoint /bin/sh `
        minio/mc:latest -c $mcScript
    if ($LASTEXITCODE -ne 0) {
        throw "MinIO mc mirror 导出失败（退出码：$LASTEXITCODE）"
    }

    # 为每个导出的对象生成独立 SHA256 校验记录，满足对象完整性校验要求
    $objectFiles = @(Get-ChildItem -LiteralPath $minioDir -Recurse -File)
    $objectChecksums = @()
    [long]$totalBytes = 0
    foreach ($obj in $objectFiles) {
        $rel = Get-NormalizedRelativePath -BaseDir $minioDir -FullPath $obj.FullName
        $hash = Get-FileSha256Hex -FilePath $obj.FullName
        $totalBytes += $obj.Length
        $objectChecksums += [ordered]@{
            path       = $rel
            size_bytes = $obj.Length
            sha256     = $hash
        }
    }

    $objectsManifestPath = Join-Path $minioDir "objects.sha256.json"
    [ordered]@{
        buckets       = @($minioBucketRaw, $minioBucketArtifacts)
        object_count  = $objectChecksums.Count
        total_bytes   = $totalBytes
        objects       = $objectChecksums
    } | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $objectsManifestPath -Encoding UTF8

    $componentSummary["minio"] = [ordered]@{
        status       = "success"
        buckets      = @($minioBucketRaw, $minioBucketArtifacts)
        object_count = $objectChecksums.Count
        total_bytes  = $totalBytes
        manifest     = "minio/objects.sha256.json"
    }
    Write-Host "[2/3] MinIO 备份完成（对象数：$($objectChecksums.Count)，总大小：$totalBytes 字节）。" -ForegroundColor Green
}

# ==============================================================================
# 3. Qdrant 向量库快照备份（调用官方 Snapshot API）
# ==============================================================================
if (Test-ComponentSelected -Selected $Components -Name "qdrant") {
    Write-Host "`n[3/3] 正在执行 Qdrant 向量库全量快照备份（http://${qdrantHost}:${qdrantPort}）..." -ForegroundColor Yellow
    $qdrantDir = Join-Path $backupDir "qdrant"
    New-Item -ItemType Directory -Path $qdrantDir -Force | Out-Null

    $qdrantBaseUrl = "http://${qdrantHost}:${qdrantPort}"
    $qdrantHeaders = @{ "api-key" = $qdrantApiKey }

    # 3.1 导出 Collection 元信息列表（不包含任何密钥）
    $collectionsResp = Invoke-RestMethod -Uri "$qdrantBaseUrl/collections" -Method Get -Headers $qdrantHeaders -TimeoutSec 30
    $collectionsMetaPath = Join-Path $qdrantDir "collections.json"
    $collectionsResp | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $collectionsMetaPath -Encoding UTF8

    # 3.2 触发全量存储快照
    $snapCreateResp = Invoke-RestMethod -Uri "$qdrantBaseUrl/snapshots?wait=true" -Method Post -Headers $qdrantHeaders -TimeoutSec 300
    if (-not $snapCreateResp.result -or [string]::IsNullOrWhiteSpace($snapCreateResp.result.name)) {
        throw "Qdrant 快照创建未返回有效快照文件名。"
    }
    $snapshotName = [string]$snapCreateResp.result.name
    $localSnapshotPath = Join-Path $qdrantDir $snapshotName

    try {
        # 3.3 下载完整二进制快照文件（-UseBasicParsing 兼容 Windows PowerShell 5.1 与 PS 7+）
        Invoke-WebRequest -UseBasicParsing -Uri "$qdrantBaseUrl/snapshots/$snapshotName" -Method Get -Headers $qdrantHeaders -OutFile $localSnapshotPath -TimeoutSec 600
    }
    finally {
        # 3.4 清理远端临时快照，避免占用 qdrant_data Volume 空间
        if (-not $KeepRemoteQdrantSnapshot) {
            try {
                Invoke-RestMethod -Uri "$qdrantBaseUrl/snapshots/$snapshotName?wait=true" -Method Delete -Headers $qdrantHeaders -TimeoutSec 60 | Out-Null
            }
            catch {
                Write-Warning "清理远端 Qdrant 快照 $snapshotName 时出现警告（不影响本地备份文件）：$($_.Exception.Message)"
            }
        }
    }

    $snapInfo = Get-Item -LiteralPath $localSnapshotPath
    if ($snapInfo.Length -le 0) {
        throw "Qdrant 快照文件为空：$localSnapshotPath"
    }

    $componentSummary["qdrant"] = [ordered]@{
        status        = "success"
        snapshot_name = $snapshotName
        file          = "qdrant/$snapshotName"
        size_bytes    = $snapInfo.Length
        sha256        = (Get-FileSha256Hex -FilePath $localSnapshotPath)
    }
    Write-Host "[3/3] Qdrant 快照备份完成（文件：$snapshotName，大小：$($snapInfo.Length) 字节）。" -ForegroundColor Green
}

# ==============================================================================
# 4. 安全巡检：严格确保备份目录中不存在任何 .env 或私钥文件
# ==============================================================================
Write-Host "`n[安全校验] 正在执行备份产物敏感文件隔离检查..." -ForegroundColor Yellow
$forbiddenFiles = @(Get-ChildItem -LiteralPath $backupDir -Recurse -File | Where-Object {
    $_.Name -match '^\.env(\.|$)' -or $_.Extension -in @(".pem", ".key", ".pfx", ".p12")
})
if ($forbiddenFiles.Count -gt 0) {
    $badList = ($forbiddenFiles | ForEach-Object { $_.FullName }) -join ", "
    throw "安全红线拦截：备份目录中检测到禁止导出的敏感文件：$badList"
}

# ==============================================================================
# 5. 生成全量 SHA256SUMS.txt 与结构化 manifest.json 并复核完整性
# ==============================================================================
Write-Host "[清单生成] 正在生成全量 SHA256 校验清单..." -ForegroundColor Yellow
$allArtifacts = @(Get-ChildItem -LiteralPath $backupDir -Recurse -File | Sort-Object FullName)
$manifestFiles = @()
$shaLines = @()

foreach ($artifact in $allArtifacts) {
    $relPath = Get-NormalizedRelativePath -BaseDir $backupDir -FullPath $artifact.FullName
    $hash = Get-FileSha256Hex -FilePath $artifact.FullName
    $manifestFiles += [ordered]@{
        path       = $relPath
        size_bytes = $artifact.Length
        sha256     = $hash
    }
    $shaLines += "$hash  $relPath"
}

$shaSumsPath = Join-Path $backupDir "SHA256SUMS.txt"
$shaLines | Set-Content -LiteralPath $shaSumsPath -Encoding UTF8

$manifestPath = Join-Path $backupDir "manifest.json"
$manifestObj = [ordered]@{
    schema_version          = "1.0"
    backup_id               = $backupId
    created_at_utc          = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    secret_export_prohibited = $true
    components              = $componentSummary
    artifact_count          = $manifestFiles.Count
    artifacts               = $manifestFiles
}
$manifestObj | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

# 立即二次回读校验，确认落盘无损坏
$verifyManifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($entry in $verifyManifest.artifacts) {
    $targetPath = Join-Path $backupDir ($entry.path -replace '/', '\')
    $actualHash = Get-FileSha256Hex -FilePath $targetPath
    if ($actualHash -ne $entry.sha256) {
        throw "备份自检失败：文件 $($entry.path) 哈希不匹配（期望：$($entry.sha256)，实际：$actualHash）"
    }
}

Write-Host "`n==================================================================" -ForegroundColor Green
Write-Host "[DocHub Backup] 备份与 SHA256 完整性校验全部通过！" -ForegroundColor Green
Write-Host "[DocHub Backup] 产物路径：$backupDir" -ForegroundColor Green
Write-Host "[DocHub Backup] 清单文件：$manifestPath（共 $($manifestFiles.Count) 个产物文件）" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
