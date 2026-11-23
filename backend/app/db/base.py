"""SQLAlchemy 2.x 声明式基类与通用租户/时间戳 Mixin。

严格遵循 ADR-002：
1. 所有业务表统一使用应用层生成的 RFC 9562 UUID v7 作为主键。
2. 所有租户业务表及其全部子表必须继承 TenantMixin，强制包含非空且带索引的
   `organization_id: Mapped[uuid.UUID]`，配合 PostgreSQL `FORCE ROW LEVEL SECURITY`
   实现 Fail-Closed 强隔离。
3. 统一定义 MetaData 约束命名规范，确保 Alembic 自动迁移稳定且可回滚。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import new_uuid7

# 确定性约束命名约定，保证 Alembic 自动生成迁移脚本与回滚脚本名称一致
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """DocHub 全局 ORM 声明式基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """时间有序 UUID v7 主键 Mixin。"""

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=new_uuid7,
        comment="时间有序 UUID v7 主键",
    )


class TimestampMixin:
    """标准 UTC 创建时间与更新时间 Mixin。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="记录创建时间（UTC）",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="记录最后更新时间（UTC）",
    )


class TenantMixin:
    """多租户强隔离 Mixin（强制每个租户表及子表持有非空 organization_id 索引列）。"""

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
