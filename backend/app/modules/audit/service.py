"""审计写入服务：统一对业务审计与安全审计载荷执行脱敏并写入当前 RLS 租户事务。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import redact_sensitive_data, request_id_ctx
from app.modules.audit.models import AuditEvent, SecurityAuditEvent


def record_audit_event(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    """在当前已绑定 org_rls_session 的事务内追加业务审计记录。"""
    sanitized_details = redact_sensitive_data(details or {})
    event = AuditEvent(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id)[:255] if resource_id is not None else None,
        request_id=request_id_ctx.get(),
        details_json=sanitized_details if isinstance(sanitized_details, dict) else {},
    )
    session.add(event)
    return event


def record_security_audit_event(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    event_type: str,
    severity: str = "MEDIUM",
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    ip_address: str | None = None,
    details: dict[str, Any] | None = None,
) -> SecurityAuditEvent:
    """在当前已绑定 org_rls_session 的事务内追加高敏感安全审计记录。"""
    sanitized_details = redact_sensitive_data(details or {})
    event = SecurityAuditEvent(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        severity=severity,
        resource_type=resource_type,
        resource_id=str(resource_id)[:255] if resource_id is not None else None,
        request_id=request_id_ctx.get(),
        ip_address=ip_address,
        details_json=sanitized_details if isinstance(sanitized_details, dict) else {},
    )
    session.add(event)
    return event
