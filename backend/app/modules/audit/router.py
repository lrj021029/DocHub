"""双审计中心 HTTP 路由：业务审计（`audit:read`）与安全审计（`security_audit:read`）物理分离。"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.db.session import get_db_session, org_rls_session
from app.modules.audit.models import AuditEvent, SecurityAuditEvent
from app.modules.identity.deps import OrgContext, require_permission
from app.modules.identity.models import User

router = APIRouter(prefix="/audit", tags=["Audit"])


class AuditEventItem(BaseModel):
    """统一审计流展示项（脱敏，不含受限文档正文或标题）。"""

    id: uuid.UUID
    event_type: str
    actor_email: str
    resource_summary: str
    severity: str | None = None
    reason: str | None = None
    request_id: str | None = None
    created_at: datetime


class AuditCursorPage(BaseModel):
    """Cursor 分页审计流响应。"""

    items: list[AuditEventItem] = Field(default_factory=list)
    next_cursor: str | None = None


def _build_cursor_condition(
    cursor: str | None,
    *,
    created_col: object,
    id_col: object,
) -> object | None:
    """解码游标并构造倒序分页过滤条件。"""
    if not cursor:
        return None
    payload = decode_cursor(cursor)
    raw_ts = payload.get("created_at")
    raw_id = payload.get("id")
    if not isinstance(raw_ts, str) or not isinstance(raw_id, str):
        return None
    cursor_ts = datetime.fromisoformat(raw_ts)
    cursor_id = uuid.UUID(raw_id)
    return or_(
        created_col < cursor_ts,  # type: ignore[operator]
        and_(created_col == cursor_ts, id_col < cursor_id),  # type: ignore[operator]
    )


@router.get(
    "/events",
    response_model=AuditCursorPage,
    summary="查询业务操作审计事件流（Cursor 分页）",
)
async def list_business_audit_events(
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
    org_ctx: OrgContext = Depends(require_permission("audit:read")),
    session: AsyncSession = Depends(get_db_session),
) -> AuditCursorPage:
    """在 FORCE RLS 下分页读取当前组织业务审计事件。"""
    cursor_cond = _build_cursor_condition(
        cursor,
        created_col=AuditEvent.created_at,
        id_col=AuditEvent.id,
    )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        stmt = (
            select(AuditEvent, User.email)
            .outerjoin(User, User.id == AuditEvent.actor_user_id)
            .where(AuditEvent.organization_id == org_ctx.organization_id)
        )
        if cursor_cond is not None:
            stmt = stmt.where(cursor_cond)  # type: ignore[arg-type]

        rows = (
            await session.execute(
                stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).limit(limit + 1)
            )
        ).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        AuditEventItem(
            id=ev.id,
            event_type=ev.action,
            actor_email=actor_email or "system",
            resource_summary=f"{ev.resource_type}:{ev.resource_id or '-'}",
            severity=None,
            reason=str(ev.details_json.get("reason")) if ev.details_json.get("reason") else None,
            request_id=ev.request_id,
            created_at=ev.created_at,
        )
        for ev, actor_email in page_rows
    ]

    next_cursor: str | None = None
    if has_more and page_rows:
        last_ev = page_rows[-1][0]
        next_cursor = encode_cursor(
            {"created_at": last_ev.created_at.isoformat(), "id": str(last_ev.id)}
        )

    return AuditCursorPage(items=items, next_cursor=next_cursor)


@router.get(
    "/security-events",
    response_model=AuditCursorPage,
    summary="查询高敏感安全审计事件流（仅安全审计员可见，Cursor 分页）",
)
async def list_security_audit_events(
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
    org_ctx: OrgContext = Depends(require_permission("security_audit:read")),
    session: AsyncSession = Depends(get_db_session),
) -> AuditCursorPage:
    """在 FORCE RLS 与 security_audit:read 强保护下读取安全取证事件。"""
    cursor_cond = _build_cursor_condition(
        cursor,
        created_col=SecurityAuditEvent.created_at,
        id_col=SecurityAuditEvent.id,
    )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        stmt = (
            select(SecurityAuditEvent, User.email)
            .outerjoin(User, User.id == SecurityAuditEvent.actor_user_id)
            .where(SecurityAuditEvent.organization_id == org_ctx.organization_id)
        )
        if cursor_cond is not None:
            stmt = stmt.where(cursor_cond)  # type: ignore[arg-type]

        rows = (
            await session.execute(
                stmt.order_by(
                    SecurityAuditEvent.created_at.desc(),
                    SecurityAuditEvent.id.desc(),
                ).limit(limit + 1)
            )
        ).all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]
    items = [
        AuditEventItem(
            id=ev.id,
            event_type=ev.event_type,
            actor_email=actor_email or "system",
            resource_summary=f"{ev.resource_type or 'security'}:{ev.resource_id or '-'}",
            severity=ev.severity,
            reason=str(ev.details_json.get("reason")) if ev.details_json.get("reason") else None,
            request_id=ev.request_id,
            created_at=ev.created_at,
        )
        for ev, actor_email in page_rows
    ]

    next_cursor: str | None = None
    if has_more and page_rows:
        last_ev = page_rows[-1][0]
        next_cursor = encode_cursor(
            {"created_at": last_ev.created_at.isoformat(), "id": str(last_ev.id)}
        )

    return AuditCursorPage(items=items, next_cursor=next_cursor)
