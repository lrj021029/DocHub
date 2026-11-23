"""企业组织领域 ORM 模型（平台级表 `organizations` 与租户 RLS 表 `organization_members`）。

安全约束（ADR-002）：
- `organization_members` 开启 `ENABLE + FORCE ROW LEVEL SECURITY`，采用双上下文策略：
  `organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::uuid
   OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid`
  当两项会话变量均未设置时 Fail-Closed 返回 0 行。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """企业组织实体（平台级租户主表）。"""

    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        comment="企业组织名称",
    )
    slug: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        unique=True,
        index=True,
        comment="组织唯一短标识（URL/配置友好）",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="组织状态（ACTIVE / SUSPENDED / ARCHIVED）",
    )


class OrganizationMember(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """企业组织成员关系表（强制启用 PostgreSQL FORCE RLS）。"""

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_members_org_user",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="成员用户 ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="成员状态（ACTIVE / SUSPENDED / REMOVED）",
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="加入组织时间（UTC）",
    )
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="移出组织时间（UTC）",
    )
