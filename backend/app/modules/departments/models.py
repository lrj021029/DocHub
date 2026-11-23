"""部门树与多部门归属 ORM 模型（全部为租户表，强制启用 PostgreSQL FORCE RLS）。

包含：
1. `departments`：部门邻接树。
2. `department_closures`：部门 Closure Table（含 `organization_id`，支撑 O(1) 祖先检索与防环）。
3. `department_memberships`：用户多部门/兼职关系（主部门标记与 `valid_from`/`valid_until`）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Department(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """组织内部门邻接树表（强制 FORCE RLS）。"""

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "parent_id",
            "name",
            name="uq_departments_org_parent_name",
        ),
        Index("ix_departments_org_parent", "organization_id", "parent_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="上级部门 ID（根部门为 NULL）",
    )
    name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="部门名称",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="部门状态（ACTIVE / ARCHIVED）",
    )


class DepartmentClosure(TenantMixin, Base):
    """部门闭包表（Closure Table，强制冗余 organization_id 并启用 FORCE RLS）。"""

    __tablename__ = "department_closures"
    __table_args__ = (
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        Index(
            "ix_department_closures_org_descendant",
            "organization_id",
            "descendant_id",
        ),
        Index(
            "ix_department_closures_org_ancestor",
            "organization_id",
            "ancestor_id",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    ancestor_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
        comment="祖先部门 ID",
    )
    descendant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
        comment="后代部门 ID（depth=0 时等于 ancestor_id）",
    )
    depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="祖先到后代的层级深度（自身为 0）",
    )


class DepartmentMembership(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """用户多部门与兼职归属关系表（强制 FORCE RLS）。"""

    __tablename__ = "department_memberships"
    __table_args__ = (
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_range_order",
        ),
        Index(
            "ix_department_memberships_org_user",
            "organization_id",
            "user_id",
        ),
        Index(
            "ix_department_memberships_org_dept",
            "organization_id",
            "department_id",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属部门 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="成员用户 ID",
    )
    membership_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PRIMARY",
        server_default="PRIMARY",
        comment="归属类型（PRIMARY 主部门 / SECONDARY 兼职 / TEMPORARY 临时借调）",
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="是否为用户在该组织内的主部门",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="归属状态（ACTIVE / REVOKED）",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="归属生效时间（UTC）",
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="归属到期时间（NULL 表示长期有效）",
    )
