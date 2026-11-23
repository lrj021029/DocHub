"""身份认证领域 ORM 模型（平台级表：`users`、`user_sessions`）。

安全约束：
1. `users.password_hash` 仅保存 Argon2id 哈希，严禁明文存储密码。
2. `user_sessions.refresh_token_hash` 仅保存 SHA-256 摘要，并记录 `family_id` 与
   `replaced_by_session_id` 以支持 Refresh Token Rotation 与重放攻击家族吊销。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import new_uuid7
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """全平台用户身份实体。"""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="登录邮箱（全平台唯一，规范化小写）",
    )
    display_name: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
        comment="用户显示名称",
    )
    password_hash: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Argon2id 密码哈希值",
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="ACTIVE",
        server_default="ACTIVE",
        comment="账号状态（ACTIVE / DISABLED / LOCKED）",
    )


class UserSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Refresh Token 会话与轮换家族记录（平台级表）。"""

    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_family", "user_id", "family_id"),
        Index("ix_user_sessions_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联用户 ID",
    )
    active_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        comment="该会话当前绑定的活跃组织 ID",
    )
    family_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        default=new_uuid7,
        index=True,
        comment="Refresh Token 轮换家族 ID（检测到重放攻击时整族吊销）",
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="Refresh Token 原文的 SHA-256 十六进制摘要",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="Refresh Token 绝对过期时间（UTC）",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="会话吊销或轮换失效时间（非空表示已失效）",
    )
    revoke_reason: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="失效原因（ROTATED / LOGOUT / REPLAY_DETECTED / EXPIRED / ADMIN_REVOKED）",
    )
    replaced_by_session_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("user_sessions.id", ondelete="SET NULL"),
        nullable=True,
        comment="轮换后继会话 ID（用于识别旧令牌重放攻击）",
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="客户端 User-Agent 摘要",
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="会话创建或最近刷新来源 IP",
    )
