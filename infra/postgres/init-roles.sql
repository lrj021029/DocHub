-- ==============================================================================
-- DocHub PostgreSQL 17 角色与最小权限初始化脚本
-- 挂载位置：/docker-entrypoint-initdb.d/01-init-roles.sql
--
-- 安全设计（遵循 ADR-002 与 TECHNICAL_SPEC §14.2）：
-- 1. 双角色分离：
--    - app_migrator：Alembic DDL 迁移专用角色，拥有 public schema 建表权限并作为业务表 Owner。
--    - app_runtime ：FastAPI API 与 Celery Worker 运行期角色，无 SUPERUSER、无 BYPASSRLS、
--                    非业务表 Owner，强制受 PostgreSQL FORCE ROW LEVEL SECURITY 约束。
-- 2. 最小权限原则：
--    - 回收 PUBLIC 在 public schema 的 CREATE 权限。
--    - 通过 ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator 自动向 app_runtime 授予
--      新建表的 SELECT/INSERT/UPDATE/DELETE 及序列的 USAGE/SELECT 权限，不授予 DDL 权限。
-- 3. 生产环境提醒：
--    - 下方默认密码仅用于本地开发初始化，生产环境部署后必须通过 ALTER ROLE 轮换为高强度密码，
--      并与密钥管理系统中的 POSTGRES_MIGRATOR_PASSWORD / POSTGRES_RUNTIME_PASSWORD 保持一致。
-- ==============================================================================

\set ON_ERROR_STOP on

-- 1. 幂等创建迁移专用角色 app_migrator（无超级用户权限、不绕过 RLS）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_migrator') THEN
        CREATE ROLE app_migrator
            LOGIN
            PASSWORD 'dochub_migrator_dev'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    ELSE
        -- 确保存量角色属性符合安全基线
        ALTER ROLE app_migrator
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END
$$;

-- 2. 幂等创建运行期角色 app_runtime（严禁 SUPERUSER 与 BYPASSRLS，确保 RLS 强制生效）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        CREATE ROLE app_runtime
            LOGIN
            PASSWORD 'dochub_runtime_dev'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    ELSE
        -- 强制重申最小权限属性，防止权限漂移绕过 RLS
        ALTER ROLE app_runtime
            LOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOREPLICATION
            NOBYPASSRLS;
    END IF;
END
$$;

-- 3. 收紧默认 PUBLIC 权限，仅对显式角色授权
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE dochub FROM PUBLIC;

-- 4. 授予数据库连接与临时对象权限
GRANT CONNECT, TEMPORARY ON DATABASE dochub TO app_migrator;
GRANT CONNECT, TEMPORARY ON DATABASE dochub TO app_runtime;

-- 5. Schema 级权限分离：
--    - app_migrator 拥有 public schema 的 USAGE + CREATE 权限（用于 Alembic 建表与变更）
--    - app_runtime  仅拥有 public schema 的 USAGE 权限（禁止建表或修改表结构，绝非表 Owner）
GRANT USAGE, CREATE ON SCHEMA public TO app_migrator;
GRANT USAGE ON SCHEMA public TO app_runtime;

-- 6. 默认权限继承：当 app_migrator 在 public schema 中创建表与序列时，
--    自动向 app_runtime 授予最小业务 DML 权限与序列读取权限
ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;

ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_runtime;

-- 7. 幂等兜底：为 public schema 中可能已存在的对象同步最小权限
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime;
