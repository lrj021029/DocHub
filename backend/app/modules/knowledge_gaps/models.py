"""FAQ Mining 与 Knowledge Gap 闭环治理 ORM 模型（全部强制启用 PostgreSQL FORCE RLS）。

包含：
1. `question_events`：脱敏问题事件流（PII 脱敏，并显式区分 outcome 避免把 ACL_DENIED 误归为 Gap）。
2. `question_clusters`：HDBSCAN（`sklearn.cluster.HDBSCAN`）聚类簇。
3. `question_cluster_members`：问题与簇成员隶属关系。
4. `faq_candidates`：基于授权来源生成的 FAQ 候选。
5. `knowledge_gaps`：11 态知识缺口记录（不含 ACL_DENIED 与服务故障事件）。
6. `knowledge_gap_tasks`：知识补全整改任务（关联负责人、截止时间、补全文档与回归验证证据）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class QuestionEvent(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """脱敏问题事件表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "question_events"
    __table_args__ = (
        Index("ix_question_events_org_space_created", "organization_id", "space_id", "created_at"),
        Index("ix_question_events_org_outcome", "organization_id", "outcome"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="目标知识空间 ID",
    )
    user_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="脱敏用户单向哈希")
    session_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="脱敏会话单向哈希"
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="提问者主部门 ID（用于部门覆盖度统计）",
    )
    normalized_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="PII 脱敏并归一化后的问题文本",
    )
    sensitivity_level: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="L1",
        server_default="L1",
        comment="问题关联敏感等级",
    )
    outcome: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment=(
            "事件归因（ANSWERED / FAQ_HIT / NO_RETRIEVAL / LOW_RELEVANCE / "
            "INSUFFICIENT_EVIDENCE / ACL_DENIED / PROVIDER_FAILURE）"
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="事件记录时间（UTC）",
    )


class QuestionCluster(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """HDBSCAN 问题聚类簇表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "question_clusters"
    __table_args__ = (Index("ix_question_clusters_org_space", "organization_id", "space_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="关联知识空间 ID",
    )
    scope_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="聚类范围键")
    algorithm_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="sklearn-hdbscan-v1",
        server_default="sklearn-hdbscan-v1",
        comment="聚类算法与参数版本",
    )
    cluster_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    representative_question: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="簇内代表性标准问法",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="簇状态（ACTIVE / MERGED / ARCHIVED）",
    )
    metrics_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="频次、独立用户数、独立会话数与部门覆盖度统计",
    )


class QuestionClusterMember(TenantMixin, Base):
    """问题事件与聚类簇关联表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "question_cluster_members"
    __table_args__ = (
        Index("ix_question_cluster_members_org_cluster", "organization_id", "cluster_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("question_clusters.id", ondelete="CASCADE"),
        primary_key=True,
        comment="聚类簇 ID",
    )
    question_event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("question_events.id", ondelete="CASCADE"),
        primary_key=True,
        comment="问题事件 ID",
    )
    membership_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        default=1.0,
        server_default="1.0",
        comment="聚类归属置信分值",
    )


class FAQCandidate(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """FAQ 挖掘候选项表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "faq_candidates"
    __table_args__ = (Index("ix_faq_candidates_org_status", "organization_id", "candidate_status"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("question_clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="来源聚类簇 ID",
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="所属知识空间 ID",
    )
    faq_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="提交审核后关联生成的 FAQ ID",
    )
    candidate_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="PENDING_SUBMIT",
        server_default="PENDING_SUBMIT",
        comment="候选状态（PENDING_SUBMIT / SUBMITTED / APPROVED / REJECTED）",
    )
    generated_question: Mapped[str] = mapped_column(Text, nullable=False, comment="候选标准问题")
    generated_answer_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="基于授权证据生成的结构化候选答案与引用",
    )
    confidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="多维置信度评估明细（频次、引用完整性、证据覆盖率）",
    )


class KnowledgeGap(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """知识缺口主对象（11 态生命周期，绝不录入 ACL_DENIED，强制 FORCE RLS）。"""

    __tablename__ = "knowledge_gaps"
    __table_args__ = (
        Index("ix_knowledge_gaps_org_status", "organization_id", "status"),
        Index("ix_knowledge_gaps_org_space", "organization_id", "space_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    space_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="所属知识空间 ID",
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("question_clusters.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="关联问题聚类簇 ID",
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="缺口主题描述")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="DETECTED",
        server_default="DETECTED",
        comment=(
            "11 态缺口状态（DETECTED/TRIAGED/ASSIGNED/IN_PROGRESS/CONTENT_SUBMITTED/"
            "REVIEWING/RESOLVED/DUPLICATED/REJECTED/CLOSED/REOPENED）"
        ),
    )
    priority: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="MEDIUM",
        server_default="MEDIUM",
        comment="优先级（LOW / MEDIUM / HIGH / CRITICAL）",
    )
    question_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    unique_user_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class KnowledgeGapTask(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """知识缺口整改补充任务表（强制绑定回归验证证据方允许 RESOLVED，强制 FORCE RLS）。"""

    __tablename__ = "knowledge_gap_tasks"
    __table_args__ = (Index("ix_knowledge_gap_tasks_org_gap", "organization_id", "gap_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    gap_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_gaps.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联 KnowledgeGap ID",
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="整改责任人 ID",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ASSIGNED",
        server_default="ASSIGNED",
        comment="任务状态（ASSIGNED/IN_PROGRESS/CONTENT_SUBMITTED/REVIEWING/RESOLVED/CLOSED）",
    )
    priority: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="MEDIUM",
        server_default="MEDIUM",
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    related_document_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
        comment="整改新增或修订的关联文档 ID",
    )
    related_faq_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="SET NULL"),
        nullable=True,
        comment="整改补充的关联 FAQ ID",
    )
    resolution_evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="缺口回归验证查询（Gap Regression Query）结果与验收证据",
    )
