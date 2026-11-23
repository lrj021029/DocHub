"""角色与功能权限 ORM 模型（严格物理分离平台 SystemRole 与租户 OrganizationRole）。

安全设计原则：
1. 平台级表：`system_roles`、`permissions`、`system_role_permissions`、`user_system_roles`。
2. 租户级表（全部含 `organization_id NOT NULL` 并强制 FORCE RLS）：
   `organization_roles`、`organization_role_permissions`、`user_role_memberships`。
3. `SYSTEM_ADMIN` 与组织角色彻底解耦，绝不隐式具备 `document:read` 等业务知识读取权限。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Permission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """功能权限字典表（平台级）。"""

    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(
        String(96),
        nullable=False,
        unique=True,
        index=True,
        comment="权限唯一编码（如 org:member:manage、document:read）",
    )
    scope: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ORGANIZATION",
        server_default="ORGANIZATION",
        comment="权限作用域（SYSTEM 平台级 / ORGANIZATION 组织级）",
    )
    description: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="权限中文说明",
    )


class SystemRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """平台级系统角色表（如 SYSTEM_ADMIN）。"""

    __tablename__ = "system_roles"

    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="系统角色唯一编码",
    )
    name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="系统角色展示名称",
    )
    description: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="系统角色职责说明",
    )


class SystemRolePermission(Base):
    """系统角色与权限关联表（平台级，与组织角色权限表物理分离）。"""

    __tablename__ = "system_role_permissions"

    system_role_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("system_roles.id", ondelete="CASCADE"),
        primary_key=True,
        comment="系统角色 ID",
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
        comment="功能权限 ID",
    )


class UserSystemRole(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """用户与系统角色绑定表（平台级）。"""

    __tablename__ = "user_system_roles"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "system_role_id",
            name="uq_user_system_roles_user_role",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="用户 ID",
    )
    system_role_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("system_roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="系统角色 ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="绑定状态（ACTIVE / REVOKED）",
    )


class OrganizationRole(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """企业组织角色表（租户级，强制 FORCE RLS）。"""

    __tablename__ = "organization_roles"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "code",
            name="uq_organization_roles_org_code",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="组织内角色唯一编码（如 EMPLOYEE、HR、KNOWLEDGE_MANAGER）",
    )
    name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="组织角色展示名称",
    )
    description: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="组织角色职责说明",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="角色状态（ACTIVE / DISABLED）",
    )


class OrganizationRolePermission(TenantMixin, Base):
    """组织角色与功能权限关联表（租户级，强制 FORCE RLS，与系统角色权限物理分离）。"""

    __tablename__ = "organization_role_permissions"
    __table_args__ = (
        Index(
            "ix_org_role_permissions_org_role",
            "organization_id",
            "organization_role_id",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    organization_role_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organization_roles.id", ondelete="CASCADE"),
        primary_key=True,
        comment="组织角色 ID",
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("permissions.id", ondelete="CASCADE"),
        primary_key=True,
        comment="功能权限 ID",
    )


class UserRoleMembership(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """用户在组织内的角色授予记录（租户级，强制 FORCE RLS，支持生效/到期时间）。"""

    __tablename__ = "user_role_memberships"
    __table_args__ = (
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_range_order",
        ),
        Index(
            "ix_user_role_memberships_org_user",
            "organization_id",
            "user_id",
        ),
        Index(
            "ix_user_role_memberships_org_role",
            "organization_id",
            "organization_role_id",
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
    organization_role_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organization_roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="组织角色 ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="授予状态（ACTIVE / REVOKED）",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="角色生效时间（UTC）",
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="角色到期时间（NULL 表示长期有效）",
    )
