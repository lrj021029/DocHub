"""FAQ 知识沉淀、三轴解耦状态机、来源交集 ACL 与 Fast Path 缓存 ORM 模型。

全部 8 张表均为租户表，强制包含非空 `organization_id` 并开启 PostgreSQL FORCE RLS。
遵循 ADR-001 裁决：
- `lifecycle_state` (`CANDIDATE`/`PENDING_REVIEW`/`REJECTED`/`ARCHIVED`)、
  `publication_state` (`UNPUBLISHED`/`PUBLISHED`/`DISABLED`)、
  `source_health_state` (`HEALTHY`/`STALE`/`EXPIRED`) 三轴完全解耦。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class FAQ(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """FAQ 主对象（三轴状态完全解耦，强制 FORCE RLS）。"""

    __tablename__ = "faqs"
    __table_args__ = (
        Index("ix_faqs_org_space_pub", "organization_id", "space_id", "publication_state"),
        Index("ix_faqs_org_health", "organization_id", "source_health_state"),
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
    lifecycle_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="CANDIDATE",
        server_default="CANDIDATE",
        comment="生命周期状态（CANDIDATE / PENDING_REVIEW / REJECTED / ARCHIVED）",
    )
    publication_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UNPUBLISHED",
        server_default="UNPUBLISHED",
        comment="发布状态（UNPUBLISHED / PUBLISHED / DISABLED）",
    )
    source_health_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="HEALTHY",
        server_default="HEALTHY",
        comment="来源健康状态（HEALTHY / STALE / EXPIRED）",
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="当前生效 FAQVersion ID",
    )
    is_manual: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="是否管理员手工创建（非聚类挖掘生成）",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="创建人 ID（用于严格阻断创建者自审）",
    )
    maintainer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="FAQ 长期维护责任人 ID",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="可选 FAQ 有效期截止时间（UTC）",
    )
    hit_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Fast Path 累计命中次数",
    )


class FAQVersion(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """不可变 FAQ 版本实体（租户表，强制 FORCE RLS）。"""

    __tablename__ = "faq_versions"
    __table_args__ = (
        UniqueConstraint("organization_id", "faq_id", "version_no", name="uq_faq_versions_faq_ver"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    faq_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属 FAQ ID",
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="递增版本号")
    question: Mapped[str] = mapped_column(Text, nullable=False, comment="标准问题")
    answer_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="答案汇总 SHA-256")
    fact_basis: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="手工创建 FAQ 时的必填事实依据说明",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="版本创建人 ID",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间（UTC）",
    )


class FAQSegment(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """结构化 FAQ 答案片段表（支持段落级来源鉴权，强制 FORCE RLS）。"""

    __tablename__ = "faq_segments"
    __table_args__ = (
        Index("ix_faq_segments_org_ver_no", "organization_id", "faq_version_id", "segment_no"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    faq_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faq_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属 FAQVersion ID",
    )
    segment_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    content_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="MARKDOWN",
        server_default="MARKDOWN",
        comment="片段内容格式",
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False, comment="FAQ 答案片段正文")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="片段附加元数据",
    )


class FAQSourceRef(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """FAQ 段落来源文档/Chunk 映射表（用于权限交集复核与来源过期感知，强制 FORCE RLS）。"""

    __tablename__ = "faq_source_refs"
    __table_args__ = (
        Index("ix_faq_source_refs_org_seg", "organization_id", "faq_segment_id"),
        Index("ix_faq_source_refs_org_doc", "organization_id", "document_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    faq_segment_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faq_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属 FAQSegment ID",
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="来源文档 ID",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        comment="来源文档版本 ID",
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="SET NULL"),
        nullable=True,
        comment="来源 Chunk ID",
    )
    citation_ref: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="可读引用标识摘要",
    )


class FAQACL(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """FAQ 独立访问控制快照表（默认遵循来源文档最严格权限交集，强制 FORCE RLS）。"""

    __tablename__ = "faq_acls"
    __table_args__ = (UniqueConstraint("organization_id", "faq_id", name="uq_faq_acls_org_faq"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    faq_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联 FAQ ID",
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="FAQ ACL 策略快照（不可扩大敏感来源访问范围）",
    )


class FAQReview(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """FAQ 人工审核历史表（强制不可自审，租户表，强制 FORCE RLS）。"""

    __tablename__ = "faq_reviews"
    __table_args__ = (Index("ix_faq_reviews_org_faq", "organization_id", "faq_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    faq_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faq_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="被审核的 FAQVersion ID",
    )
    faq_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联 FAQ ID",
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="审核人 ID（严禁等于 FAQ 创建者）",
    )
    decision: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="审核结论（APPROVED / REJECTED / NEEDS_REVISION）",
    )
    review_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="CONTENT_AND_ACL",
        server_default="CONTENT_AND_ACL",
        comment="审核类型",
    )
    comments: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="审核意见")
    policy_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="v1",
        server_default="v1",
        comment="审核依据的合规策略版本",
    )
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="审核时间（UTC）",
    )


class FAQPublishRecord(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """FAQ 发布操作事实记录表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "faq_publish_records"
    __table_args__ = (Index("ix_faq_publish_records_org_faq", "organization_id", "faq_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    faq_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联 FAQ ID",
    )
    faq_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faq_versions.id", ondelete="CASCADE"),
        nullable=False,
        comment="发布的 FAQVersion ID",
    )
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="发布操作人 ID",
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="发布时间（UTC）",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="PUBLISHED",
        server_default="PUBLISHED",
        comment="发布事实状态（PUBLISHED / DISABLED）",
    )


class FAQCacheIndex(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """FAQ Fast Path Redis 缓存版本映射表（受 Outbox 事件驱动失效，强制 FORCE RLS）。"""

    __tablename__ = "faq_cache_indexes"
    __table_args__ = (
        UniqueConstraint("organization_id", "faq_id", name="uq_faq_cache_indexes_org_faq"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    faq_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联 FAQ ID",
    )
    faq_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faq_versions.id", ondelete="CASCADE"),
        nullable=False,
        comment="当前缓存版本 ID",
    )
    acl_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    source_version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="缓存失效代次号（每次文档变更或 ACL 变更递增）",
    )
