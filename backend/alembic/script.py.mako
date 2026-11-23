"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
# 迁移版本标识，由 Alembic 自动维护
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """正向迁移：应用结构变更与 RLS 策略。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """回滚迁移：安全撤销本次结构变更。"""
    ${downgrades if downgrades else "pass"}
