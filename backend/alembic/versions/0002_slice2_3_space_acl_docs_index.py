"""Slice 2 & 3: Knowledge Space、四维 ACL、文档导入与解析切片、索引治理、Outbox 与 FORCE RLS。

Revision ID: 0002_slice2_3_space_acl_docs_index
Revises: 0001_slice1_iam_rls
Create Date: 2026-11-23 10:12:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_slice2_3_space_acl_docs"
down_revision: str | Sequence[str] | None = "0001_slice1_iam_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 本次迁移新增的全部 25 张租户表（均强制开启 ENABLE + FORCE ROW LEVEL SECURITY）
_SLICE2_3_RLS_TABLES: tuple[str, ...] = (
    "embedding_providers",
    "embedding_profiles",
    "knowledge_spaces",
    "space_memberships",
    "space_policies",
    "documents",
    "document_versions",
    "document_acls",
    "acl_grants",
    "acl_grant_department_snapshots",
    "acl_revisions",
    "break_glass_grants",
    "import_jobs",
    "parse_jobs",
    "parsed_artifacts",
    "parsed_blocks",
    "chunk_sets",
    "chunks",
    "index_generations",
    "active_index_pointers",
    "index_jobs",
    "outbox_events",
    "processed_messages",
    "idempotency_records",
    "async_jobs",
)


def _uuid_col(*, nullable: bool = False) -> sa.Column[object]:
    """便捷生成 PostgreSQL UUID 列定义。"""
    return sa.Column(postgresql.UUID(as_uuid=True), nullable=nullable)


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
    """正向迁移：创建 Slice 2 & 3 全部 25 张租户表并启用 PostgreSQL FORCE RLS。"""
    # --- 1. Embedding Provider 与 Profile ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "embedding_providers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("base_url_ref", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_providers")),
        sa.UniqueConstraint("organization_id", "code", name="uq_embedding_providers_org_code"),
    )
    op.create_index(
        op.f("ix_embedding_providers_organization_id"),
        "embedding_providers",
        ["organization_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "embedding_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=64), server_default="v1", nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column(
            "language_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["embedding_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_profiles")),
        sa.UniqueConstraint("organization_id", "code", name="uq_embedding_profiles_org_code"),
    )
    op.create_index(
        op.f("ix_embedding_profiles_organization_id"),
        "embedding_profiles",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_embedding_profiles_provider_id"),
        "embedding_profiles",
        ["provider_id"],
    )

    # --- 2. Knowledge Space、成员与策略 ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "knowledge_spaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("visibility", sa.String(length=24), server_default="PRIVATE", nullable=False),
        sa.Column(
            "answer_policy",
            sa.String(length=48),
            server_default="STRICT_EVIDENCE",
            nullable=False,
        ),
        sa.Column("default_embedding_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        sa.Column(
            "config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("config_version", sa.Integer(), server_default="1", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["default_embedding_profile_id"],
            ["embedding_profiles.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_spaces")),
        sa.UniqueConstraint("organization_id", "slug", name="uq_knowledge_spaces_org_slug"),
    )
    op.create_index(
        op.f("ix_knowledge_spaces_organization_id"),
        "knowledge_spaces",
        ["organization_id"],
    )
    op.create_index(
        "ix_knowledge_spaces_org_status",
        "knowledge_spaces",
        ["organization_id", "status"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "space_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), server_default="MEMBER", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        c_at,
        u_at,
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name=op.f("ck_space_memberships_valid_range_order"),
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_space_memberships")),
        sa.UniqueConstraint(
            "organization_id",
            "space_id",
            "user_id",
            name="uq_space_memberships_org_space_user",
        ),
    )
    op.create_index(
        op.f("ix_space_memberships_organization_id"),
        "space_memberships",
        ["organization_id"],
    )
    op.create_index(
        "ix_space_memberships_org_space",
        "space_memberships",
        ["organization_id", "space_id"],
    )
    op.create_index(
        "ix_space_memberships_org_user",
        "space_memberships",
        ["organization_id", "user_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "space_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "policy_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_space_policies")),
        sa.UniqueConstraint("organization_id", "space_id", name="uq_space_policies_org_space"),
    )
    op.create_index(
        op.f("ix_space_policies_organization_id"), "space_policies", ["organization_id"]
    )

    # --- 3. 文档与不可变版本 ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=32), server_default="UPLOAD", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="QUARANTINED", nullable=False),
        sa.Column("current_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sensitivity_level", sa.String(length=8), server_default="L1", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["knowledge_spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_organization_id"), "documents", ["organization_id"])
    op.create_index(
        "ix_documents_org_space_status",
        "documents",
        ["organization_id", "space_id", "status"],
    )
    op.create_index(
        "ix_documents_org_sensitivity",
        "documents",
        ["organization_id", "sensitivity_level"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("source_object_key", sa.String(length=512), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "organization_id",
            "document_id",
            "version_no",
            name="uq_document_versions_doc_ver",
        ),
    )
    op.create_index(
        op.f("ix_document_versions_organization_id"),
        "document_versions",
        ["organization_id"],
    )
    op.create_index(
        "ix_document_versions_org_doc",
        "document_versions",
        ["organization_id", "document_id"],
    )

    # --- 4. 四维 Document ACL 与 Break-glass ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "document_acls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="ACTIVE", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_acls")),
        sa.UniqueConstraint("organization_id", "document_id", name="uq_document_acls_org_doc"),
    )
    op.create_index(op.f("ix_document_acls_organization_id"), "document_acls", ["organization_id"])
    op.create_index(op.f("ix_document_acls_document_id"), "document_acls", ["document_id"])

    c_at, u_at = _ts_cols()
    op.create_table(
        "acl_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acl_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(length=24), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("include_descendants", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "inheritance_mode", sa.String(length=24), server_default="DYNAMIC", nullable=False
        ),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        c_at,
        u_at,
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name=op.f("ck_acl_grants_valid_range_order"),
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acl_id"], ["document_acls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_acl_grants")),
    )
    op.create_index(op.f("ix_acl_grants_organization_id"), "acl_grants", ["organization_id"])
    op.create_index("ix_acl_grants_org_acl", "acl_grants", ["organization_id", "acl_id"])
    op.create_index(
        "ix_acl_grants_org_subject",
        "acl_grants",
        ["organization_id", "subject_type", "subject_id"],
    )

    op.create_table(
        "acl_grant_department_snapshots",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acl_grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acl_grant_id"], ["acl_grants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "acl_grant_id",
            "department_id",
            name=op.f("pk_acl_grant_department_snapshots"),
        ),
    )
    op.create_index(
        op.f("ix_acl_grant_department_snapshots_organization_id"),
        "acl_grant_department_snapshots",
        ["organization_id"],
    )
    op.create_index(
        "ix_acl_grant_dept_snap_org_grant",
        "acl_grant_department_snapshots",
        ["organization_id", "acl_grant_id"],
    )
    op.create_index(
        "ix_acl_grant_dept_snap_org_dept",
        "acl_grant_department_snapshots",
        ["organization_id", "department_id"],
    )

    op.create_table(
        "acl_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("acl_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "diff_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acl_id"], ["document_acls.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_acl_revisions")),
        sa.UniqueConstraint(
            "organization_id", "acl_id", "revision", name="uq_acl_revisions_acl_rev"
        ),
    )
    op.create_index(op.f("ix_acl_revisions_organization_id"), "acl_revisions", ["organization_id"])
    op.create_index("ix_acl_revisions_org_acl", "acl_revisions", ["organization_id", "acl_id"])

    c_at, u_at = _ts_cols()
    op.create_table(
        "break_glass_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="PENDING", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_break_glass_grants")),
    )
    op.create_index(
        op.f("ix_break_glass_grants_organization_id"),
        "break_glass_grants",
        ["organization_id"],
    )
    op.create_index(
        "ix_break_glass_org_user_status",
        "break_glass_grants",
        ["organization_id", "user_id", "status"],
    )

    # --- 5. 文档分阶段处理流水线、解析产物与 Parent-Child Chunk ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="CREATED", nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_jobs")),
    )
    op.create_index(op.f("ix_import_jobs_organization_id"), "import_jobs", ["organization_id"])
    op.create_index(
        "ix_import_jobs_org_version",
        "import_jobs",
        ["organization_id", "document_version_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "parse_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parser_provider", sa.String(length=64), server_default="auto", nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=True),
        sa.Column("external_task_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="QUEUED", nullable=False),
        sa.Column("tier", sa.String(length=32), nullable=True),
        sa.Column("error_detail", sa.String(length=512), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parse_jobs")),
    )
    op.create_index(op.f("ix_parse_jobs_organization_id"), "parse_jobs", ["organization_id"])
    op.create_index(
        "ix_parse_jobs_org_version", "parse_jobs", ["organization_id", "document_version_id"]
    )
    op.create_index("ix_parse_jobs_org_status", "parse_jobs", ["organization_id", "status"])
    op.create_index(op.f("ix_parse_jobs_external_task_id"), "parse_jobs", ["external_task_id"])

    c_at, u_at = _ts_cols()
    op.create_table(
        "parsed_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", sa.String(length=48), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parsed_artifacts")),
    )
    op.create_index(
        op.f("ix_parsed_artifacts_organization_id"),
        "parsed_artifacts",
        ["organization_id"],
    )
    op.create_index(
        "ix_parsed_artifacts_org_version",
        "parsed_artifacts",
        ["organization_id", "document_version_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "parsed_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("block_type", sa.String(length=32), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column(
            "heading_path",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("bounding_box", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_parsed_blocks")),
    )
    op.create_index(op.f("ix_parsed_blocks_organization_id"), "parsed_blocks", ["organization_id"])
    op.create_index(
        "ix_parsed_blocks_org_version_seq",
        "parsed_blocks",
        ["organization_id", "document_version_id", "sequence"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "chunk_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunker_version", sa.String(length=48), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="QUEUED", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunk_sets")),
    )
    op.create_index(op.f("ix_chunk_sets_organization_id"), "chunk_sets", ["organization_id"])
    op.create_index(
        "ix_chunk_sets_org_version", "chunk_sets", ["organization_id", "document_version_id"]
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "source_block_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "page_range",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_set_id"], ["chunk_sets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["parent_chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
    )
    op.create_index(op.f("ix_chunks_organization_id"), "chunks", ["organization_id"])
    op.create_index("ix_chunks_org_doc_ver", "chunks", ["organization_id", "document_version_id"])
    op.create_index("ix_chunks_org_chunk_set", "chunks", ["organization_id", "chunk_set_id"])
    op.create_index(op.f("ix_chunks_parent_chunk_id"), "chunks", ["parent_chunk_id"])

    # --- 6. 蓝绿索引代次、激活指针与索引任务 ---
    c_at, u_at = _ts_cols()
    op.create_table(
        "index_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("collection_name", sa.String(length=128), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.String(length=48), server_default="v1", nullable=False),
        sa.Column("status", sa.String(length=24), server_default="CREATED", nullable=False),
        sa.Column(
            "metrics_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["embedding_profile_id"],
            ["embedding_profiles.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_generations")),
        sa.UniqueConstraint(
            "organization_id",
            "embedding_profile_id",
            "generation_no",
            name="uq_index_generations_profile_gen",
        ),
    )
    op.create_index(
        op.f("ix_index_generations_organization_id"),
        "index_generations",
        ["organization_id"],
    )
    op.create_index(
        "ix_index_generations_org_status",
        "index_generations",
        ["organization_id", "status"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "active_index_pointers",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "scope_type",
            sa.String(length=32),
            server_default="KNOWLEDGE_SPACE",
            nullable=False,
        ),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embedding_profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("index_generation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["embedding_profile_id"],
            ["embedding_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["index_generation_id"],
            ["index_generations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_active_index_pointers")),
        sa.UniqueConstraint(
            "organization_id",
            "scope_type",
            "scope_id",
            name="uq_active_index_pointers_scope",
        ),
    )
    op.create_index(
        op.f("ix_active_index_pointers_organization_id"),
        "active_index_pointers",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_active_index_pointers_scope_id"),
        "active_index_pointers",
        ["scope_id"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "index_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="CREATED", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["generation_id"], ["index_generations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_index_jobs")),
    )
    op.create_index(op.f("ix_index_jobs_organization_id"), "index_jobs", ["organization_id"])
    op.create_index("ix_index_jobs_org_gen", "index_jobs", ["organization_id", "generation_id"])

    # --- 7. 事务 Outbox、幂等收件箱与统一异步任务 ---
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("payload_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "payload_json",
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
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
    )
    op.create_index(op.f("ix_outbox_events_organization_id"), "outbox_events", ["organization_id"])
    op.create_index(
        "ix_outbox_events_org_published", "outbox_events", ["organization_id", "published_at"]
    )
    op.create_index("ix_outbox_events_org_type", "outbox_events", ["organization_id", "event_type"])

    op.create_table(
        "processed_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("consumer_name", sa.String(length=96), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processed_messages")),
        sa.UniqueConstraint(
            "organization_id",
            "consumer_name",
            "event_id",
            name="uq_processed_messages_consumer_event",
        ),
    )
    op.create_index(
        op.f("ix_processed_messages_organization_id"),
        "processed_messages",
        ["organization_id"],
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column(
            "response_body_json",
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
        sa.ForeignKeyConstraint(["principal_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        sa.UniqueConstraint(
            "organization_id",
            "fingerprint",
            name="uq_idempotency_records_org_fp",
        ),
    )
    op.create_index(
        op.f("ix_idempotency_records_organization_id"),
        "idempotency_records",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_idempotency_records_fingerprint"),
        "idempotency_records",
        ["fingerprint"],
    )

    c_at, u_at = _ts_cols()
    op.create_table(
        "async_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="QUEUED", nullable=False),
        sa.Column("progress", sa.Integer(), server_default="0", nullable=False),
        sa.Column("celery_task_id", sa.String(length=128), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        c_at,
        u_at,
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_async_jobs")),
    )
    op.create_index(op.f("ix_async_jobs_organization_id"), "async_jobs", ["organization_id"])
    op.create_index(
        "ix_async_jobs_org_type_status",
        "async_jobs",
        ["organization_id", "job_type", "status"],
    )

    # --- 8. 为全部 25 张新增租户表强制开启 ENABLE + FORCE ROW LEVEL SECURITY ---
    for table_name in _SLICE2_3_RLS_TABLES:
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
    """回滚迁移：按依赖逆序移除 RLS 策略与全部 25 张 Slice 2 & 3 表。"""
    for table_name in reversed(_SLICE2_3_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS rls_{table_name}_org_isolation ON {table_name}")
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")
        op.drop_table(table_name)
