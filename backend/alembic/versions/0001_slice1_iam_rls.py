"""Slice 1: 身份认证、多组织、部门 Closure Table、RBAC 分离与 PostgreSQL FORCE RLS。

Revision ID: 0001_slice1_iam_rls
Revises: None
Create Date: 2026-11-23 09:53:00+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# 迁移版本标识（Slice 1 首版单头迁移）
revision: str = "0001_slice1_iam_rls"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 采用纯 organization_id 强隔离策略的租户表清单
_ORG_STRICT_RLS_TABLES: tuple[str, ...] = (
    "departments",
    "department_closures",
    "department_memberships",
    "organization_roles",
    "organization_role_permissions",
    "user_role_memberships",
    "audit_events",
    "security_audit_events",
)

# 全部启用 ENABLE + FORCE ROW LEVEL SECURITY 的租户表清单
_ALL_RLS_TABLES: tuple[str, ...] = ("organization_members", *_ORG_STRICT_RLS_TABLES)


def upgrade() -> None:
    """正向迁移：创建 Slice 1 全部平台表、租户表、索引及 PostgreSQL FORCE RLS 策略。"""
    # --- 1. 平台级表 ---
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
        sa.UniqueConstraint("slug", name=op.f("uq_organizations_slug")),
    )
    op.create_index(op.f("ix_organizations_slug"), "organizations", ["slug"], unique=True)

    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("active_organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=64), nullable=True),
        sa.Column("replaced_by_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["active_organization_id"],
            ["organizations.id"],
            name=op.f("fk_user_sessions_active_organization_id_organizations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_session_id"],
            ["user_sessions.id"],
            name=op.f("fk_user_sessions_replaced_by_session_id_user_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        sa.UniqueConstraint("refresh_token_hash", name=op.f("uq_user_sessions_refresh_token_hash")),
    )
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_user_sessions_family_id"),
        "user_sessions",
        ["family_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_sessions_refresh_token_hash"),
        "user_sessions",
        ["refresh_token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_user_sessions_user_family",
        "user_sessions",
        ["user_id", "family_id"],
        unique=False,
    )
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"], unique=False)

    op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=96), nullable=False),
        sa.Column("scope", sa.String(length=32), server_default="ORGANIZATION", nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("code", name=op.f("uq_permissions_code")),
    )
    op.create_index(op.f("ix_permissions_code"), "permissions", ["code"], unique=True)

    op.create_table(
        "system_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_system_roles")),
        sa.UniqueConstraint("code", name=op.f("uq_system_roles_code")),
    )
    op.create_index(op.f("ix_system_roles_code"), "system_roles", ["code"], unique=True)

    op.create_table(
        "system_role_permissions",
        sa.Column("system_role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_system_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["system_role_id"],
            ["system_roles.id"],
            name=op.f("fk_system_role_permissions_system_role_id_system_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "system_role_id",
            "permission_id",
            name=op.f("pk_system_role_permissions"),
        ),
    )

    op.create_table(
        "user_system_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("system_role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
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
        sa.ForeignKeyConstraint(
            ["system_role_id"],
            ["system_roles.id"],
            name=op.f("fk_user_system_roles_system_role_id_system_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_system_roles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_system_roles")),
        sa.UniqueConstraint(
            "user_id",
            "system_role_id",
            name="uq_user_system_roles_user_role",
        ),
    )
    op.create_index(
        op.f("ix_user_system_roles_user_id"),
        "user_system_roles",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_system_roles_system_role_id"),
        "user_system_roles",
        ["system_role_id"],
        unique=False,
    )

    # --- 2. 租户级表（全部包含非空 organization_id 并启用 FORCE RLS） ---
    op.create_table(
        "organization_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_members_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_organization_members_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_members")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_members_org_user",
        ),
    )
    op.create_index(
        op.f("ix_organization_members_organization_id"),
        "organization_members",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_organization_members_user_id"),
        "organization_members",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "departments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
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
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_departments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["departments.id"],
            name=op.f("fk_departments_parent_id_departments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_departments")),
        sa.UniqueConstraint(
            "organization_id",
            "parent_id",
            "name",
            name="uq_departments_org_parent_name",
        ),
    )
    op.create_index(
        op.f("ix_departments_organization_id"),
        "departments",
        ["organization_id"],
        unique=False,
    )
    op.create_index(op.f("ix_departments_parent_id"), "departments", ["parent_id"], unique=False)
    op.create_index(
        "ix_departments_org_parent",
        "departments",
        ["organization_id", "parent_id"],
        unique=False,
    )

    op.create_table(
        "department_closures",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ancestor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("descendant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.CheckConstraint("depth >= 0", name=op.f("ck_department_closures_depth_non_negative")),
        sa.ForeignKeyConstraint(
            ["ancestor_id"],
            ["departments.id"],
            name=op.f("fk_department_closures_ancestor_id_departments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["descendant_id"],
            ["departments.id"],
            name=op.f("fk_department_closures_descendant_id_departments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_department_closures_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "ancestor_id",
            "descendant_id",
            name=op.f("pk_department_closures"),
        ),
    )
    op.create_index(
        op.f("ix_department_closures_organization_id"),
        "department_closures",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_department_closures_org_descendant",
        "department_closures",
        ["organization_id", "descendant_id"],
        unique=False,
    )
    op.create_index(
        "ix_department_closures_org_ancestor",
        "department_closures",
        ["organization_id", "ancestor_id"],
        unique=False,
    )

    op.create_table(
        "department_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "membership_type",
            sa.String(length=32),
            server_default="PRIMARY",
            nullable=False,
        ),
        sa.Column("is_primary", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name=op.f("ck_department_memberships_valid_range_order"),
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            name=op.f("fk_department_memberships_department_id_departments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_department_memberships_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_department_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_department_memberships")),
    )
    op.create_index(
        op.f("ix_department_memberships_organization_id"),
        "department_memberships",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_department_memberships_department_id"),
        "department_memberships",
        ["department_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_department_memberships_user_id"),
        "department_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_department_memberships_org_user",
        "department_memberships",
        ["organization_id", "user_id"],
        unique=False,
    )
    op.create_index(
        "ix_department_memberships_org_dept",
        "department_memberships",
        ["organization_id", "department_id"],
        unique=False,
    )

    op.create_table(
        "organization_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
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
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_roles_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_roles")),
        sa.UniqueConstraint(
            "organization_id",
            "code",
            name="uq_organization_roles_org_code",
        ),
    )
    op.create_index(
        op.f("ix_organization_roles_organization_id"),
        "organization_roles",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "organization_role_permissions",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_role_permissions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_role_id"],
            ["organization_roles.id"],
            name=op.f("fk_organization_role_permissions_organization_role_id_organization_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_organization_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_role_id",
            "permission_id",
            name=op.f("pk_organization_role_permissions"),
        ),
    )
    op.create_index(
        op.f("ix_organization_role_permissions_organization_id"),
        "organization_role_permissions",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_org_role_permissions_org_role",
        "organization_role_permissions",
        ["organization_id", "organization_role_id"],
        unique=False,
    )

    op.create_table(
        "user_role_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="ACTIVE", nullable=False),
        sa.Column(
            "valid_from",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name=op.f("ck_user_role_memberships_valid_range_order"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_user_role_memberships_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_role_id"],
            ["organization_roles.id"],
            name=op.f("fk_user_role_memberships_organization_role_id_organization_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_role_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_role_memberships")),
    )
    op.create_index(
        op.f("ix_user_role_memberships_organization_id"),
        "user_role_memberships",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_role_memberships_user_id"),
        "user_role_memberships",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_role_memberships_organization_role_id"),
        "user_role_memberships",
        ["organization_role_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_role_memberships_org_user",
        "user_role_memberships",
        ["organization_id", "user_id"],
        unique=False,
    )
    op.create_index(
        "ix_user_role_memberships_org_role",
        "user_role_memberships",
        ["organization_id", "organization_role_id"],
        unique=False,
    )

    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "details_json",
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
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_audit_events_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_organization_id"),
        "audit_events",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_events_actor_user_id"),
        "audit_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_org_created",
        "audit_events",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_org_action",
        "audit_events",
        ["organization_id", "action"],
        unique=False,
    )

    op.create_table(
        "security_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("severity", sa.String(length=16), server_default="MEDIUM", nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column(
            "details_json",
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
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_security_audit_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_security_audit_events_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_security_audit_events")),
    )
    op.create_index(
        op.f("ix_security_audit_events_organization_id"),
        "security_audit_events",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_security_audit_events_actor_user_id"),
        "security_audit_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_security_audit_events_org_created",
        "security_audit_events",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_security_audit_events_org_type",
        "security_audit_events",
        ["organization_id", "event_type"],
        unique=False,
    )

    # --- 3. 强制启用 PostgreSQL ENABLE + FORCE ROW LEVEL SECURITY ---
    # 3.1 organization_members：兼顾组织管理与本人列举组织，双变量为空时 Fail-Closed 返回 0 行
    op.execute("ALTER TABLE organization_members ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_members FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY rls_organization_members_isolation ON organization_members
        USING (
            organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::uuid
            OR user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        )
        WITH CHECK (
            organization_id = NULLIF(current_setting('app.current_organization_id', true), '')::uuid
        )
        """
    )

    # 3.2 其余全部租户表严格按照 organization_id Fail-Closed 隔离
    for table_name in _ORG_STRICT_RLS_TABLES:
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

    # 3.3 幂等授予运行期受限角色 app_runtime 最小 DML 权限（无 DDL、无 BYPASSRLS）
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime"
    )


def downgrade() -> None:
    """回滚迁移：按依赖逆序移除 RLS 策略与全部 Slice 1 表。"""
    op.execute("DROP POLICY IF EXISTS rls_organization_members_isolation ON organization_members")
    for table_name in _ORG_STRICT_RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS rls_{table_name}_org_isolation ON {table_name}")

    for table_name in _ALL_RLS_TABLES:
        op.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} DISABLE ROW LEVEL SECURITY")

    op.drop_table("security_audit_events")
    op.drop_table("audit_events")
    op.drop_table("user_role_memberships")
    op.drop_table("organization_role_permissions")
    op.drop_table("organization_roles")
    op.drop_table("department_memberships")
    op.drop_table("department_closures")
    op.drop_table("departments")
    op.drop_table("organization_members")
    op.drop_table("user_system_roles")
    op.drop_table("system_role_permissions")
    op.drop_table("system_roles")
    op.drop_table("permissions")
    op.drop_table("user_sessions")
    op.drop_table("organizations")
    op.drop_table("users")
