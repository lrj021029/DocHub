"""AI 对话、可重鉴权段落、检索审计与模型用量 ORM 模型（全部强制启用 PostgreSQL FORCE RLS）。

包含：
1. `conversations`：多轮对话会话。
2. `conversation_spaces`：会话绑定的目标知识空间列表。
3. `messages`：消息实体（含 `answer_policy_used`、`faq_id`、`is_splittable`）。
4. `message_segments`：支持逐段历史重鉴权的内容片段（记录 `source_document_ids` 与最高敏感度）。
5. `retrieval_audits`：每次问答检索审计摘要（不存明文敏感 Query，仅存 hash）。
6. `retrieval_candidates`：候选 Chunk 引用（绝不保存未授权正文！）。
7. `retrieval_decisions`：权威 ACL 授权判定明细（`ALLOW`/`DENY` + `reason_code`）。
8. `llm_usages`：LLM 调用 Token 与耗时审计。
9. `message_feedbacks`：用户对回答的 `HELPFUL` / `UNHELPFUL` 质量反馈。
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


class Conversation(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """AI 问答会话表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_org_user", "organization_id", "user_id"),)

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
        comment="会话所属用户 ID",
    )
    title: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="新对话",
        server_default="新对话",
        comment="会话标题",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="会话状态（ACTIVE / ARCHIVED）",
    )


class ConversationSpace(TenantMixin, Base):
    """会话选择的知识空间绑定表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "conversation_spaces"
    __table_args__ = (
        Index("ix_conversation_spaces_org_conv", "organization_id", "conversation_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
        comment="关联会话 ID",
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_spaces.id", ondelete="CASCADE"),
        primary_key=True,
        comment="选中的知识空间 ID",
    )


class Message(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """对话消息表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_org_conv_created", "organization_id", "conversation_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属会话 ID",
    )
    role: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="消息角色（USER / ASSISTANT / SYSTEM）",
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
        comment="消息文本内容",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="COMPLETED",
        server_default="COMPLETED",
        comment="消息状态（STREAMING / COMPLETED / CANCELLED / FAILED）",
    )
    answer_policy_used: Mapped[str | None] = mapped_column(
        String(48),
        nullable=True,
        comment="本轮使用的回答策略（STRICT_EVIDENCE / GROUNDED_WITH_GENERAL_CONTEXT）",
    )
    faq_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("faqs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="若命中 FAQ Fast Path，记录关联 FAQ ID",
    )
    is_splittable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="是否支持按段落重鉴权拆分（false 时撤权后整条隐藏）",
    )


class MessageSegment(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """支持历史会话动态逐段重鉴权的消息片段表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "message_segments"
    __table_args__ = (
        Index("ix_message_segments_org_msg_seq", "organization_id", "message_id", "sequence"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属消息 ID",
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="片段正文")
    segment_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="EVIDENCE",
        server_default="EVIDENCE",
        comment="片段类型（EVIDENCE / GENERAL / ABSTENTION / FAQ_DIRECT）",
    )
    source_document_ids: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="本段落引用的来源文档 ID 列表（用于历史打开时实时 ACL 重鉴权）",
    )
    sensitivity_level: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="L1",
        server_default="L1",
        comment="段落关联来源最高敏感等级",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间（UTC）",
    )


class RetrievalAudit(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """单次问答检索审计主表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "retrieval_audits"
    __table_args__ = (Index("ix_retrieval_audits_org_msg", "organization_id", "message_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联消息 ID",
    )
    query_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="原始 Query 的 SHA-256 哈希（不直接落敏感原文）",
    )
    retrieval_config_version: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        default="v1",
        server_default="v1",
        comment="检索策略配置版本",
    )
    policy_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="当时生效策略版本",
    )
    latency_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="检索与鉴权总耗时（毫秒）",
    )
    authorized_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="通过 PostgreSQL ACL 复核的候选数",
    )
    denied_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="被 PostgreSQL ACL 拒绝的候选数",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="审计记录时间（UTC）",
    )


class RetrievalCandidate(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """检索候选引用表（仅存 ID 与打分元数据，严禁存未授权正文！强制 FORCE RLS）。"""

    __tablename__ = "retrieval_candidates"
    __table_args__ = (Index("ix_retrieval_candidates_org_audit", "organization_id", "audit_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("retrieval_audits.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联检索审计 ID",
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="候选 Chunk ID",
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="所属知识空间 ID",
    )
    per_space_rank: Mapped[int] = mapped_column(Integer, nullable=False, comment="空间内 RRF 排名")
    retrieval_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="hybrid_rrf",
        server_default="hybrid_rrf",
        comment="召回来源（dense / sparse / hybrid_rrf）",
    )
    score_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="脱敏后的打分与排名元数据",
    )


class RetrievalDecision(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """检索候选权威 ACL 判定明细表（仅限安全审计查阅，强制 FORCE RLS）。"""

    __tablename__ = "retrieval_decisions"
    __table_args__ = (Index("ix_retrieval_decisions_org_audit", "organization_id", "audit_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    audit_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("retrieval_audits.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联检索审计 ID",
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    decision: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="权威判定结论（ALLOW / DENY）",
    )
    reason_code: Mapped[str] = mapped_column(String(96), nullable=False, comment="判定原因码")
    acl_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="判定时 ACL 版本"
    )


class LLMUsage(UUIDPrimaryKeyMixin, TenantMixin, Base):
    """LLM 模型调用 Token 用量与延迟审计表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "llm_usages"
    __table_args__ = (Index("ix_llm_usages_org_created", "organization_id", "created_at"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联消息 ID",
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, comment="LLM Provider 标识")
    model: Mapped[str] = mapped_column(String(128), nullable=False, comment="模型名称")
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    first_token_ms: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="首字延迟（毫秒）",
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment="调用成本与外发策略元数据",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="记录时间（UTC）",
    )


class MessageFeedback(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """用户回答质量反馈表（租户表，强制 FORCE RLS）。"""

    __tablename__ = "message_feedbacks"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "message_id",
            "user_id",
            name="uq_message_feedbacks_org_msg_user",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属企业组织 ID（PostgreSQL RLS 强隔离边界键）",
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="反馈目标消息 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="反馈提交用户 ID",
    )
    rating: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="评价类型（HELPFUL / UNHELPFUL）",
    )
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="可选补充说明")
