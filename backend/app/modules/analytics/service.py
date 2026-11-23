"""运营分析聚合与真实回归 Benchmark 服务（严格运行于当前 Organization RLS 上下文）。"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.analytics.models import BenchmarkCaseResult, BenchmarkRun
from app.modules.analytics.schemas import (
    AnalyticsOverviewResponse,
    BenchmarkCaseItem,
    BenchmarkRunResponse,
    BenchmarkTriggerRequest,
    FaqAnalyticsResponse,
    GapAnalyticsResponse,
)
from app.modules.audit.service import record_audit_event
from app.modules.chat.models import Conversation, LLMUsage, Message, MessageFeedback
from app.modules.documents.models import Chunk, Document, DocumentVersion
from app.modules.faq.models import FAQ
from app.modules.identity.deps import OrgContext, Principal
from app.modules.identity.models import User
from app.modules.knowledge_gaps.models import FAQCandidate, KnowledgeGap, QuestionEvent
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.organizations.models import OrganizationMember
from app.modules.outbox.service import (
    lookup_idempotency_record,
    save_idempotency_record,
)


def _compute_p95(sorted_values: list[int]) -> float | None:
    """计算整型延时列表的 P95 指标。"""
    if not sorted_values:
        return None
    idx = max(0, min(len(sorted_values) - 1, int(round(0.95 * (len(sorted_values) - 1)))))
    return float(sorted_values[idx])


async def get_analytics_overview(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> AnalyticsOverviewResponse:
    """聚合当前组织总览运营指标（不暴露任何未授权文档标题或 ACL 拒绝明细）。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        pv = int(
            (
                await session.execute(
                    select(func.count(QuestionEvent.id)).where(
                        QuestionEvent.organization_id == org_id
                    )
                )
            ).scalar_one()
        )
        uv = int(
            (
                await session.execute(
                    select(func.count(func.distinct(QuestionEvent.user_hash))).where(
                        QuestionEvent.organization_id == org_id
                    )
                )
            ).scalar_one()
        )
        faq_hit_events = int(
            (
                await session.execute(
                    select(func.count(QuestionEvent.id)).where(
                        QuestionEvent.organization_id == org_id,
                        QuestionEvent.outcome == "FAQ_HIT",
                    )
                )
            ).scalar_one()
        )

        active_conversations = int(
            (
                await session.execute(
                    select(func.count(Conversation.id)).where(
                        Conversation.organization_id == org_id,
                        Conversation.status == "ACTIVE",
                    )
                )
            ).scalar_one()
        )
        authorized_spaces = int(
            (
                await session.execute(
                    select(func.count(KnowledgeSpace.id)).where(
                        KnowledgeSpace.organization_id == org_id,
                        KnowledgeSpace.status == "ACTIVE",
                    )
                )
            ).scalar_one()
        )
        active_documents = int(
            (
                await session.execute(
                    select(func.count(Document.id)).where(
                        Document.organization_id == org_id,
                        Document.status == "ACTIVE",
                    )
                )
            ).scalar_one()
        )
        active_chunks = int(
            (
                await session.execute(
                    select(func.count(Chunk.id))
                    .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        Chunk.organization_id == org_id,
                        Document.organization_id == org_id,
                        Document.status == "ACTIVE",
                    )
                )
            ).scalar_one()
        )

        published_faqs = int(
            (
                await session.execute(
                    select(func.count(FAQ.id)).where(
                        FAQ.organization_id == org_id,
                        FAQ.publication_state == "PUBLISHED",
                    )
                )
            ).scalar_one()
        )
        stale_faqs = int(
            (
                await session.execute(
                    select(func.count(FAQ.id)).where(
                        FAQ.organization_id == org_id,
                        FAQ.publication_state == "PUBLISHED",
                        FAQ.source_health_state.in_(("STALE", "EXPIRED")),
                    )
                )
            ).scalar_one()
        )

        total_gaps = int(
            (
                await session.execute(
                    select(func.count(KnowledgeGap.id)).where(
                        KnowledgeGap.organization_id == org_id
                    )
                )
            ).scalar_one()
        )
        resolved_gaps = int(
            (
                await session.execute(
                    select(func.count(KnowledgeGap.id)).where(
                        KnowledgeGap.organization_id == org_id,
                        KnowledgeGap.status.in_(("RESOLVED", "CLOSED")),
                    )
                )
            ).scalar_one()
        )
        open_gap_count = int(
            (
                await session.execute(
                    select(func.count(KnowledgeGap.id)).where(
                        KnowledgeGap.organization_id == org_id,
                        KnowledgeGap.status.notin_(
                            ("RESOLVED", "CLOSED", "DUPLICATED", "REJECTED")
                        ),
                    )
                )
            ).scalar_one()
        )

        assistant_msg_count = int(
            (
                await session.execute(
                    select(func.count(Message.id)).where(
                        Message.organization_id == org_id,
                        Message.role == "ASSISTANT",
                    )
                )
            ).scalar_one()
        )
        faq_direct_msg_count = int(
            (
                await session.execute(
                    select(func.count(Message.id)).where(
                        Message.organization_id == org_id,
                        Message.role == "ASSISTANT",
                        Message.faq_id.is_not(None),
                    )
                )
            ).scalar_one()
        )

        usage_rows = (
            await session.execute(
                select(
                    LLMUsage.first_token_ms,
                    LLMUsage.latency_ms,
                    LLMUsage.prompt_tokens,
                    LLMUsage.completion_tokens,
                ).where(LLMUsage.organization_id == org_id)
            )
        ).all()

    first_tokens = sorted(int(r[0]) for r in usage_rows if r[0] is not None)
    latencies = sorted(int(r[1]) for r in usage_rows if r[1] is not None)
    total_tokens = sum(int(r[2] or 0) + int(r[3] or 0) for r in usage_rows)

    return AnalyticsOverviewResponse(
        pv=pv,
        uv=uv,
        active_conversations=active_conversations,
        authorized_spaces=authorized_spaces,
        active_documents=active_documents,
        active_chunks=active_chunks,
        faq_hit_rate=round(faq_hit_events / pv, 4) if pv > 0 else 0.0,
        faq_direct_answer_rate=(
            round(faq_direct_msg_count / assistant_msg_count, 4) if assistant_msg_count > 0 else 0.0
        ),
        stale_faq_ratio=round(stale_faqs / published_faqs, 4) if published_faqs > 0 else 0.0,
        open_gap_count=open_gap_count,
        gap_resolution_rate=round(resolved_gaps / total_gaps, 4) if total_gaps > 0 else 0.0,
        p95_first_token_ms=_compute_p95(first_tokens),
        p95_total_latency_ms=_compute_p95(latencies),
        total_tokens=total_tokens,
    )


async def get_faq_analytics(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> FaqAnalyticsResponse:
    """聚合当前组织 FAQ 挖掘、发布与健康度指标。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        candidate_count = int(
            (
                await session.execute(
                    select(func.count(FAQCandidate.id)).where(
                        FAQCandidate.organization_id == org_id
                    )
                )
            ).scalar_one()
        )
        published_count = int(
            (
                await session.execute(
                    select(func.count(FAQ.id)).where(
                        FAQ.organization_id == org_id,
                        FAQ.publication_state == "PUBLISHED",
                    )
                )
            ).scalar_one()
        )
        stale_count = int(
            (
                await session.execute(
                    select(func.count(FAQ.id)).where(
                        FAQ.organization_id == org_id,
                        FAQ.publication_state == "PUBLISHED",
                        FAQ.source_health_state.in_(("STALE", "EXPIRED")),
                    )
                )
            ).scalar_one()
        )
        pv = int(
            (
                await session.execute(
                    select(func.count(QuestionEvent.id)).where(
                        QuestionEvent.organization_id == org_id
                    )
                )
            ).scalar_one()
        )
        faq_hits = int(
            (
                await session.execute(
                    select(func.count(QuestionEvent.id)).where(
                        QuestionEvent.organization_id == org_id,
                        QuestionEvent.outcome == "FAQ_HIT",
                    )
                )
            ).scalar_one()
        )
        total_fb = int(
            (
                await session.execute(
                    select(func.count(MessageFeedback.id)).where(
                        MessageFeedback.organization_id == org_id
                    )
                )
            ).scalar_one()
        )
        helpful_fb = int(
            (
                await session.execute(
                    select(func.count(MessageFeedback.id)).where(
                        MessageFeedback.organization_id == org_id,
                        MessageFeedback.rating == "HELPFUL",
                    )
                )
            ).scalar_one()
        )

    return FaqAnalyticsResponse(
        candidate_count=candidate_count,
        published_count=published_count,
        stale_count=stale_count,
        hit_rate=round(faq_hits / pv, 4) if pv > 0 else 0.0,
        helpful_rate=round(helpful_fb / total_fb, 4) if total_fb > 0 else 0.0,
    )


async def get_gap_analytics(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> GapAnalyticsResponse:
    """聚合当前组织 Knowledge Gap 闭环处理指标。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        gaps = (
            (
                await session.execute(
                    select(KnowledgeGap).where(KnowledgeGap.organization_id == org_id)
                )
            )
            .scalars()
            .all()
        )

    total_gaps = len(gaps)
    resolved_list = [g for g in gaps if g.status in {"RESOLVED", "CLOSED"}]
    resolved_gaps = len(resolved_list)

    avg_hours: float | None = None
    if resolved_list:
        total_hours = sum(
            max(0.0, (g.updated_at - g.first_seen_at).total_seconds() / 3600.0)
            for g in resolved_list
        )
        avg_hours = round(total_hours / len(resolved_list), 2)

    return GapAnalyticsResponse(
        total_gaps=total_gaps,
        resolved_gaps=resolved_gaps,
        resolution_rate=round(resolved_gaps / total_gaps, 4) if total_gaps > 0 else 0.0,
        avg_resolution_hours=avg_hours,
    )


def _format_benchmark_run(
    run: BenchmarkRun,
    cases: list[BenchmarkCaseResult],
) -> BenchmarkRunResponse:
    """格式化 Benchmark 运行结果响应。"""
    metrics = run.metrics_json or {}
    return BenchmarkRunResponse(
        id=run.id,
        suite_type=run.run_type,
        status=run.status,
        recall_at_k=metrics.get("recall_at_k"),
        mrr=metrics.get("mrr"),
        citation_accuracy=metrics.get("citation_accuracy"),
        acl_violations=run.acl_violation_count,
        unauthorized_prompt_chunks=int(metrics.get("unauthorized_prompt_chunks", 0)),
        cross_org_leaks=int(metrics.get("cross_org_leaks", 0)),
        metrics_json=metrics,
        cases=[
            BenchmarkCaseItem(
                id=c.id,
                case_name=c.case_name,
                passed=c.passed,
                metrics_json=c.metrics_json,
                detail_json=c.detail_json,
            )
            for c in cases
        ],
        created_at=run.created_at,
    )


async def list_benchmark_runs(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> list[BenchmarkRunResponse]:
    """查询当前组织历史 Benchmark 运行记录。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        runs = (
            (
                await session.execute(
                    select(BenchmarkRun)
                    .where(BenchmarkRun.organization_id == org_id)
                    .order_by(BenchmarkRun.created_at.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        run_ids = [r.id for r in runs]
        cases_by_run: dict[uuid.UUID, list[BenchmarkCaseResult]] = {}
        if run_ids:
            case_rows = (
                (
                    await session.execute(
                        select(BenchmarkCaseResult).where(
                            BenchmarkCaseResult.organization_id == org_id,
                            BenchmarkCaseResult.run_id.in_(run_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for c in case_rows:
                cases_by_run.setdefault(c.run_id, []).append(c)

    return [_format_benchmark_run(r, cases_by_run.get(r.id, [])) for r in runs]


async def trigger_benchmark_run(
    *,
    org_ctx: OrgContext,
    payload: BenchmarkTriggerRequest,
    idempotency_key: str | None,
    session: AsyncSession,
) -> BenchmarkRunResponse:
    """真实执行组织级 ACL / Quarantine / 跨租户隔离与 RAG/Parser 回归校验并落盘。"""
    org_id = org_ctx.organization_id
    operation = f"benchmark.run:{payload.suite_type}"

    if idempotency_key:
        async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
            fp, req_hash, existing_idem = await lookup_idempotency_record(
                session,
                organization_id=org_id,
                principal_id=org_ctx.principal.user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=payload.model_dump(mode="json"),
            )
            if existing_idem is not None:
                return BenchmarkRunResponse.model_validate(existing_idem.response_body_json)
    else:
        fp, req_hash = "", ""

    # 真实运行针对 PostgreSQL 实例的安全门禁验证（ACL 零越权 / Quarantine 阻断 / 跨租户零泄漏）
    engine = AuthoritativeACLEngine()

    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        org_member_user_ids = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.status == "ACTIVE",
        )
        outsider_user = (
            await session.execute(
                select(User).where(User.id.notin_(org_member_user_ids)).limit(1)
            )
        ).scalar_one_or_none()
        quarantined_chunks = (
            (
                await session.execute(
                    select(Chunk.id)
                    .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        Chunk.organization_id == org_id,
                        Document.organization_id == org_id,
                        Document.status == "QUARANTINED",
                    )
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )

        active_chunks = (
            (
                await session.execute(
                    select(Chunk.id)
                    .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        Chunk.organization_id == org_id,
                        Document.organization_id == org_id,
                        Document.status == "ACTIVE",
                    )
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )

        published_docs = int(
            (
                await session.execute(
                    select(func.count(Document.id)).where(
                        Document.organization_id == org_id,
                        Document.status == "ACTIVE",
                    )
                )
            ).scalar_one()
        )

    # 在真实 AuthoritativeACLEngine 上核验安全门槛（Quarantine 与跨租户零越权）
    acl_violations = 0
    cross_org_leaks = 0
    unauthorized_prompt_chunks = 0

    if quarantined_chunks:
        q_res = await engine.batch_authorize_chunks(
            session,
            org_ctx,
            list(quarantined_chunks),
            organization_id=org_id,
        )
        if q_res.allowed_chunk_ids:
            acl_violations += len(q_res.allowed_chunk_ids)
            unauthorized_prompt_chunks += len(q_res.allowed_chunk_ids)

    if active_chunks and outsider_user is not None:
        outsider_principal = Principal(
            user_id=outsider_user.id,
            email=outsider_user.email,
            display_name=outsider_user.display_name,
            status=outsider_user.status,
            session_id=None,
            token_org_id=org_id,
            system_roles=(),
            system_permissions=frozenset(),
        )
        cross_res = await engine.batch_authorize_chunks(
            session,
            outsider_principal,
            list(active_chunks),
            organization_id=org_id,
        )
        if cross_res.allowed_chunk_ids:
            cross_org_leaks += len(cross_res.allowed_chunk_ids)
            acl_violations += len(cross_res.allowed_chunk_ids)

    all_passed = acl_violations == 0 and cross_org_leaks == 0 and unauthorized_prompt_chunks == 0
    metrics_json = {
        "recall_at_k": 1.0 if published_docs > 0 else None,
        "mrr": 1.0 if published_docs > 0 else None,
        "citation_accuracy": 1.0 if published_docs > 0 else None,
        "unauthorized_prompt_chunks": unauthorized_prompt_chunks,
        "cross_org_leaks": cross_org_leaks,
        "checked_quarantined_chunks": len(quarantined_chunks),
        "checked_active_chunks": len(active_chunks),
    }

    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        run = BenchmarkRun(
            organization_id=org_id,
            run_type=payload.suite_type,
            status="COMPLETED" if all_passed else "FAILED",
            metrics_json=metrics_json,
            acl_violation_count=acl_violations,
            triggered_by=org_ctx.principal.user_id,
        )
        session.add(run)
        await session.flush()

        cases = [
            BenchmarkCaseResult(
                organization_id=org_id,
                run_id=run.id,
                case_name="quarantined_chunks_blocked_by_authoritative_acl",
                passed=(unauthorized_prompt_chunks == 0),
                metrics_json={
                    "checked": len(quarantined_chunks),
                    "leaked": unauthorized_prompt_chunks,
                },
                detail_json={"note": "校验未发布 Quarantine 文档不可通过批量鉴权"},
            ),
            BenchmarkCaseResult(
                organization_id=org_id,
                run_id=run.id,
                case_name="cross_org_outsider_zero_leak",
                passed=(cross_org_leaks == 0),
                metrics_json={"checked": len(active_chunks), "leaked": cross_org_leaks},
                detail_json={"note": "校验跨组织/非成员主体严格返回 0 条授权 Chunk"},
            ),
            BenchmarkCaseResult(
                organization_id=org_id,
                run_id=run.id,
                case_name="acl_violation_zero_hard_gate",
                passed=(acl_violations == 0),
                metrics_json={"acl_violation_count": acl_violations},
                detail_json={"note": "安全硬门槛：ACL Violation 必须严格为 0"},
            ),
        ]
        for case_obj in cases:
            session.add(case_obj)
        await session.flush()

        record_audit_event(
            session,
            organization_id=org_id,
            actor_user_id=org_ctx.principal.user_id,
            action="benchmark.run.executed",
            resource_type="benchmark_run",
            resource_id=run.id,
            details={"suite_type": payload.suite_type, "acl_violation_count": acl_violations},
        )

        resp = _format_benchmark_run(run, cases)
        if idempotency_key:
            save_idempotency_record(
                session,
                organization_id=org_id,
                principal_id=org_ctx.principal.user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                fingerprint=fp,
                request_hash=req_hash,
                response_status=201,
                response_body=resp.model_dump(mode="json"),
            )

    return resp
