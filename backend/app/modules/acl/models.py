"""四维 Document ACL 与 Break-glass 紧急访问 ORM 模型（全部强制启用 PostgreSQL FORCE RLS）。

包含：
1. `document_acls`：文档当前 ACL 聚合根（带乐观锁 `revision`）。
2. `acl_grants`：四维 Allow OR 授权项（GLOBAL / DEPARTMENT / ROLE / USER，无 Deny Override）。
3. `acl_grant_department_snapshots`：`SNAPSHOT` 模式下授权时刻展开的部门 ID 集合快照。
4. `acl_revisions`：不可变 ACL 修订历史（含完整快照 JSON、Diff、变更人与必填理由）。
5. `break_glass_grants`：紧急访问授权（默认关闭，需显式审批、理由、时限并记安全审计）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentACL(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """文档 ACL 聚合根（每篇文档唯一，变更时通过 SELECT ... FOR UPDATE 加行锁递增 revision）。"""

    __tablename__ = "document_acls"
    __table_args__ = (
        UniqueConstraint("organization_id", "document_id", name="uq_document_acls_org_doc"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属文档 ID",
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="ACL 乐观锁与同步修订号（初始为 0，每次有效配置递增 1）",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="ACL 状态（ACTIVE / LOCKED）",
    )


class ACLGrant(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """四维 Allow OR 授权项（GLOBAL / DEPARTMENT / ROLE / USER）。"""

    __tablename__ = "acl_grants"
    __table_args__ = (
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_range_order",
        ),
        Index("ix_acl_grants_org_acl", "organization_id", "acl_id"),
        Index("ix_acl_grants_org_subject", "organization_id", "subject_type", "subject_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    acl_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_acls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属 DocumentACL ID",
    )
    subject_type: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="授权主体类型（GLOBAL / DEPARTMENT / ROLE / USER）",
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="强类型主体 ID（GLOBAL 时为 NULL，其余为部门/组织角色/用户 UUID）",
    )
    include_descendants: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="部门授权是否包含下级子部门（仅 DEPARTMENT 有效）",
    )
    inheritance_mode: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="DYNAMIC",
        server_default="DYNAMIC",
        comment="部门继承模式（DYNAMIC 实时闭包表 / SNAPSHOT 授权时刻快照）",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="授权生效时间（UTC）",
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="授权到期时间（UTC，NULL 表示长期）",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="配置该授权的管理员用户 ID",
    )


class ACLGrantDepartmentSnapshot(TenantMixin, Base):
    """SNAPSHOT 继承模式下，授权瞬间展开的部门 ID 集合快照（租户表，强制 FORCE RLS）。"""

    __tablename__ = "acl_grant_department_snapshots"
    __table_args__ = (
        Index("ix_acl_grant_dept_snap_org_grant", "organization_id", "acl_grant_id"),
        Index("ix_acl_grant_dept_snap_org_dept", "organization_id", "department_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    acl_grant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("acl_grants.id", ondelete="CASCADE"),
        primary_key=True,
        comment="关联的 ACLGrant ID",
    )
    department_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("departments.id", ondelete="CASCADE"),
        primary_key=True,
        comment="授权瞬间被包含的部门 ID",
    )


class ACLRevision(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """不可变 ACL 修订历史与 Diff 记录表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "acl_revisions"
    __table_args__ = (
        UniqueConstraint("organization_id", "acl_id", "revision", name="uq_acl_revisions_acl_rev"),
        Index("ix_acl_revisions_org_acl", "organization_id", "acl_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    acl_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_acls.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的 DocumentACL ID",
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, comment="递增修订号")
    snapshot_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="本次修订后的完整授权项快照",
    )
    diff_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="相较上一版本的增删 Diff",
    )
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="变更操作人 ID",
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False, comment="必填变更理由")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="修订提交时间（UTC）",
    )


class BreakGlassGrant(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """限时紧急访问授权表（默认关闭/拒绝，需显式审批、理由、时限并记安全审计）。"""

    __tablename__ = "break_glass_grants"
    __table_args__ = (
        Index("ix_break_glass_org_user_status", "organization_id", "user_id", "status"),
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
        comment="被授予紧急访问权限的用户 ID",
    )
    scope: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="紧急访问作用域（例如 document:<uuid>）",
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False, comment="紧急访问事由")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="紧急访问绝对到期时间（UTC）",
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="独立审批人 ID（未审批时不得生效）",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PENDING",
        server_default="PENDING",
        comment="紧急访问状态（PENDING / APPROVED / REVOKED / EXPIRED）",
    )
