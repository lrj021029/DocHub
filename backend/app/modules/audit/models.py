"""审计治理领域 ORM 模型（业务审计 `audit_events` 与安全审计 `security_audit_events` 物理分离）。

遵循 TECHNICAL_SPEC §14.5 & ADR-002：
1. 业务审计与安全审计分表存储，安全审计读取权限仅授权给 `SECURITY_AUDITOR`。
2. 两张表均为租户表，强制包含 `organization_id NOT NULL` 并启用 PostgreSQL FORCE RLS。
3. 严禁在 `details_json` 中写入明文密钥、密码、未授权文档正文或未授权文档标题。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UUIDPrimaryKeyMixin


class AuditEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """业务操作审计事件表（不可变追加，强制 FORCE RLS）。"""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_org_created", "organization_id", "created_at"),
        Index("ix_audit_events_org_action", "organization_id", "action"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="操作发起用户 ID",
    )
    action: Mapped[str] = mapped_column(
        String(96),
        nullable=False,
        comment="业务动作编码（如 org.member.added、department.moved）",
    )
    resource_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="目标资源类型（如 organization_member、department、organization_role）",
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="目标资源 ID 或受限端点路径",
    )
    request_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="关联请求追踪 ID",
    )
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="脱敏后的结构化审计元数据",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="事件记录时间（UTC）",
    )


class SecurityAuditEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """高敏感安全审计事件表（Refresh 重放、越权尝试、ACL 拒绝、权限变更，强制 FORCE RLS）。"""

    __tablename__ = "security_audit_events"
    __table_args__ = (
        Index("ix_security_audit_events_org_created", "organization_id", "created_at"),
        Index("ix_security_audit_events_org_type", "organization_id", "event_type"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="触发安全事件的用户 ID",
    )
    event_type: Mapped[str] = mapped_column(
        String(96),
        nullable=False,
        comment="安全事件类型（如 auth.refresh.replay_detected、authz.permission_denied）",
    )
    severity: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="MEDIUM",
        server_default="MEDIUM",
        comment="严重级别（LOW / MEDIUM / HIGH / CRITICAL）",
    )
    resource_type: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="关联资源类型",
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="关联资源 ID 或端点路径",
    )
    request_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="关联请求追踪 ID",
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="客户端来源 IP",
    )
    details_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="脱敏后的安全取证上下文（严禁含密钥或明文凭据）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="事件记录时间（UTC）",
    )
