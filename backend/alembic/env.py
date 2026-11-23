"""Alembic 异步迁移环境脚本。

安全约束（ADR-002）：
1. 强制使用 `Settings.migrator_database_url`（即 `app_migrator` 角色）执行 DDL 迁移，
   与运行期 `app_runtime` 角色彻底分离。
2. 支持异步 PostgreSQL 引擎（SQLAlchemy 2.x Async + asyncpg）。
3. 显式导入全部业务领域 ORM 模型以确保 `Base.metadata` 完整注册。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base

# 显式注册 Slice 1 全部领域模型到 Base.metadata
from app.modules.audit import models as _audit_models  # noqa: F401
from app.modules.departments import models as _department_models  # noqa: F401
from app.modules.identity import models as _identity_models  # noqa: F401
from app.modules.organizations import models as _organization_models  # noqa: F401
from app.modules.roles import models as _role_models  # noqa: F401

# Alembic Config 对象，对应 alembic.ini 配置
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 绑定全局声明式 MetaData，供迁移对比与校验使用
target_metadata = Base.metadata


def _get_migrator_url() -> str:
    """获取迁移专用数据库连接串（强制使用 app_migrator 角色）。"""
    settings = get_settings()
    return settings.migrator_database_url.get_secret_value()


def run_migrations_offline() -> None:
    """离线模式执行迁移：仅生成 SQL 脚本，不建立实际数据库连接。"""
    url = _get_migrator_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步桥接连接上执行实际迁移事务。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式执行迁移：使用 asyncpg 异步引擎以 `app_migrator` 身份连接数据库。"""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _get_migrator_url()

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线迁移入口：驱动异步事件循环执行迁移。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
