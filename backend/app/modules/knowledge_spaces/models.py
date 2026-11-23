"""Knowledge Space 领域 ORM 模型（全部为租户表，强制启用 PostgreSQL FORCE RLS）。

包含：
1. `knowledge_spaces`：知识空间主实体（visibility、answer_policy、默认 Profile、配置版本）。
2. `space_memberships`：Private Space 显式成员表（支持 `valid_from`/`valid_until`/`status`）。
3. `space_policies`：Space 入口与治理策略快照表（带 revision）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class KnowledgeSpace(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """知识空间实体（租户表，强制 FORCE RLS）。"""

    __tablename__ = "knowledge_spaces"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_knowledge_spaces_org_slug"),
        Index("ix_knowledge_spaces_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False, comment="知识空间名称")
    slug: Mapped[str] = mapped_column(String(96), nullable=False, comment="组织内唯一短标识")
    description: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="知识空间描述",
    )
    visibility: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PRIVATE",
        server_default="PRIVATE",
        comment="空间可见性（PUBLIC 仅限本组织成员 / PRIVATE 仅限显式成员）",
    )
    answer_policy: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        default="STRICT_EVIDENCE",
        server_default="STRICT_EVIDENCE",
        comment="AI 问答策略（STRICT_EVIDENCE / GROUNDED_WITH_GENERAL_CONTEXT）",
    )
    default_embedding_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_profiles.id", ondelete="SET NULL"),
        nullable=True,
        comment="默认绑定的向量模型 Profile ID",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="空间状态（ACTIVE / ARCHIVED）",
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="空间级扩展配置（切片参数、检索覆盖项等）",
    )
    config_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="配置乐观锁版本号",
    )


class SpaceMembership(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """私有知识空间显式成员关系表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "space_memberships"
    __table_args__ = (
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_range_order",
        ),
        UniqueConstraint(
            "organization_id",
            "space_id",
            "user_id",
            name="uq_space_memberships_org_space_user",
        ),
        Index("ix_space_memberships_org_space", "organization_id", "space_id"),
        Index("ix_space_memberships_org_user", "organization_id", "user_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属知识空间 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="显式成员用户 ID",
    )
    role: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="MEMBER",
        server_default="MEMBER",
        comment="空间内成员角色（MEMBER / ADMIN）",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="成员状态（ACTIVE / REVOKED）",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="生效时间（UTC）",
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="到期时间（UTC，空表示长期有效）",
    )


class SpacePolicy(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """知识空间入口与外发策略表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "space_policies"
    __table_args__ = (
        UniqueConstraint("organization_id", "space_id", name="uq_space_policies_org_space"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属知识空间 ID",
    )
    policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="空间入口与数据治理策略配置",
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="策略修订号",
    )
