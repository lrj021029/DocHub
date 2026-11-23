"""治理、组织级 Provider 外发策略与 Benchmark 评测 ORM 模型（全部强制启用 FORCE RLS）。"""

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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class OrganizationProviderPolicy(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """组织级 Provider 路由偏好与外发授权策略表（绝不存储任何明文 API Key，强制 FORCE RLS）。"""

    __tablename__ = "organization_provider_policies"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_org_provider_policies_org"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    allow_l1_external_egress: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="组织是否明确授权 L1 内部数据调用受控外部 Provider",
    )
    embedding_primary_provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="qwen",
        server_default="qwen",
        comment="首选 Embedding Provider（qwen / local_bge）",
    )
    reranker_primary_provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="qwen",
        server_default="qwen",
        comment="首选 Reranker Provider（qwen / local_bge）",
    )
    local_bge_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="组织是否开启本地 BGE 服务路由",
    )
    local_ocr_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="组织是否开启本地 OCR 解析路由（关闭时 L2/L3 扫描件强制 BLOCKED_POLICY）",
    )
    llm_base_url_override: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="组织级 LLM Base URL 覆盖（不含密钥）",
    )
    llm_model_override: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="组织级默认 LLM 模型名覆盖",
    )


class BenchmarkRun(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """RAG / Parser / ACL 回归基准评测运行记录表（强制 FORCE RLS）。"""

    __tablename__ = "benchmark_runs"
    __table_args__ = (Index("ix_benchmark_runs_org_created", "organization_id", "created_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    run_type: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        default="RAG",
        server_default="RAG",
        comment="评测套件类型（RAG / PARSER / ACL）",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="COMPLETED",
        server_default="COMPLETED",
        comment="运行状态（RUNNING / COMPLETED / FAILED）",
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="Recall@K、MRR、Citation Accuracy、跨租户泄露计数等指标",
    )
    acl_violation_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="安全硬门槛：ACL 越权违规次数（必须严格等于 0）",
    )
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="触发用户 ID",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="运行时间（UTC）",
    )


class BenchmarkCaseResult(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """Benchmark 单条测试用例结果明细表（强制 FORCE RLS）。"""

    __tablename__ = "benchmark_case_results"
    __table_args__ = (Index("ix_benchmark_case_results_org_run", "organization_id", "run_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("benchmark_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属 BenchmarkRun ID",
    )
    case_name: Mapped[str] = mapped_column(String(160), nullable=False, comment="用例名称")
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="单条用例评分明细",
    )
    detail_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="执行诊断摘要",
    )
