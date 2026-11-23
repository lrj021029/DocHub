"""事务性 Outbox 写入与 API 幂等键持久化辅助服务。

设计要点：
1. `enqueue_outbox_event` 必须在业务写操作所在的同一个 `org_rls_session` 事务内调用，
   确保领域事实与 OutboxEvent 原子提交，杜绝双写不一致。
2. `check_idempotency` / `save_idempotency_record` 基于 `principal + organization + operation`
   强作用域指纹防重放与同键异参冲突（返回 409）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.core.idempotency import (
    IdempotencyScope,
    build_idempotency_fingerprint,
    compute_request_payload_hash,
)
from app.core.logging import redact_sensitive_data
from app.modules.outbox.models import IdempotencyRecord, OutboxEvent


def enqueue_outbox_event(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    aggregate_revision: int,
    event_type: str,
    payload: dict[str, Any],
    payload_version: int = 1,
) -> OutboxEvent:
    """在当前活跃租户事务内追加一条 OutboxEvent（自动脱敏载荷）。"""
    sanitized = redact_sensitive_data(payload)
    event = OutboxEvent(
        organization_id=organization_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        aggregate_revision=aggregate_revision,
        event_type=event_type,
        payload_version=payload_version,
        payload_json=sanitized if isinstance(sanitized, dict) else {},
    )
    session.add(event)
    return event


async def lookup_idempotency_record(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    principal_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    request_payload: Any,
) -> tuple[str, str, IdempotencyRecord | None]:
    """查询幂等记录；若同键不同参立即抛出 409 ConflictError。

    Returns:
        `(fingerprint, request_hash, existing_record_or_none)`
    """
    scope = IdempotencyScope.create(
        principal_id=principal_id,
        organization_id=organization_id,
        operation=operation,
    )
    fingerprint = build_idempotency_fingerprint(scope, idempotency_key)
    request_hash = compute_request_payload_hash(request_payload)

    existing = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.organization_id == organization_id,
                IdempotencyRecord.fingerprint == fingerprint,
            )
        )
    ).scalar_one_or_none()

    if existing is not None and existing.request_hash != request_hash:
        raise ConflictError(
            code="IDEMPOTENCY_KEY_CONFLICT",
            message="相同的 Idempotency-Key 已被用于不同的请求参数",
        )

    return fingerprint, request_hash, existing


def save_idempotency_record(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    principal_id: uuid.UUID,
    operation: str,
    idempotency_key: str,
    fingerprint: str,
    request_hash: str,
    response_status: int,
    response_body: dict[str, Any],
) -> IdempotencyRecord:
    """在当前事务内持久化幂等响应快照。"""
    record = IdempotencyRecord(
        organization_id=organization_id,
        principal_id=principal_id,
        operation=operation,
        idempotency_key=idempotency_key,
        fingerprint=fingerprint,
        request_hash=request_hash,
        response_status=response_status,
        response_body_json=response_body,
    )
    session.add(record)
    return record
