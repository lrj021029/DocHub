"""文档、不可变版本、分阶段处理任务、解析产物、结构化 Block 与 Parent-Child Chunk ORM 模型。

安全与架构约束（ADR-002 & TECHNICAL_SPEC §5.3 / §7.6）：
1. 全部 8 张表均为租户表，强制包含非空 `organization_id` 并开启 PostgreSQL FORCE RLS。
2. 新导入文档默认处于 `QUARANTINED` 状态，未配置有效 ACL 并显式发布前严禁被检索召回。
3. ImportJob / ParseJob / ChunkSet 状态完全拆分，绝不以单一总 status 掩盖真实阶段状态。
4. `chunks.content_text` 为权威正文存储，必须经 PostgreSQL ACL 复核后方可读取送入 Reranker/LLM。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Document(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """文档逻辑主对象（租户表，强制 FORCE RLS）。"""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_org_space_status", "organization_id", "space_id", "status"),
        Index("ix_documents_org_sensitivity", "organization_id", "sensitivity_level"),
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
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="文档标题")
    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="UPLOAD",
        server_default="UPLOAD",
        comment="文档来源类型（UPLOAD / BATCH_IMPORT / API）",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="QUARANTINED",
        server_default="QUARANTINED",
        comment="文档生命周期状态（QUARANTINED 隔离待授权 / ACTIVE 可用 / ARCHIVED 已归档）",
    )
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="当前生效文档版本 ID（由发布流程原子指向不可变 DocumentVersion）",
    )
    sensitivity_level: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="L1",
        server_default="L1",
        comment="数据敏感等级（L0 公开 / L1 内部 / L2 机密默认本地 / L3 绝密严禁外发）",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="文档创建人 ID",
    )


class DocumentVersion(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """不可变文档版本实体（租户表，强制 FORCE RLS）。"""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "document_id",
            "version_no",
            name="uq_document_versions_doc_ver",
        ),
        Index("ix_document_versions_org_doc", "organization_id", "document_id"),
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
        comment="所属逻辑文档 ID",
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="递增版本号")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, comment="清洗后的安全文件名")
    source_object_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="MinIO 原始对象存储键（服务端防路径穿越生成）",
    )
    file_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="服务端核验后的原始文件 SHA-256 摘要",
    )
    mime_type: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="校验后的 MIME 类型"
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="服务端核验后的文件字节数",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="上传人 ID",
    )


class ImportJob(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """导入校验任务状态机（CREATED->UPLOADING->VERIFYING->QUARANTINED/REJECTED/FAILED）。"""

    __tablename__ = "import_jobs"
    __table_args__ = (
        Index("ix_import_jobs_org_version", "organization_id", "document_version_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的文档版本 ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="CREATED",
        server_default="CREATED",
        comment="导入状态（CREATED/UPLOADING/VERIFYING/QUARANTINED/REJECTED/FAILED）",
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="关联客户端幂等键",
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="错误码")
    error_message: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="脱敏后的失败说明",
    )


class ParseJob(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """文档解析任务状态机（独立追踪 MinerU 外部任务 ID、重试与 BLOCKED_POLICY 策略阻断）。"""

    __tablename__ = "parse_jobs"
    __table_args__ = (
        Index("ix_parse_jobs_org_version", "organization_id", "document_version_id"),
        Index("ix_parse_jobs_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的文档版本 ID",
    )
    parser_provider: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="auto",
        server_default="auto",
        comment="解析引擎标识（mineru_official / local_ocr / lightweight_docx 等）",
    )
    parser_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="解析器版本",
    )
    external_task_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
        comment="外部 MinerU 异步任务 ID（崩溃恢复对账凭据）",
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="解析任务幂等键",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="QUEUED",
        server_default="QUEUED",
        comment=(
            "解析状态（QUEUED/SUBMITTING/SUBMITTED/RUNNING/SUCCEEDED/"
            "FAILED/CANCEL_REQUESTED/CANCELLED/UNKNOWN/BLOCKED_POLICY）"
        ),
    )
    tier: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="解析档位")
    error_detail: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="脱敏后的错误或策略阻断原因",
    )


class ParsedArtifact(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """解析产物存储记录（JSON / Markdown / 原始压缩包 / PDF 页面渲染等，强制 FORCE RLS）。"""

    __tablename__ = "parsed_artifacts"
    __table_args__ = (
        Index("ix_parsed_artifacts_org_version", "organization_id", "document_version_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的文档版本 ID",
    )
    artifact_type: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        comment="产物类型（markdown / structured_json / raw_zip / page_image）",
    )
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, comment="MinIO 对象键")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="产物 SHA-256")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="产物附加元数据",
    )


class ParsedBlock(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """标准化文档解析结构化 Block（含页码、标题路径与 Bounding Box，强制 FORCE RLS）。"""

    __tablename__ = "parsed_blocks"
    __table_args__ = (
        Index(
            "ix_parsed_blocks_org_version_seq", "organization_id", "document_version_id", "sequence"
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的文档版本 ID",
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    block_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="块类型（heading / paragraph / table / formula / list / code / image）",
    )
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="来源页码")
    heading_path: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="章节标题层级路径",
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False, comment="标准化块正文")
    bounding_box: Mapped[list[float] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="页内 Bounding Box 坐标（无精确坐标时为 null，绝不伪造）",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="解析块附加元数据",
    )


class ChunkSet(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """一套切片结果集（绑定 chunker_version 与 config_hash，强制 FORCE RLS）。"""

    __tablename__ = "chunk_sets"
    __table_args__ = (Index("ix_chunk_sets_org_version", "organization_id", "document_version_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的文档版本 ID",
    )
    chunker_version: Mapped[str] = mapped_column(String(48), nullable=False, comment="切片器版本")
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="切片参数摘要")
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="QUEUED",
        server_default="QUEUED",
        comment="切片状态（QUEUED / RUNNING / SUCCEEDED / FAILED）",
    )


class Chunk(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Parent-Child 结构化切片与权威正文表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_org_doc_ver", "organization_id", "document_version_id"),
        Index("ix_chunks_org_chunk_set", "organization_id", "chunk_set_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    chunk_set_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chunk_sets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属 ChunkSet ID",
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的不可变文档版本 ID",
    )
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="父切片 ID（Parent Chunk 为 NULL，Child Chunk 指向其父块）",
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    content_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="权威切片正文（仅允许经 PostgreSQL ACL 复核通过后读取）",
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="正文 SHA-256")
    token_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="估算 Token 数量",
    )
    source_block_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="来源 ParsedBlock ID 列表",
    )
    page_range: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="起止页码区间",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="附加切片元数据（标题路径、块类型等）",
    )
    indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="写入 Qdrant 索引完成时间（UTC）",
    )
