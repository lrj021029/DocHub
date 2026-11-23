"""Celery 异步业务任务与事务性 Outbox 幂等分发消费者（OutboxDispatcherService）。

核心安全与可靠性保证（TECHNICAL_SPEC §1.2.10 & §6.4）：
1. 基于 `outbox_events` + `processed_messages`（`organization_id + consumer_name + event_id`
   唯一约束）实现消费者严格幂等去重：同一 `event_id` 重复投递时零副作用直接跳过。
2. Worker 绝不信任消息载荷内的敏感度或权限缓存结论：
   - 处理文档版本时回查 PostgreSQL `Document` / `DocumentVersion` / `KnowledgeSpace` 最新事实；
   - 处理 `acl.revision.created` 时通过 `AuthoritativeACLEngine.get_document_acl_projection`
     回查 PostgreSQL 最新授权快照后再异步更新 Qdrant 预过滤 Payload。
3. 所有数据库操作严格运行在 `org_rls_session` 事务级隔离上下文下。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.session import AsyncSessionLocal, org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.documents.models import Document
from app.modules.indexing.models import ActiveIndexPointer, IndexGeneration
from app.modules.indexing.pipeline import DocumentIngestionPipeline
from app.modules.indexing.qdrant_store import ACLProjectionPayload, QdrantIndexStore
from app.modules.outbox.models import OutboxEvent, ProcessedMessage
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

DEFAULT_OUTBOX_CONSUMER = "dochub_outbox_dispatcher"

FaqInvalidateHook = Callable[[uuid.UUID, OutboxEvent], Awaitable[None] | None]


class OutboxDispatchOutcome(BaseModel):
    """单条 OutboxEvent 消费结果。"""

    event_id: uuid.UUID
    event_type: str
    status: str = Field(
        ...,
        description="消费结果状态（PROCESSED / SKIPPED_DUPLICATE / IGNORED / FAILED）",
    )
    updated_collections: list[str] = Field(default_factory=list)
    detail: str | None = None


class OutboxRelayBatchReport(BaseModel):
    """批量中继 Outbox 待投递事件的执行汇总。"""

    organization_id: uuid.UUID
    scanned_count: int = 0
    processed_count: int = 0
    skipped_duplicate_count: int = 0
    failed_count: int = 0
    outcomes: list[OutboxDispatchOutcome] = Field(default_factory=list)


class OutboxDispatcherService:
    """事务性发件箱（Outbox）幂等消费者服务。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        qdrant_store: QdrantIndexStore | None = None,
        acl_engine: AuthoritativeACLEngine | None = None,
        pipeline: DocumentIngestionPipeline | None = None,
        faq_invalidate_hook: FaqInvalidateHook | None = None,
        consumer_name: str = DEFAULT_OUTBOX_CONSUMER,
    ) -> None:
        self._settings = settings or get_settings()
        self._qdrant = qdrant_store or QdrantIndexStore(settings=self._settings)
        self._acl_engine = acl_engine or AuthoritativeACLEngine()
        self._pipeline = pipeline
        self._faq_invalidate_hook = faq_invalidate_hook
        self._consumer_name = consumer_name

    async def _resolve_target_collections_for_document(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> list[str]:
        """在当前 org_rls_session 下查找该文档所在 Space 需要同步 ACL Payload 的 Collection。"""
        space_id = (
            await session.execute(
                select(Document.space_id).where(
                    Document.organization_id == organization_id,
                    Document.id == document_id,
                )
            )
        ).scalar_one_or_none()
        if space_id is None:
            return []

        collections: list[str] = []
        pointer_row = (
            await session.execute(
                select(IndexGeneration.collection_name)
                .join(
                    ActiveIndexPointer,
                    ActiveIndexPointer.index_generation_id == IndexGeneration.id,
                )
                .where(
                    ActiveIndexPointer.organization_id == organization_id,
                    ActiveIndexPointer.scope_type == "KNOWLEDGE_SPACE",
                    ActiveIndexPointer.scope_id == space_id,
                )
            )
        ).scalar_one_or_none()
        if pointer_row:
            collections.append(pointer_row)

        # 同时覆盖同一组织内处于 ACTIVE / READY 状态的代次，保证蓝绿切换期同步一致
        extra_cols = (
            (
                await session.execute(
                    select(IndexGeneration.collection_name)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.status.in_(("ACTIVE", "READY")),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        for col in extra_cols:
            if col not in collections:
                collections.append(col)
        return collections

    async def _handle_acl_revision_created(
        self,
        session: AsyncSession,
        *,
        event: OutboxEvent,
    ) -> tuple[list[str], str]:
        """消费 `acl.revision.created`：回查 PG 最新 ACL 投影并同步更新 Qdrant Payload。"""
        payload = event.payload_json or {}
        raw_doc_id = payload.get("document_id") or str(event.aggregate_id)
        document_id = uuid.UUID(str(raw_doc_id))

        projection = await self._acl_engine.get_document_acl_projection(
            session,
            organization_id=event.organization_id,
            document_id=document_id,
        )
        collections = await self._resolve_target_collections_for_document(
            session,
            organization_id=event.organization_id,
            document_id=document_id,
        )

        acl_payload = ACLProjectionPayload(
            acl_global=projection.acl_global,
            acl_dept_ids=projection.acl_dept_ids,
            acl_role_ids=projection.acl_role_ids,
            acl_user_ids=projection.acl_user_ids,
        )
        updated: list[str] = []
        for col_name in collections:
            exists = await self._qdrant._client.collection_exists(collection_name=col_name)  # noqa: SLF001
            if not exists:
                continue
            await self._qdrant.update_document_acl_payload(
                col_name,
                organization_id=str(event.organization_id),
                document_id=str(document_id),
                acl_revision=projection.acl_revision,
                acl_projection=acl_payload,
            )
            updated.append(col_name)

        return updated, f"已同步 ACL revision={projection.acl_revision}"

    async def _handle_document_reprocess(
        self,
        session: AsyncSession,
        *,
        event: OutboxEvent,
    ) -> str:
        """消费文档上传/解析/重处理 Outbox 事件并驱动流水线执行。"""
        payload = event.payload_json or {}
        raw_ver_id = payload.get("document_version_id") or str(event.aggregate_id)
        version_id = uuid.UUID(str(raw_ver_id))
        pipeline = self._pipeline or DocumentIngestionPipeline(
            settings=self._settings,
            qdrant_store=self._qdrant,
            acl_engine=self._acl_engine,
        )
        res = await pipeline.process_document_version(
            session,
            organization_id=event.organization_id,
            document_version_id=version_id,
        )
        return f"流水线处理完成: status={res.status}"

    async def _handle_faq_cache_invalidate(
        self,
        *,
        event: OutboxEvent,
    ) -> str:
        """预留 `faq.cache.invalidate` 事件消费钩子。"""
        if self._faq_invalidate_hook is not None:
            ret = self._faq_invalidate_hook(event.organization_id, event)
            if inspect.isawaitable(ret):
                await ret
            return "已执行 FAQ 缓存失效回调"
        logger.info(
            "收到 FAQ 缓存失效钩子事件（预留扩展）: org=%s event_id=%s",
            event.organization_id,
            event.id,
        )
        return "FAQ 缓存失效钩子已登记"

    async def process_event(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        event_id: uuid.UUID,
    ) -> OutboxDispatchOutcome:
        """在受控 org_rls_session 下幂等消费单条 OutboxEvent。"""
        async with org_rls_session(session, organization_id):
            event = (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.organization_id == organization_id,
                        OutboxEvent.id == event_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if event is None:
                return OutboxDispatchOutcome(
                    event_id=event_id,
                    event_type="unknown",
                    status="IGNORED",
                    detail="OutboxEvent 不存在或不属于当前组织",
                )

            existing_pm = (
                await session.execute(
                    select(ProcessedMessage.id).where(
                        ProcessedMessage.organization_id == organization_id,
                        ProcessedMessage.consumer_name == self._consumer_name,
                        ProcessedMessage.event_id == event.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_pm is not None:
                return OutboxDispatchOutcome(
                    event_id=event.id,
                    event_type=event.event_type,
                    status="SKIPPED_DUPLICATE",
                    detail="事件已存在于 processed_messages，幂等跳过",
                )

            event_type = event.event_type
            event_snapshot = event

        # 执行具体领域副作用（内部自行开启/复用安全会话上下文）
        updated_collections: list[str] = []
        detail_msg: str | None = None
        try:
            if event_type == "acl.revision.created":
                async with org_rls_session(session, organization_id):
                    updated_collections, detail_msg = await self._handle_acl_revision_created(
                        session,
                        event=event_snapshot,
                    )
            elif event_type in {
                "document.reprocess.requested",
                "document.parse.requested",
                "document.uploaded",
            }:
                detail_msg = await self._handle_document_reprocess(
                    session,
                    event=event_snapshot,
                )
            elif event_type == "faq.cache.invalidate":
                detail_msg = await self._handle_faq_cache_invalidate(event=event_snapshot)
            else:
                detail_msg = f"未注册专项处理器，按已确认事件归档: {event_type}"
        except Exception as exc:
            async with org_rls_session(session, organization_id):
                ev_row = (
                    await session.execute(
                        select(OutboxEvent)
                        .where(
                            OutboxEvent.organization_id == organization_id,
                            OutboxEvent.id == event_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if ev_row is not None:
                    ev_row.attempt_count += 1
                    ev_row.last_error = f"{type(exc).__name__}: {str(exc)[:400]}"
            raise

        # 记录 ProcessedMessage 并标记 published_at（利用唯一约束保证并发幂等）
        now = datetime.now(UTC)
        async with org_rls_session(session, organization_id):
            ev_row = (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.organization_id == organization_id,
                        OutboxEvent.id == event_id,
                    )
                    .with_for_update()
                )
            ).scalar_one()
            try:
                async with session.begin_nested():
                    session.add(
                        ProcessedMessage(
                            organization_id=organization_id,
                            consumer_name=self._consumer_name,
                            event_id=ev_row.id,
                            aggregate_revision=ev_row.aggregate_revision,
                        )
                    )
                    await session.flush()
            except IntegrityError:
                return OutboxDispatchOutcome(
                    event_id=ev_row.id,
                    event_type=ev_row.event_type,
                    status="SKIPPED_DUPLICATE",
                    detail="并发消费命中 processed_messages 唯一约束，安全跳过",
                )

            ev_row.attempt_count += 1
            ev_row.published_at = now
            ev_row.last_error = None

        return OutboxDispatchOutcome(
            event_id=event_id,
            event_type=event_type,
            status="PROCESSED",
            updated_collections=updated_collections,
            detail=detail_msg,
        )

    async def relay_pending_events(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        event_types: tuple[str, ...] | None = None,
        limit: int = 50,
    ) -> OutboxRelayBatchReport:
        """拉取当前组织尚未投递的 OutboxEvent 批量幂等消费。"""
        async with org_rls_session(session, organization_id):
            stmt = (
                select(OutboxEvent.id)
                .where(
                    OutboxEvent.organization_id == organization_id,
                    OutboxEvent.published_at.is_(None),
                )
                .order_by(OutboxEvent.created_at.asc())
                .limit(max(1, limit))
            )
            if event_types:
                stmt = stmt.where(OutboxEvent.event_type.in_(event_types))
            pending_ids = list((await session.execute(stmt)).scalars().all())

        report = OutboxRelayBatchReport(
            organization_id=organization_id,
            scanned_count=len(pending_ids),
        )
        for ev_id in pending_ids:
            try:
                outcome = await self.process_event(
                    session,
                    organization_id=organization_id,
                    event_id=ev_id,
                )
                report.outcomes.append(outcome)
                if outcome.status == "PROCESSED":
                    report.processed_count += 1
                elif outcome.status == "SKIPPED_DUPLICATE":
                    report.skipped_duplicate_count += 1
            except Exception as exc:
                report.failed_count += 1
                report.outcomes.append(
                    OutboxDispatchOutcome(
                        event_id=ev_id,
                        event_type="error",
                        status="FAILED",
                        detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                    )
                )

        return report


async def _run_process_document_version_async(
    organization_id: str,
    document_version_id: str,
) -> dict[str, Any]:
    org_uuid = uuid.UUID(organization_id)
    ver_uuid = uuid.UUID(document_version_id)
    pipeline = DocumentIngestionPipeline()
    try:
        async with AsyncSessionLocal() as session:
            result = await pipeline.process_document_version(
                session,
                organization_id=org_uuid,
                document_version_id=ver_uuid,
            )
            return result.model_dump(mode="json")
    finally:
        await pipeline._qdrant.close()  # noqa: SLF001


async def _run_relay_outbox_async(
    organization_id: str,
    limit: int,
) -> dict[str, Any]:
    org_uuid = uuid.UUID(organization_id)
    dispatcher = OutboxDispatcherService()
    try:
        async with AsyncSessionLocal() as session:
            report = await dispatcher.relay_pending_events(
                session,
                organization_id=org_uuid,
                limit=limit,
            )
            return report.model_dump(mode="json")
    finally:
        await dispatcher._qdrant.close()  # noqa: SLF001


@celery_app.task(name="app.workers.tasks.ingestion.process_document_version")
def process_document_version_task(
    organization_id: str,
    document_version_id: str,
) -> dict[str, Any]:
    """Celery 文档解析/切片/向量化/蓝绿建索引异步任务（队列：`ingestion`）。

    注意：入队参数仅传递 `organization_id` 与 `document_version_id`，任务内部强制
    通过 `org_rls_session` 回查 PostgreSQL 最新敏感度与权限事实。
    """
    return asyncio.run(_run_process_document_version_async(organization_id, document_version_id))


@celery_app.task(name="app.workers.tasks.outbox.relay_pending_events")
def relay_outbox_events_task(
    organization_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Celery Outbox 异步中继消费任务（队列：`outbox`）。"""
    if not organization_id:
        return {"status": "idle", "reason": "missing_organization_id"}
    return asyncio.run(_run_relay_outbox_async(organization_id, limit))


@celery_app.task(name="app.workers.tasks.indexing.reconcile_active_index_pointers")
def reconcile_active_index_pointers_task() -> dict[str, str]:
    """Celery 蓝绿索引指针巡检对账任务骨架（队列：`indexing`）。"""
    return {"status": "ok", "task": "reconcile_active_index_pointers"}


@celery_app.task(name="app.workers.tasks.faq.run_scheduled_question_clustering")
def run_scheduled_question_clustering_task() -> dict[str, str]:
    """Celery FAQ 定时批处理聚类任务骨架（队列：`faq`）。"""
    return {"status": "ok", "task": "run_scheduled_question_clustering"}
