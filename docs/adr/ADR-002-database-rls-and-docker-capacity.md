# ADR-002：PostgreSQL RLS 强隔离、UUID v7 与 Docker 容量预算

- 状态：已批准（Approved）
- 日期：2026-11-23

## 1. 数据库版本与主键策略

- 选用 **PostgreSQL 17** 官方镜像（`postgres:17-alpine`），稳定支持异步驱动 `asyncpg` 与 `FORCE ROW LEVEL SECURITY`。
- 主键统一使用 **UUID v7**（应用层时间有序 UUID v7 生成器 `app.core.ids.new_uuid7()`，兼容 PG 17/18 且无扩展编译依赖）。

## 2. 双角色分离与强制 RLS 规范

1. `app_migrator`：作为业务表 Owner，仅用于 Alembic DDL 迁移与初始化。
2. `app_runtime`：无 `SUPERUSER`、无 `BYPASSRLS`、非表 Owner，供 FastAPI API 与 Celery Worker 运行期使用。
3. 所有租户业务表及其全部子表（包括 `document_versions`、`acl_grants`、`acl_grant_department_snapshots`、`chunks`、`messages`、`message_segments`、`answer_citations`、`faq_versions`、`faq_segments`、`knowledge_gap_tasks`、`outbox_events` 等）均强制冗余 `organization_id UUID NOT NULL`。
4. 所有租户表启用 `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`，策略统一采用 Fail-Closed 表达式：
   `organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::uuid`
5. 请求与 Worker 事务中仅允许使用事务级 `SET LOCAL app.current_organization_id = '<uuid>'`，严禁使用会话级 `SET`，杜绝连接池复用上下文污染。

## 3. Windows Docker Desktop 资源预算（实测 15.4 GiB RAM / 2 vCPU / RTX 4060 8GB）

- 所有持久化服务（PostgreSQL、Redis、MinIO、Qdrant、Loki、Tempo、Prometheus、Grafana）统一采用 **Docker Named Volumes**，避免 Windows NTFS bind mount I/O 退化。
- 采用主 `docker-compose.yml` + Profiles 分层启停：
  - `core`：PostgreSQL、Redis、MinIO、Qdrant（常驻约 2.5~3.5 GB 内存）
  - `app`：FastAPI API、Celery Worker、Celery Beat、Frontend Nginx
  - `observability`：OTel Collector、Prometheus、Loki、Tempo、Grafana
  - `full-demo`：完整演示栈
- 所有基础设施端口仅绑定 `127.0.0.1`，不对外网裸露管理端口。
