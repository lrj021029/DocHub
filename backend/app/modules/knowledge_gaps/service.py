"""Knowledge Gap 准入分类器、11 态闭环治理与回归验证门禁服务。"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

import jieba
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.modules.audit.service import record_audit_event, record_security_audit_event
from app.modules.documents.models import Chunk, Document
from app.modules.faq.mining import QuestionRedactor, hash_identifier
from app.modules.faq.models import FAQ, FAQSegment, FAQVersion
from app.modules.identity.models import User
from app.modules.knowledge_gaps.models import (
    KnowledgeGap,
    KnowledgeGapTask,
    QuestionEvent,
)
from app.modules.knowledge_gaps.schemas import (
    GAP_11_STATES,
    GAP_ADMITTED_OUTCOMES,
    GAP_OPERATIONAL_REJECTED_OUTCOMES,
    GAP_SECURITY_REJECTED_OUTCOMES,
    GAP_TASK_ALLOWED_STATES,
    GapSignalIngestionResult,
    GapSignalInput,
    KnowledgeGapItem,
    KnowledgeGapListResponse,
    KnowledgeGapTaskCreateRequest,
    KnowledgeGapTaskItem,
    KnowledgeGapTaskUpdateRequest,
)
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.outbox.service import enqueue_outbox_event

_PRIORITY_RANK: dict[str, int] = {
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}
_OPEN_GAP_EXCLUDED_STATES: tuple[str, ...] = (
    "RESOLVED",
    "REJECTED",
    "DUPLICATED",
    "CLOSED",
)
_CJK_SPAN_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_REGRESSION_MIN_SCORE = 0.25


def _extract_regression_tokens(text: str) -> set[str]:
    """提取用于回归验证检索的混合词项（jieba 切词 + 汉字二元组 + 英文词）。"""
    lowered = text.lower()
    tokens: set[str] = set()
    for word in jieba.cut_for_search(lowered):
        cleaned = word.strip()
        if len(cleaned) >= 2:
            tokens.add(cleaned)
    for span in _CJK_SPAN_RE.findall(lowered):
        if len(span) == 1:
            tokens.add(span)
        for idx in range(len(span) - 1):
            tokens.add(span[idx : idx + 2])
    for ascii_tok in _ASCII_TOKEN_RE.findall(lowered):
        if len(ascii_tok) >= 2:
            tokens.add(ascii_tok)
    return tokens


def _compute_text_match_score(query: str, candidate_text: str) -> float:
    """计算回归验证问句与候选文档切片/FAQ 正文的词汇覆盖率得分。"""
    q_tokens = _extract_regression_tokens(query)
    if not q_tokens:
        return 0.0
    c_lower = candidate_text.lower()
    c_tokens = _extract_regression_tokens(c_lower)
    hits = sum(1 for tok in q_tokens if tok in c_tokens or tok in c_lower)
    return hits / len(q_tokens)


async def ingest_gap_signal(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    signal: GapSignalInput,
) -> GapSignalIngestionResult:
    """知识缺口准入分类器：严格区分真缺口、权限拒绝与系统服务故障。"""
    outcome = signal.outcome.strip().upper()
    redacted_text = QuestionRedactor.normalize(signal.question_text) or "[EMPTY]"
    user_hash = hash_identifier(
        str(signal.user_id) if signal.user_id is not None else "anonymous",
        salt=f"user:{organization_id}",
    )
    session_hash = hash_identifier(
        signal.session_id or "anonymous",
        salt=f"session:{organization_id}",
    )

    event = QuestionEvent(
        organization_id=organization_id,
        space_id=signal.space_id,
        user_hash=user_hash,
        session_hash=session_hash,
        department_id=signal.department_id,
        normalized_text=redacted_text,
        sensitivity_level=signal.sensitivity_level,
        outcome=outcome,
    )
    session.add(event)
    await session.flush()

    if outcome in GAP_SECURITY_REJECTED_OUTCOMES:
        # 绝不允许将 ACL 越权/无权限命中写入 KnowledgeGap，必须独立记入安全审计事件
        record_security_audit_event(
            session,
            organization_id=organization_id,
            actor_user_id=signal.user_id,
            event_type=f"GAP_SIGNAL_REJECTED_{outcome}",
            severity="HIGH",
            resource_type="knowledge_space",
            resource_id=signal.space_id,
            details={
                "outcome": outcome,
                "redacted_question": redacted_text,
                "reason": "权限拒绝事件严禁伪装成知识缺口进入 knowledge_gaps",
            },
        )
        await session.flush()
        return GapSignalIngestionResult(
            admitted=False,
            action="ROUTED_TO_SECURITY_AUDIT",
            gap_id=None,
            question_event_id=event.id,
            redacted_text=redacted_text,
        )

    if outcome in GAP_OPERATIONAL_REJECTED_OUTCOMES:
        # 模型网关超时、MinerU 服务不可用属于运维故障，严禁误归因到内容缺口
        await session.flush()
        return GapSignalIngestionResult(
            admitted=False,
            action="SKIPPED_OPERATIONAL_FAILURE",
            gap_id=None,
            question_event_id=event.id,
            redacted_text=redacted_text,
        )

    if outcome not in GAP_ADMITTED_OUTCOMES:
        raise ValidationError(
            code="INVALID_GAP_OUTCOME",
            message=f"不支持的知识缺口信号类型: {outcome}",
        )

    now = datetime.now(UTC)
    topic = redacted_text[:255]
    gap_stmt: Select[tuple[KnowledgeGap]] = select(KnowledgeGap).where(
        KnowledgeGap.organization_id == organization_id,
        KnowledgeGap.status.notin_(_OPEN_GAP_EXCLUDED_STATES),
    )
    if signal.cluster_id is not None:
        gap_stmt = gap_stmt.where(KnowledgeGap.cluster_id == signal.cluster_id)
    else:
        gap_stmt = gap_stmt.where(
            KnowledgeGap.space_id == signal.space_id,
            KnowledgeGap.title == topic,
        )

    existing_gap = (await session.execute(gap_stmt)).scalar_one_or_none()
    if existing_gap is not None:
        existing_gap.question_count += 1
        existing_gap.unique_user_count += 1
        existing_gap.last_seen_at = now
        if _PRIORITY_RANK.get(signal.priority, 0) > _PRIORITY_RANK.get(existing_gap.priority, 0):
            existing_gap.priority = signal.priority
        await session.flush()
        return GapSignalIngestionResult(
            admitted=True,
            action="ADMITTED_TO_GAP",
            gap_id=existing_gap.id,
            question_event_id=event.id,
            redacted_text=redacted_text,
        )

    gap = KnowledgeGap(
        organization_id=organization_id,
        space_id=signal.space_id,
        cluster_id=signal.cluster_id,
        title=topic,
        status="DETECTED",
        priority=signal.priority,
        question_count=1,
        unique_user_count=1,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(gap)
    await session.flush()

    enqueue_outbox_event(
        session,
        organization_id=organization_id,
        aggregate_type="knowledge_gap",
        aggregate_id=gap.id,
        aggregate_revision=1,
        event_type="knowledge_gap.detected",
        payload={
            "gap_id": str(gap.id),
            "space_id": str(signal.space_id) if signal.space_id else None,
            "outcome": outcome,
            "topic": topic,
            "priority": signal.priority,
        },
    )
    await session.flush()
    return GapSignalIngestionResult(
        admitted=True,
        action="ADMITTED_TO_GAP",
        gap_id=gap.id,
        question_event_id=event.id,
        redacted_text=redacted_text,
    )


def _to_task_item(
    task: KnowledgeGapTask,
    *,
    assignee_name: str | None = None,
) -> KnowledgeGapTaskItem:
    """将 ORM KnowledgeGapTask 转换为 API 响应模型。"""
    evidence = task.resolution_evidence_json or {}
    reg_flag = evidence.get("regression_passed")
    return KnowledgeGapTaskItem(
        id=task.id,
        gap_id=task.gap_id,
        assignee_id=task.assignee_id,
        assignee_name=assignee_name,
        priority=task.priority,
        status=task.status,
        due_at=task.due_at,
        related_document_id=task.related_document_id,
        related_faq_id=task.related_faq_id,
        regression_passed=bool(reg_flag) if reg_flag is not None else None,
        resolution_evidence_json=evidence,
    )


async def list_knowledge_gaps(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> KnowledgeGapListResponse:
    """分页查询知识缺口列表及其整改任务明细。"""
    stmt = select(KnowledgeGap).where(KnowledgeGap.organization_id == organization_id)
    if space_id is not None:
        stmt = stmt.where(KnowledgeGap.space_id == space_id)
    if status is not None:
        stmt = stmt.where(KnowledgeGap.status == status)
    stmt = stmt.order_by(KnowledgeGap.last_seen_at.desc()).limit(limit).offset(offset)
    gaps = (await session.execute(stmt)).scalars().all()
    if not gaps:
        return KnowledgeGapListResponse(items=[], total=0)

    gap_ids = [g.id for g in gaps]
    space_ids = {g.space_id for g in gaps if g.space_id is not None}

    space_names: dict[uuid.UUID, str] = {}
    if space_ids:
        space_rows = (
            await session.execute(
                select(KnowledgeSpace.id, KnowledgeSpace.name).where(
                    KnowledgeSpace.organization_id == organization_id,
                    KnowledgeSpace.id.in_(space_ids),
                )
            )
        ).all()
        space_names = {row[0]: row[1] for row in space_rows}

    tasks = (
        (
            await session.execute(
                select(KnowledgeGapTask)
                .where(
                    KnowledgeGapTask.organization_id == organization_id,
                    KnowledgeGapTask.gap_id.in_(gap_ids),
                )
                .order_by(KnowledgeGapTask.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    assignee_ids = {t.assignee_id for t in tasks if t.assignee_id is not None}
    assignee_names: dict[uuid.UUID, str] = {}
    if assignee_ids:
        user_rows = (
            await session.execute(
                select(User.id, User.display_name, User.email).where(
                    User.id.in_(assignee_ids),
                )
            )
        ).all()
        assignee_names = {row[0]: (row[1] or row[2]) for row in user_rows}

    tasks_by_gap: dict[uuid.UUID, list[KnowledgeGapTaskItem]] = {}
    for task in tasks:
        tasks_by_gap.setdefault(task.gap_id, []).append(
            _to_task_item(
                task,
                assignee_name=assignee_names.get(task.assignee_id) if task.assignee_id else None,
            )
        )

    topics = [g.title for g in gaps]
    event_outcomes: dict[str, str] = {}
    if topics:
        ev_rows = (
            await session.execute(
                select(QuestionEvent.normalized_text, QuestionEvent.outcome)
                .where(
                    QuestionEvent.organization_id == organization_id,
                    QuestionEvent.normalized_text.in_(topics),
                    QuestionEvent.outcome.in_(GAP_ADMITTED_OUTCOMES),
                )
                .order_by(QuestionEvent.created_at.desc())
            )
        ).all()
        for text_val, outcome_val in ev_rows:
            event_outcomes.setdefault(text_val[:255], outcome_val)

    items = [
        KnowledgeGapItem(
            id=g.id,
            organization_id=g.organization_id,
            space_id=g.space_id,
            space_name=space_names.get(g.space_id) if g.space_id else None,
            topic=g.title,
            event_type=event_outcomes.get(g.title, "NO_RETRIEVAL"),
            occurrence_count=g.question_count,
            unique_user_count=g.unique_user_count,
            status=g.status,
            priority=g.priority,
            tasks=tasks_by_gap.get(g.id, []),
            last_seen_at=g.last_seen_at,
            created_at=g.created_at,
        )
        for g in gaps
    ]
    return KnowledgeGapListResponse(items=items, total=len(items))


async def _validate_related_resources(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    document_id: uuid.UUID | None,
    faq_id: uuid.UUID | None,
) -> None:
    """校验任务关联的文档或 FAQ 属于当前租户且真实存在。"""
    if document_id is not None:
        doc = (
            await session.execute(
                select(Document.id).where(
                    Document.organization_id == organization_id,
                    Document.id == document_id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="关联文档不存在")

    if faq_id is not None:
        faq = (
            await session.execute(
                select(FAQ.id).where(
                    FAQ.organization_id == organization_id,
                    FAQ.id == faq_id,
                )
            )
        ).scalar_one_or_none()
        if faq is None:
            raise NotFoundError(code="FAQ_NOT_FOUND", message="关联 FAQ 不存在")


async def create_knowledge_gap_task(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    gap_id: uuid.UUID,
    payload: KnowledgeGapTaskCreateRequest,
) -> KnowledgeGapTaskItem:
    """为指定 KnowledgeGap 指派知识补全整改任务。"""
    gap = (
        await session.execute(
            select(KnowledgeGap).where(
                KnowledgeGap.organization_id == organization_id,
                KnowledgeGap.id == gap_id,
            )
        )
    ).scalar_one_or_none()
    if gap is None:
        raise NotFoundError(code="GAP_NOT_FOUND", message="知识缺口记录不存在")

    await _validate_related_resources(
        session,
        organization_id=organization_id,
        document_id=payload.related_document_id,
        faq_id=payload.related_faq_id,
    )

    task = KnowledgeGapTask(
        organization_id=organization_id,
        gap_id=gap.id,
        assignee_id=payload.assignee_id,
        status="ASSIGNED",
        priority=payload.priority,
        due_at=payload.due_at,
        related_document_id=payload.related_document_id,
        related_faq_id=payload.related_faq_id,
        resolution_evidence_json={},
    )
    session.add(task)

    if gap.status in ("DETECTED", "TRIAGED", "REOPENED"):
        gap.status = "ASSIGNED"

    record_audit_event(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="knowledge_gap.task.created",
        resource_type="knowledge_gap_task",
        resource_id=task.id,
        details={
            "gap_id": str(gap.id),
            "assignee_id": str(payload.assignee_id) if payload.assignee_id else None,
            "priority": payload.priority,
        },
    )
    await session.flush()

    assignee_name: str | None = None
    if task.assignee_id is not None:
        row = (
            await session.execute(
                select(User.display_name, User.email).where(User.id == task.assignee_id)
            )
        ).first()
        if row is not None:
            assignee_name = row[0] or row[1]

    return _to_task_item(task, assignee_name=assignee_name)


async def _run_gap_regression_verification(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    gap: KnowledgeGap,
    document_id: uuid.UUID | None,
    faq_id: uuid.UUID | None,
    custom_query: str | None,
) -> dict[str, Any]:
    """执行知识缺口回归验证查询，确保关联文档已生效或关联 FAQ 已发布且命中缺口问句。"""
    query_text = (custom_query or gap.title).strip()
    verified_at = datetime.now(UTC).isoformat()
    best_score = 0.0
    matched_chunk_ids: list[str] = []
    reasons: list[str] = []

    if document_id is not None:
        doc = (
            await session.execute(
                select(Document).where(
                    Document.organization_id == organization_id,
                    Document.id == document_id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            reasons.append("关联文档不存在")
        elif doc.status != "ACTIVE" or doc.current_version_id is None:
            reasons.append(f"关联文档状态为 {doc.status}，尚未发布生效（必须为 ACTIVE）")
        elif gap.space_id is not None and doc.space_id != gap.space_id:
            reasons.append("关联文档所在知识空间与缺口所属空间不一致")
        else:
            chunks = (
                (
                    await session.execute(
                        select(Chunk).where(
                            Chunk.organization_id == organization_id,
                            Chunk.document_version_id == doc.current_version_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not chunks:
                reasons.append("关联生效文档尚未完成切片入库，无法命中缺口问题")
            else:
                for chunk in chunks:
                    score = _compute_text_match_score(query_text, chunk.content_text)
                    if score >= _REGRESSION_MIN_SCORE:
                        matched_chunk_ids.append(str(chunk.id))
                    if score > best_score:
                        best_score = score
                if best_score < _REGRESSION_MIN_SCORE:
                    reasons.append(
                        f"文档切片回归匹配得分 {best_score:.2f} 低于阈值 "
                        f"{_REGRESSION_MIN_SCORE:.2f}"
                    )

    if faq_id is not None:
        faq = (
            await session.execute(
                select(FAQ).where(
                    FAQ.organization_id == organization_id,
                    FAQ.id == faq_id,
                )
            )
        ).scalar_one_or_none()
        if faq is None:
            reasons.append("关联 FAQ 不存在")
        elif faq.publication_state != "PUBLISHED" or faq.source_health_state != "HEALTHY":
            reasons.append(
                f"关联 FAQ 状态为 {faq.publication_state}/{faq.source_health_state}，"
                "不满足 PUBLISHED+HEALTHY"
            )
        elif faq.current_version_id is None:
            reasons.append("关联 FAQ 缺少有效版本")
        else:
            ver = (
                await session.execute(
                    select(FAQVersion).where(
                        FAQVersion.organization_id == organization_id,
                        FAQVersion.id == faq.current_version_id,
                    )
                )
            ).scalar_one_or_none()
            segs = (
                (
                    await session.execute(
                        select(FAQSegment.content_text).where(
                            FAQSegment.organization_id == organization_id,
                            FAQSegment.faq_version_id == faq.current_version_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            combined_faq_text = " ".join([ver.question if ver else "", *[s for s in segs if s]])
            faq_score = _compute_text_match_score(query_text, combined_faq_text)
            if faq_score > best_score:
                best_score = faq_score
            if faq_score < _REGRESSION_MIN_SCORE and not matched_chunk_ids:
                reasons.append(
                    f"FAQ 回归匹配得分 {faq_score:.2f} 低于阈值 {_REGRESSION_MIN_SCORE:.2f}"
                )

    passed = (
        best_score >= _REGRESSION_MIN_SCORE
        and (bool(matched_chunk_ids) or faq_id is not None)
        and not reasons
    )
    return {
        "regression_passed": passed,
        "query": query_text,
        "best_score": round(best_score, 4),
        "threshold": _REGRESSION_MIN_SCORE,
        "document_id": str(document_id) if document_id else None,
        "faq_id": str(faq_id) if faq_id else None,
        "matched_chunk_ids": matched_chunk_ids,
        "reason": "; ".join(reasons) if reasons else None,
        "verified_at": verified_at,
    }


async def update_knowledge_gap_task(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: KnowledgeGapTaskUpdateRequest,
) -> KnowledgeGapTaskItem:
    """更新整改任务状态；若变更至 RESOLVED 则强制校验回归验证门禁。"""
    task = (
        await session.execute(
            select(KnowledgeGapTask).where(
                KnowledgeGapTask.organization_id == organization_id,
                KnowledgeGapTask.id == task_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError(code="GAP_TASK_NOT_FOUND", message="知识缺口整改任务不存在")

    gap = (
        await session.execute(
            select(KnowledgeGap).where(
                KnowledgeGap.organization_id == organization_id,
                KnowledgeGap.id == task.gap_id,
            )
        )
    ).scalar_one_or_none()
    if gap is None:
        raise NotFoundError(code="GAP_NOT_FOUND", message="所属知识缺口不存在")

    if payload.status is not None and payload.status not in GAP_TASK_ALLOWED_STATES:
        raise ValidationError(
            code="INVALID_GAP_TASK_STATUS",
            message=f"非法任务状态: {payload.status}",
        )

    effective_doc_id = (
        payload.related_document_id
        if payload.related_document_id is not None
        else task.related_document_id
    )
    effective_faq_id = (
        payload.related_faq_id if payload.related_faq_id is not None else task.related_faq_id
    )
    await _validate_related_resources(
        session,
        organization_id=organization_id,
        document_id=effective_doc_id,
        faq_id=effective_faq_id,
    )

    if payload.assignee_id is not None:
        task.assignee_id = payload.assignee_id
    if payload.priority is not None:
        task.priority = payload.priority
    if payload.due_at is not None:
        task.due_at = payload.due_at
    task.related_document_id = effective_doc_id
    task.related_faq_id = effective_faq_id

    target_status = payload.status or task.status
    should_regress = payload.run_regression or target_status == "RESOLVED"
    if should_regress:
        if effective_doc_id is None and effective_faq_id is None:
            raise ConflictError(
                code="GAP_REGRESSION_MISSING_SOURCE",
                message="触发回归验证或标记 RESOLVED 前必须关联补充文档或发布态 FAQ",
            )
        evidence = await _run_gap_regression_verification(
            session,
            organization_id=organization_id,
            gap=gap,
            document_id=effective_doc_id,
            faq_id=effective_faq_id,
            custom_query=payload.regression_query,
        )
        merged_evidence = dict(task.resolution_evidence_json or {})
        merged_evidence.update(evidence)
        task.resolution_evidence_json = merged_evidence

        if target_status == "RESOLVED" and not evidence["regression_passed"]:
            await session.flush()
            raise ConflictError(
                code="GAP_REGRESSION_FAILED",
                message=f"回归验证未通过，拒绝关闭缺口: {evidence.get('reason')}",
            )

    task.status = target_status
    if target_status == "RESOLVED":
        gap.status = "RESOLVED"
        enqueue_outbox_event(
            session,
            organization_id=organization_id,
            aggregate_type="knowledge_gap",
            aggregate_id=gap.id,
            aggregate_revision=2,
            event_type="knowledge_gap.resolved",
            payload={
                "gap_id": str(gap.id),
                "task_id": str(task.id),
                "related_document_id": str(effective_doc_id) if effective_doc_id else None,
                "related_faq_id": str(effective_faq_id) if effective_faq_id else None,
                "regression_passed": True,
            },
        )
    elif target_status in ("IN_PROGRESS", "CONTENT_SUBMITTED", "REVIEWING"):
        gap.status = target_status

    record_audit_event(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="knowledge_gap.task.updated",
        resource_type="knowledge_gap_task",
        resource_id=task.id,
        details={
            "gap_id": str(gap.id),
            "status": task.status,
            "regression_passed": (task.resolution_evidence_json or {}).get("regression_passed"),
        },
    )
    await session.flush()

    assignee_name: str | None = None
    if task.assignee_id is not None:
        row = (
            await session.execute(
                select(User.display_name, User.email).where(User.id == task.assignee_id)
            )
        ).first()
        if row is not None:
            assignee_name = row[0] or row[1]

    return _to_task_item(task, assignee_name=assignee_name)


async def update_knowledge_gap_status(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    gap_id: uuid.UUID,
    target_status: str,
    reason: str | None = None,
) -> KnowledgeGap:
    """变更 KnowledgeGap 11 态状态（严禁直接跳过回归验证将状态设为 RESOLVED）。"""
    if target_status not in GAP_11_STATES:
        raise ValidationError(
            code="INVALID_GAP_STATUS",
            message=f"不支持的缺口状态: {target_status}",
        )
    if target_status == "RESOLVED":
        raise ConflictError(
            code="GAP_DIRECT_RESOLVE_FORBIDDEN",
            message=(
                "禁止跳过回归验证直接设为 RESOLVED，请通过整改任务提交关联文档/FAQ 并通过回归门禁"
            ),
        )

    gap = (
        await session.execute(
            select(KnowledgeGap).where(
                KnowledgeGap.organization_id == organization_id,
                KnowledgeGap.id == gap_id,
            )
        )
    ).scalar_one_or_none()
    if gap is None:
        raise NotFoundError(code="GAP_NOT_FOUND", message="知识缺口不存在")

    prev_status = gap.status
    gap.status = target_status
    record_audit_event(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        action="knowledge_gap.status.updated",
        resource_type="knowledge_gap",
        resource_id=gap.id,
        details={
            "from_status": prev_status,
            "to_status": target_status,
            "reason": reason,
        },
    )
    await session.flush()
    return gap
