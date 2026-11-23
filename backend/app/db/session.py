"""运行期异步数据库引擎、会话工厂与 PostgreSQL 事务级 RLS 安全上下文管理器。

安全架构铁律（ADR-002 & TECHNICAL_SPEC §14.2）：
1. 运行期 `runtime_engine` 强制使用受限角色 `app_runtime`
   （无 SUPERUSER、无 BYPASSRLS、非表 Owner）。
2. 所有租户业务查询与写入必须通过 `org_rls_session(session, organization_id)` 在显式事务内执行：
   `SELECT set_config('app.current_organization_id', :org_id, true)`（等价于事务级 `SET LOCAL`）。
3. 严禁使用会话级 `SET`，并在连接归还连接池前强制结束事务，彻底杜绝连接复用导致的跨租户上下文污染。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

RLS_ORG_SETTING_KEY = "app.current_organization_id"
_SESSION_RLS_ORG_INFO_KEY = "rls_organization_id"


def create_runtime_engine(settings: Settings | None = None) -> AsyncEngine:
    """构造运行期异步数据库引擎（强制绑定 `runtime_database_url` / `app_runtime` 角色）。"""
    cfg = settings or get_settings()
    return create_async_engine(
        cfg.runtime_database_url.get_secret_value(),
        echo=cfg.db_echo,
        pool_pre_ping=True,
        pool_size=cfg.db_pool_size,
        max_overflow=cfg.db_max_overflow,
        pool_timeout=cfg.db_pool_timeout,
        pool_recycle=cfg.db_pool_recycle,
    )


# 全局运行期异步引擎与会话工厂（供 FastAPI API 与 Celery Worker 使用）
runtime_engine: AsyncEngine = create_runtime_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=runtime_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入：提供受控生命周期的异步数据库会话。

    在请求结束或出现未捕获异常时确保回滚未提交事务并安全释放连接回连接池。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            if session.in_transaction():
                await session.rollback()
            raise
        finally:
            # 防止调用方遗留未提交事务，避免连接池复用时残留任何状态
            if session.in_transaction():
                await session.rollback()
            session.info.pop(_SESSION_RLS_ORG_INFO_KEY, None)


def _normalize_org_id(organization_id: uuid.UUID | str) -> str:
    """严格校验并规范化 organization_id 为标准 UUID 字符串，杜绝非法值注入。"""
    if isinstance(organization_id, uuid.UUID):
        return str(organization_id)
    try:
        return str(uuid.UUID(str(organization_id).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"非法的 organization_id，必须为有效 UUID: {organization_id!r}") from exc


@asynccontextmanager
async def org_rls_session(
    session: AsyncSession,
    organization_id: uuid.UUID | str,
) -> AsyncIterator[AsyncSession]:
    """在显式事务内绑定事务级 PostgreSQL RLS 租户上下文（SET LOCAL）。

    行为说明：
    - 若会话当前无活跃事务，则开启显式事务并在事务首条语句执行：
      `SELECT set_config('app.current_organization_id', :org_id, true)`。
      退出上下文时正常自动提交，异常自动回滚，PostgreSQL 自动回收 `SET LOCAL` 设置。
    - 若调用方已在显式事务内进入本上下文：
      - 若尚未设置 RLS 上下文，则在当前事务内设置 `SET LOCAL`；
      - 若已设置相同 `organization_id`，允许安全重入；
      - 若已设置不同 `organization_id`，立即拒绝并抛出 RuntimeError，防止跨租户污染。
    """
    normalized_org_id = _normalize_org_id(organization_id)
    set_local_stmt = text("SELECT set_config(:setting_key, :org_id, true)")

    if not session.in_transaction():
        async with session.begin():
            await session.execute(
                set_local_stmt,
                {"setting_key": RLS_ORG_SETTING_KEY, "org_id": normalized_org_id},
            )
            session.info[_SESSION_RLS_ORG_INFO_KEY] = normalized_org_id
            try:
                yield session
            finally:
                session.info.pop(_SESSION_RLS_ORG_INFO_KEY, None)
        return

    # 已处于外部显式事务内：严禁在同一事务中切换到不同的 organization_id
    existing_org_id = session.info.get(_SESSION_RLS_ORG_INFO_KEY)
    if existing_org_id is not None and existing_org_id != normalized_org_id:
        raise RuntimeError(
            "安全阻断：禁止在同一数据库事务内切换不同的 organization_id RLS 上下文"
        )

    if existing_org_id is None:
        await session.execute(
            set_local_stmt,
            {"setting_key": RLS_ORG_SETTING_KEY, "org_id": normalized_org_id},
        )
        session.info[_SESSION_RLS_ORG_INFO_KEY] = normalized_org_id

    yield session


async def dispose_runtime_engine() -> None:
    """在应用优雅停机时释放运行期数据库连接池。"""
    await runtime_engine.dispose()
