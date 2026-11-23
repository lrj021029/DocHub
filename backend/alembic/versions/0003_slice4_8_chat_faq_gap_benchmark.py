"""Slice 4-8: Chat/RAG 审计、Citation、FAQ 三轴生命周期、Mining/Gap 与治理评测 FORCE RLS 迁移。

Revision ID: 0003_slice4_8_chat_faq_gap
Revises: 0002_slice2_3_space_acl_docs
Create Date: 2026-11-23 14:33:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_slice4_8_chat_faq_gap"
down_revision: str | Sequence[str] | None = "0002_slice2_3_space_acl_docs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本次新增的 27 张业务租户表（均强制开启 ENABLE + FORCE ROW LEVEL SECURITY）
_SLICE4_8_RLS_TABLES: tuple[str, ...] = (
    "faqs",
    "faq_versions",
    "faq_segments",
    "faq_source_refs",
    "faq_acls",
    "faq_reviews",
    "faq_publish_records",
    "faq_cache_indexes",
    "conversations",
    "conversation_spaces",
    "messages",
    "message_segments",
    "answer_citations",
    "retrieval_audits",
    "retrieval_candidates",
    "retrieval_decisions",
    "llm_usages",
    "message_feedbacks",
    "question_events",
    "question_clusters",
    "question_cluster_members",
    "faq_candidates",
    "knowledge_gaps",
    "knowledge_gap_tasks",
    "organization_provider_policies",
    "benchmark_runs",
    "benchmark_case_results",
)


def _ts_cols() -> tuple[sa.Column[object], sa.Column[object]]:
    """生成标准 UTC created_at 与 updated_at 列。"""
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def upgrade() -> None:
    """正向迁移：创建 Slice 4-8 全部 27 张租户表并启用 PostgreSQL FORCE RLS。"""
    # --- 1. FAQ（先建表以便 messages.faq_id 外键引用） ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "faqs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "lifecycle_state", sa.String(length=32), server_default="CANDIDATE", nullable=False
        ),
        sa.Column(
            "publication_state",
            sa.String(length=32),
            server_default="UNPUBLISHED",
            nullable=False,
        ),
        sa.Column(
            "source_health_state",
            sa.String(length=32),
            server_default="HEALTHY",
            nullable=False,
        ),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_manual", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("maintainer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["maintainer_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faqs")),
    )
    op.create_index(op.f("ix_faqs_organization_id"), "faqs", ["organization_id"])
    op.create_index(
        "ix_faqs_org_space_pub",
        "faqs",
        ["organization_id", "space_id", "publication_state"],
    )
    op.create_index("ix_faqs_org_health", "faqs", ["organization_id", "source_health_state"])

    op.create_table(
        "faq_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer_hash", sa.String(length=64), nullable=False),
        sa.Column("fact_basis", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_versions")),
        sa.UniqueConstraint(
            "organization_id", "faq_id", "version_no", name="uq_faq_versions_faq_ver"
        ),
    )
    op.create_index(op.f("ix_faq_versions_organization_id"), "faq_versions", ["organization_id"])
    op.create_index(op.f("ix_faq_versions_faq_id"), "faq_versions", ["faq_id"])

    op.create_table(
        "faq_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_no", sa.Integer(), server_default="0", nullable=False),
        sa.Column("content_type", sa.String(length=32), server_default="MARKDOWN", nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_version_id"], ["faq_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_segments")),
    )
    op.create_index(op.f("ix_faq_segments_organization_id"), "faq_segments", ["organization_id"])
    op.create_index(
        "ix_faq_segments_org_ver_no",
        "faq_segments",
        ["organization_id", "faq_version_id", "segment_no"],
    )

    op.create_table(
        "faq_source_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("citation_ref", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_segment_id"], ["faq_segments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_source_refs")),
    )
    op.create_index(
        op.f("ix_faq_source_refs_organization_id"),
        "faq_source_refs",
        ["organization_id"],
    )
    op.create_index(
        "ix_faq_source_refs_org_seg",
        "faq_source_refs",
        ["organization_id", "faq_segment_id"],
    )
    op.create_index(
        "ix_faq_source_refs_org_doc",
        "faq_source_refs",
        ["organization_id", "document_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "faq_acls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "policy_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_acls")),
        sa.UniqueConstraint("organization_id", "faq_id", name="uq_faq_acls_org_faq"),
    )
    op.create_index(op.f("ix_faq_acls_organization_id"), "faq_acls", ["organization_id"])

    op.create_table(
        "faq_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column(
            "review_type",
            sa.String(length=32),
            server_default="CONTENT_AND_ACL",
            nullable=False,
        ),
        sa.Column("comments", sa.String(length=1000), nullable=True),
        sa.Column("policy_version", sa.String(length=32), server_default="v1", nullable=False),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_version_id"], ["faq_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_reviews")),
    )
    op.create_index(op.f("ix_faq_reviews_organization_id"), "faq_reviews", ["organization_id"])
    op.create_index("ix_faq_reviews_org_faq", "faq_reviews", ["organization_id", "faq_id"])

    op.create_table(
        "faq_publish_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="PUBLISHED", nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_version_id"], ["faq_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_publish_records")),
    )
    op.create_index(
        op.f("ix_faq_publish_records_organization_id"),
        "faq_publish_records",
        ["organization_id"],
    )
    op.create_index(
        "ix_faq_publish_records_org_faq",
        "faq_publish_records",
        ["organization_id", "faq_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "faq_cache_indexes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("faq_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acl_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("source_version_hash", sa.String(length=64), nullable=False),
        sa.Column("cache_generation", sa.Integer(), server_default="1", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_version_id"], ["faq_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_cache_indexes")),
        sa.UniqueConstraint("organization_id", "faq_id", name="uq_faq_cache_indexes_org_faq"),
    )
    op.create_index(
        op.f("ix_faq_cache_indexes_organization_id"),
        "faq_cache_indexes",
        ["organization_id"],
    )

    # --- 2. Chat, Citations & Retrieval Audit ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), server_default="新对话", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(op.f("ix_conversations_organization_id"), "conversations", ["organization_id"])
    op.create_index("ix_conversations_org_user", "conversations", ["organization_id", "user_id"])

    op.create_table(
        "conversation_spaces",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id", "space_id", name=op.f("pk_conversation_spaces")),
    )
    op.create_index(
        op.f("ix_conversation_spaces_organization_id"),
        "conversation_spaces",
        ["organization_id"],
    )
    op.create_index(
        "ix_conversation_spaces_org_conv",
        "conversation_spaces",
        ["organization_id", "conversation_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="COMPLETED", nullable=False),
        sa.Column("answer_policy_used", sa.String(length=48), nullable=True),
        sa.Column("faq_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_splittable", sa.Boolean(), server_default="true", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(op.f("ix_messages_organization_id"), "messages", ["organization_id"])
    op.create_index(
        "ix_messages_org_conv_created",
        "messages",
        ["organization_id", "conversation_id", "created_at"],
    )

    op.create_table(
        "message_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("segment_type", sa.String(length=32), server_default="EVIDENCE", nullable=False),
        sa.Column(
            "source_document_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("sensitivity_level", sa.String(length=8), server_default="L1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_segments")),
    )
    op.create_index(
        op.f("ix_message_segments_organization_id"),
        "message_segments",
        ["organization_id"],
    )
    op.create_index(
        "ix_message_segments_org_msg_seq",
        "message_segments",
        ["organization_id", "message_id", "sequence"],
    )

    op.create_table(
        "answer_citations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("block_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("bbox_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        sa.Column("acl_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["message_segment_id"], ["message_segments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["block_id"], ["parsed_blocks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_answer_citations")),
    )
    op.create_index(
        op.f("ix_answer_citations_organization_id"),
        "answer_citations",
        ["organization_id"],
    )
    op.create_index(
        "ix_answer_citations_org_msg", "answer_citations", ["organization_id", "message_id"]
    )
    op.create_index(
        "ix_answer_citations_org_seg",
        "answer_citations",
        ["organization_id", "message_segment_id"],
    )

    op.create_table(
        "retrieval_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "retrieval_config_version",
            sa.String(length=48),
            server_default="v1",
            nullable=False,
        ),
        sa.Column("policy_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("authorized_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("denied_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_audits")),
    )
    op.create_index(
        op.f("ix_retrieval_audits_organization_id"),
        "retrieval_audits",
        ["organization_id"],
    )
    op.create_index(
        "ix_retrieval_audits_org_msg", "retrieval_audits", ["organization_id", "message_id"]
    )

    op.create_table(
        "retrieval_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("per_space_rank", sa.Integer(), nullable=False),
        sa.Column(
            "retrieval_source",
            sa.String(length=32),
            server_default="hybrid_rrf",
            nullable=False,
        ),
        sa.Column(
            "score_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["audit_id"], ["retrieval_audits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_candidates")),
    )
    op.create_index(
        op.f("ix_retrieval_candidates_organization_id"),
        "retrieval_candidates",
        ["organization_id"],
    )
    op.create_index(
        "ix_retrieval_candidates_org_audit",
        "retrieval_candidates",
        ["organization_id", "audit_id"],
    )

    op.create_table(
        "retrieval_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("acl_revision", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["audit_id"], ["retrieval_audits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_retrieval_decisions")),
    )
    op.create_index(
        op.f("ix_retrieval_decisions_organization_id"),
        "retrieval_decisions",
        ["organization_id"],
    )
    op.create_index(
        "ix_retrieval_decisions_org_audit",
        "retrieval_decisions",
        ["organization_id", "audit_id"],
    )

    op.create_table(
        "llm_usages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_token_ms", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "cost_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_usages")),
    )
    op.create_index(op.f("ix_llm_usages_organization_id"), "llm_usages", ["organization_id"])
    op.create_index("ix_llm_usages_org_created", "llm_usages", ["organization_id", "created_at"])

    c_at, u_at = _ts_cols()
    op.create_table(
        "message_feedbacks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rating", sa.String(length=24), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_feedbacks")),
        sa.UniqueConstraint(
            "organization_id",
            "message_id",
            "user_id",
            name="uq_message_feedbacks_org_msg_user",
        ),
    )
    op.create_index(
        op.f("ix_message_feedbacks_organization_id"),
        "message_feedbacks",
        ["organization_id"],
    )

    # --- 3. FAQ Mining & Knowledge Gap ---
    op.create_table(
        "question_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_hash", sa.String(length=64), nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("sensitivity_level", sa.String(length=8), server_default="L1", nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_events")),
    )
    op.create_index(
        op.f("ix_question_events_organization_id"), "question_events", ["organization_id"]
    )
    op.create_index(
        "ix_question_events_org_space_created",
        "question_events",
        ["organization_id", "space_id", "created_at"],
    )
    op.create_index(
        "ix_question_events_org_outcome",
        "question_events",
        ["organization_id", "outcome"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "question_clusters",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_key", sa.String(length=128), nullable=False),
        sa.Column(
            "algorithm_version",
            sa.String(length=64),
            server_default="sklearn-hdbscan-v1",
            nullable=False,
        ),
        sa.Column("cluster_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("representative_question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_question_clusters")),
    )
    op.create_index(
        op.f("ix_question_clusters_organization_id"),
        "question_clusters",
        ["organization_id"],
    )
    op.create_index(
        "ix_question_clusters_org_space",
        "question_clusters",
        ["organization_id", "space_id"],
    )

    op.create_table(
        "question_cluster_members",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("membership_score", sa.Float(), server_default="1.0", nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["question_clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["question_event_id"], ["question_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "cluster_id",
            "question_event_id",
            name=op.f("pk_question_cluster_members"),
        ),
    )
    op.create_index(
        op.f("ix_question_cluster_members_organization_id"),
        "question_cluster_members",
        ["organization_id"],
    )
    op.create_index(
        "ix_question_cluster_members_org_cluster",
        "question_cluster_members",
        ["organization_id", "cluster_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "faq_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("faq_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "candidate_status",
            sa.String(length=32),
            server_default="PENDING_SUBMIT",
            nullable=False,
        ),
        sa.Column("generated_question", sa.Text(), nullable=False),
        sa.Column(
            "generated_answer_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "confidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["question_clusters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["faq_id"], ["faqs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_faq_candidates")),
    )
    op.create_index(
        op.f("ix_faq_candidates_organization_id"), "faq_candidates", ["organization_id"]
    )
    op.create_index(
        "ix_faq_candidates_org_status",
        "faq_candidates",
        ["organization_id", "candidate_status"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "knowledge_gaps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="DETECTED", nullable=False),
        sa.Column("priority", sa.String(length=16), server_default="MEDIUM", nullable=False),
        sa.Column("question_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("unique_user_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cluster_id"], ["question_clusters.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_gaps")),
    )
    op.create_index(
        op.f("ix_knowledge_gaps_organization_id"), "knowledge_gaps", ["organization_id"]
    )
    op.create_index("ix_knowledge_gaps_org_status", "knowledge_gaps", ["organization_id", "status"])
    op.create_index(
        "ix_knowledge_gaps_org_space", "knowledge_gaps", ["organization_id", "space_id"]
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "knowledge_gap_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gap_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="ASSIGNED", nullable=False),
        sa.Column("priority", sa.String(length=16), server_default="MEDIUM", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("related_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("related_faq_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "resolution_evidence_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["gap_id"], ["knowledge_gaps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["related_document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["related_faq_id"], ["faqs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_gap_tasks")),
    )
    op.create_index(
        op.f("ix_knowledge_gap_tasks_organization_id"),
        "knowledge_gap_tasks",
        ["organization_id"],
    )
    op.create_index(
        "ix_knowledge_gap_tasks_org_gap",
        "knowledge_gap_tasks",
        ["organization_id", "gap_id"],
    )

    # --- 4. 治理、Provider 外发策略与 Benchmark ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "organization_provider_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("allow_l1_external_egress", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "embedding_primary_provider",
            sa.String(length=32),
            server_default="qwen",
            nullable=False,
        ),
        sa.Column(
            "reranker_primary_provider",
            sa.String(length=32),
            server_default="qwen",
            nullable=False,
        ),
        sa.Column("local_bge_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("local_ocr_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("llm_base_url_override", sa.String(length=255), nullable=True),
        sa.Column("llm_model_override", sa.String(length=128), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_provider_policies")),
        sa.UniqueConstraint("organization_id", name="uq_org_provider_policies_org"),
    )
    op.create_index(
        op.f("ix_organization_provider_policies_organization_id"),
        "organization_provider_policies",
        ["organization_id"],
    )

    op.create_table(
        "benchmark_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_type", sa.String(length=48), server_default="RAG", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="COMPLETED", nullable=False),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("acl_violation_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("triggered_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["triggered_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_runs")),
    )
    op.create_index(
        op.f("ix_benchmark_runs_organization_id"), "benchmark_runs", ["organization_id"]
    )
    op.create_index(
        "ix_benchmark_runs_org_created",
        "benchmark_runs",
        ["organization_id", "created_at"],
    )

    op.create_table(
        "benchmark_case_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_name", sa.String(length=160), nullable=False),
        sa.Column("passed", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "detail_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["benchmark_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_benchmark_case_results")),
    )
    op.create_index(
        op.f("ix_benchmark_case_results_organization_id"),
        "benchmark_case_results",
        ["organization_id"],
    )
    op.create_index(
        "ix_benchmark_case_results_org_run",
        "benchmark_case_results",
        ["organization_id", "run_id"],
    )

    # --- 5. 为全部 27 张新增租户表强制开启 ENABLE + FORCE ROW LEVEL SECURITY ---
    for table_name in _SLICE4_8_RLS_TABLES:
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY rls_{table_name}_org_isolation ON {table_name}
            USING (
                organization_id = NULLIF(
                    current_setting('app.current_organization_id', true), ''
                )::uuid
            )
            WITH CHECK (
                organization_id = NULLIF(
                    current_setting('app.current_organization_id', true), ''
                )::uuid
            )
            """
        )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime")


def downgrade() -> None:
    """回滚迁移：按依赖逆序移除 RLS 策略与全部 27 张 Slice 4-8 表。"""
    for table_name in reversed(_SLICE4_8_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS rls_{table_name}_org_isolation ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")
        op.drop_table(table_name)
