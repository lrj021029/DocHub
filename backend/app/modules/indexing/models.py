"""Embedding Provider/Profile 与蓝绿索引版本治理 ORM 模型（全部强制启用 PostgreSQL FORCE RLS）。

包含：
1. `embedding_providers`：组织级向量模型 Provider 注册记录（绝不存明文密钥，仅存引用标识）。
2. `embedding_profiles`：向量模型配置（model_id、dimensions、language_config）。
3. `index_generations`：Qdrant 蓝绿索引构建代次（V1 Active 不被覆盖，V2 Staging 全量构建）。
4. `active_index_pointers`：当前激活索引指针（带乐观锁 `revision`）。
5. `index_jobs`：索引构建与校验任务状态机。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class EmbeddingProvider(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """向量模型 Provider 注册表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "embedding_providers"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_embedding_providers_org_code"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="Provider 短标识")
    provider_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Provider 类型（qwen / local_bge / openai_compatible）",
    )
    base_url_ref: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="配置引用键或脱敏端点标识（严禁存储明文 API Key）",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="是否启用",
    )


class EmbeddingProfile(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """向量模型 Profile 配置表（按 Profile 共享 Qdrant Collection，强制 FORCE RLS）。"""

    __tablename__ = "embedding_profiles"
    __table_args__ = (
        UniqueConstraint("organization_id", "code", name="uq_embedding_profiles_org_code"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_providers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的 EmbeddingProvider ID",
    )
    code: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        comment="Profile 唯一标识（如 qwen-text-embedding-v3、local-bge-m3）",
    )
    model_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="模型标识")
    model_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="v1",
        server_default="v1",
        comment="模型版本",
    )
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False, comment="向量维度")
    language_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="语言与稀疏分词配置",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="Profile 状态（ACTIVE / DEPRECATED）",
    )


class IndexGeneration(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Qdrant 蓝绿索引构建代次表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "index_generations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "embedding_profile_id",
            "generation_no",
            name="uq_index_generations_profile_gen",
        ),
        Index("ix_index_generations_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    embedding_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="绑定的 EmbeddingProfile ID",
    )
    collection_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="对应的 Qdrant 共享 Collection 名称",
    )
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="递增代次号")
    chunker_version: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        default="v1",
        server_default="v1",
        comment="切片器版本",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="CREATED",
        server_default="CREATED",
        comment="索引代次状态（CREATED/BUILDING/VALIDATING/READY/ACTIVE/RETIRED/ROLLED_BACK/FAILED）",
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="构建与评测指标摘要",
    )


class ActiveIndexPointer(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """当前激活索引指针表（带乐观锁 revision，强制 FORCE RLS）。"""

    __tablename__ = "active_index_pointers"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "scope_type",
            "scope_id",
            name="uq_active_index_pointers_scope",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    scope_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="KNOWLEDGE_SPACE",
        server_default="KNOWLEDGE_SPACE",
        comment="指针作用域类型（KNOWLEDGE_SPACE / ORGANIZATION）",
    )
    scope_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        index=True,
        comment="作用域实体 ID（如 space_id）",
    )
    embedding_profile_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        comment="当前激活的 EmbeddingProfile ID",
    )
    index_generation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_generations.id", ondelete="RESTRICT"),
        nullable=False,
        comment="当前激活的 IndexGeneration ID",
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="蓝绿切换乐观锁修订号",
    )


class IndexJob(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """索引构建任务表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "index_jobs"
    __table_args__ = (Index("ix_index_jobs_org_gen", "organization_id", "generation_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_generations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联的 IndexGeneration ID",
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="可选关联的文档版本 ID（增量索引场景）",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="CREATED",
        server_default="CREATED",
        comment="索引任务状态（CREATED/BUILDING/VALIDATING/READY/ACTIVE/FAILED）",
    )
    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="真实处理完成百分比（0~100，严禁伪造假进度）",
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="错误码")
    error_message: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="脱敏后的失败原因",
    )
