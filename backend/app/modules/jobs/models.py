"""统一异步任务追踪视图 ORM 模型（`async_jobs`，租户表，强制 FORCE RLS）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AsyncJob(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """跨领域异步任务统一追踪视图（与具体 ImportJob/ParseJob/IndexJob 领域状态机互补）。"""

    __tablename__ = "async_jobs"
    __table_args__ = (
        Index("ix_async_jobs_org_type_status", "organization_id", "job_type", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    job_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="任务类型（DOCUMENT_IMPORT / DOCUMENT_PARSE / INDEX_BUILD / FAQ_MINING）",
    )
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, comment="目标资源类型")
    target_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="目标资源 ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="QUEUED",
        server_default="QUEUED",
        comment="任务状态（QUEUED / RUNNING / SUCCEEDED / FAILED / BLOCKED_POLICY / CANCELLED）",
    )
    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="真实进度百分比（0~100）",
    )
    celery_task_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="关联 Celery 任务 ID",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="任务发起人 ID",
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="任务附加元数据",
    )
