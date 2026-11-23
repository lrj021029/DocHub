"""事务性发件箱（Outbox）、消费者幂等收件箱（Inbox）与 API 幂等记录 ORM 模型。

全部为租户表，强制包含非空 `organization_id` 并开启 PostgreSQL FORCE RLS。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UUIDPrimaryKeyMixin


class OutboxEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """事务性发件箱事件表（与业务写操作同事务提交，供 Worker 异步投递）。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_org_published", "organization_id", "published_at"),
        Index("ix_outbox_events_org_type", "organization_id", "event_type"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="聚合根类型")
    aggregate_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="聚合根 ID",
    )
    aggregate_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="聚合根修订号（用于消费者乱序去重）",
    )
    event_type: Mapped[str] = mapped_column(
        String(96),
        nullable=False,
        comment="领域事件类型（如 acl.revision.created、document.uploaded）",
    )
    payload_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="事件载荷版本号",
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="脱敏后的结构化事件载荷",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="事件创建时间（UTC）",
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="成功投递时间（NULL 表示待投递）",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="累计投递尝试次数",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最近失败原因")


class ProcessedMessage(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """消费者幂等收件箱表（防止至少一次投递导致重复消费，强制 FORCE RLS）。"""

    __tablename__ = "processed_messages"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "consumer_name",
            "event_id",
            name="uq_processed_messages_consumer_event",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    consumer_name: Mapped[str] = mapped_column(String(96), nullable=False, comment="消费者标识")
    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="已处理的 OutboxEvent ID",
    )
    aggregate_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="已处理聚合修订号",
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="消费完成时间（UTC）",
    )


class IdempotencyRecord(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """API 写操作幂等键持久化表（作用域绑定 principal + organization + operation）。"""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "fingerprint",
            name="uq_idempotency_records_org_fp",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="发起操作的用户 ID",
    )
    operation: Mapped[str] = mapped_column(String(96), nullable=False, comment="幂等操作名称")
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="客户端幂等键"
    )
    fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="principal + organization + operation + key 的 SHA-256 指纹",
    )
    request_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="请求体摘要（用于检测同键不同参冲突）",
    )
    response_status: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="缓存 HTTP 状态码"
    )
    response_body_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="幂等重放响应体",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="记录创建时间（UTC）",
    )
