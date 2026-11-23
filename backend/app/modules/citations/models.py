"""引用溯源 ORM 模型（`answer_citations`，强制启用 PostgreSQL FORCE RLS）。

遵循 TECHNICAL_SPEC §9.6：
- 绑定 `document_id`、`document_version_id`、`chunk_id`、`block_id`、`page_number`、
  `bbox_json`、`quote_hash` 与当时 `acl_revision`。
- 点击引用查看详情时必须再次通过 `AuthoritativeACLEngine` 实时重鉴权，不得只凭引用 ID 放行。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, UUIDPrimaryKeyMixin


class AnswerCitation(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """回答片段精确引用溯源表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "answer_citations"
    __table_args__ = (
        Index("ix_answer_citations_org_msg", "organization_id", "message_id"),
        Index("ix_answer_citations_org_seg", "organization_id", "message_segment_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    message_segment_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("message_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属消息片段 ID",
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属消息 ID",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="引用来源逻辑文档 ID",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        comment="引用来源不可变版本 ID",
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
        comment="引用来源 Chunk ID",
    )
    block_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("parsed_blocks.id", ondelete="SET NULL"),
        nullable=True,
        comment="引用来源结构化 ParsedBlock ID",
    )
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="精确页码")
    bbox_json: Mapped[list[float] | dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="PDF 页面高亮 Bounding Box（无精确坐标时为 null，绝不伪造）",
    )
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="引用摘录 SHA-256")
    acl_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="回答生成时校验通过的 ACL 版本号",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间（UTC）",
    )
