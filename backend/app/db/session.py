"""运行期异步数据库引擎、会话工厂与 PostgreSQL 事务级 RLS 安全上下文管理器。

安全架构铁律（ADR-002 & TECHNICAL_SPEC §14.2）：
1. 运行期 `runtime_engine` 强制使用受限角色 `app_runtime`
   （无 SUPERUSER、无 BYPASSRLS、非表 Owner）。
2. 所有租户业务查询与写入必须通过 `org_rls_session(session, organization_id, user_id=None)`
   在显式事务内执行事务级 `set_config(..., true)`（等价于 `SET LOCAL`）。
3. 登录或列举用户所属组织时使用 `user_rls_session(session, user_id)`，仅注入
   `app.current_user_id`，确保 `organization_members` 的 RLS 策略仅暴露当前用户本人记录，
   且两项变量均未设置时 Fail-Closed 返回 0 行。
4. 严禁使用会话级 `SET`，并在连接归还连接池前强制结束事务，杜绝连接复用上下文污染。
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
RLS_USER_SETTING_KEY = "app.current_user_id"
_SESSION_RLS_ORG_INFO_KEY = "rls_organization_id"
_SESSION_RLS_USER_INFO_KEY = "rls_user_id"


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
            if session.in_transaction():
                await session.rollback()
            session.info.pop(_SESSION_RLS_ORG_INFO_KEY, None)
            session.info.pop(_SESSION_RLS_USER_INFO_KEY, None)


def _normalize_uuid(value: uuid.UUID | str, *, field_name: str) -> str:
    """严格校验并规范化 UUID 字段，杜绝非法字符串注入。"""
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"非法的 {field_name}，必须为有效 UUID: {value!r}") from exc


@asynccontextmanager
async def user_rls_session(
    session: AsyncSession,
    user_id: uuid.UUID | str,
) -> AsyncIterator[AsyncSession]:
    """在显式事务内绑定事务级 `app.current_user_id`（用于登录、列举本人所属组织等场景）。

    说明：本上下文故意不设置 `app.current_organization_id`，仅开放受 `user_id` 策略保护的
    本人成员关系视图，其他纯租户表仍处于 Fail-Closed（返回 0 行）。
    """
    normalized_user_id = _normalize_uuid(user_id, field_name="user_id")
    set_local_stmt = text("SELECT set_config(:setting_key, :value, true)")

    if not session.in_transaction():
        async with session.begin():
            await session.execute(
                set_local_stmt,
                {"setting_key": RLS_USER_SETTING_KEY, "value": normalized_user_id},
            )
            session.info[_SESSION_RLS_USER_INFO_KEY] = normalized_user_id
            try:
                yield session
            finally:
                session.info.pop(_SESSION_RLS_USER_INFO_KEY, None)
        return

    existing_user_id = session.info.get(_SESSION_RLS_USER_INFO_KEY)
    if existing_user_id is not None and existing_user_id != normalized_user_id:
        raise RuntimeError("安全阻断：禁止在同一数据库事务内切换不同的 user_id RLS 上下文")

    if existing_user_id is None:
        await session.execute(
            set_local_stmt,
            {"setting_key": RLS_USER_SETTING_KEY, "value": normalized_user_id},
        )
        session.info[_SESSION_RLS_USER_INFO_KEY] = normalized_user_id

    yield session


@asynccontextmanager
async def org_rls_session(
    session: AsyncSession,
    organization_id: uuid.UUID | str,
    user_id: uuid.UUID | str | None = None,
) -> AsyncIterator[AsyncSession]:
    """在显式事务内绑定事务级 PostgreSQL RLS 租户上下文（SET LOCAL）。

    行为说明：
    - 强制校验 `organization_id` 为合法 UUID，并在事务首条语句执行：
      `SELECT set_config('app.current_organization_id', :org_id, true)`。
    - 若同时传入 `user_id`，同步在当前事务设置 `app.current_user_id`。
    - 严禁在同一数据库事务内切换至不同的 `organization_id` 或 `user_id`，否则立即阻断。
    """
    normalized_org_id = _normalize_uuid(organization_id, field_name="organization_id")
    normalized_user_id = (
        _normalize_uuid(user_id, field_name="user_id") if user_id is not None else None
    )
    set_local_stmt = text("SELECT set_config(:setting_key, :value, true)")

    if not session.in_transaction():
        async with session.begin():
            await session.execute(
                set_local_stmt,
                {"setting_key": RLS_ORG_SETTING_KEY, "value": normalized_org_id},
            )
            session.info[_SESSION_RLS_ORG_INFO_KEY] = normalized_org_id
            if normalized_user_id is not None:
                await session.execute(
                    set_local_stmt,
                    {"setting_key": RLS_USER_SETTING_KEY, "value": normalized_user_id},
                )
                session.info[_SESSION_RLS_USER_INFO_KEY] = normalized_user_id
            try:
                yield session
            finally:
                session.info.pop(_SESSION_RLS_ORG_INFO_KEY, None)
                session.info.pop(_SESSION_RLS_USER_INFO_KEY, None)
        return

    # 已处于外部显式事务内：严禁在同一事务中切换到不同的 organization_id 或 user_id
    existing_org_id = session.info.get(_SESSION_RLS_ORG_INFO_KEY)
    if existing_org_id is not None and existing_org_id != normalized_org_id:
        raise RuntimeError(
            "安全阻断：禁止在同一数据库事务内切换不同的 organization_id RLS 上下文"
        )

    existing_user_id = session.info.get(_SESSION_RLS_USER_INFO_KEY)
    if (
        normalized_user_id is not None
        and existing_user_id is not None
        and existing_user_id != normalized_user_id
    ):
        raise RuntimeError("安全阻断：禁止在同一数据库事务内切换不同的 user_id RLS 上下文")

    if existing_org_id is None:
        await session.execute(
            set_local_stmt,
            {"setting_key": RLS_ORG_SETTING_KEY, "value": normalized_org_id},
        )
        session.info[_SESSION_RLS_ORG_INFO_KEY] = normalized_org_id

    if normalized_user_id is not None and existing_user_id is None:
        await session.execute(
            set_local_stmt,
            {"setting_key": RLS_USER_SETTING_KEY, "value": normalized_user_id},
        )
        session.info[_SESSION_RLS_USER_INFO_KEY] = normalized_user_id

    yield session


async def dispose_runtime_engine() -> None:
    """在应用优雅停机时释放运行期数据库连接池。"""
    await runtime_engine.dispose()
