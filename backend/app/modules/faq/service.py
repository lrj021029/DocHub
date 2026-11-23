"""FAQ 三轴状态机、禁自审校验、多来源最严交集 ACL 与 STALE 失活服务。

核心架构与安全红线（ADR-001 & TECHNICAL_SPEC §10）：
1. 三轴完全解耦：
   - `lifecycle_state`: `CANDIDATE` -> `PENDING_REVIEW` -> `ARCHIVED` / `REJECTED`
   - `publication_state`: `UNPUBLISHED` / `PUBLISHED` / `DISABLED`
   - `source_health_state`: `HEALTHY` / `STALE` / `EXPIRED`
2. 禁止自审：`submit_review_decision` 强制检查 `reviewer_id != faq.created_by` 与
   `reviewer_id != faq_version.created_by`，违规立即抛出 `ForbiddenError("SELF_REVIEW_FORBIDDEN")`
   并写入 `SecurityAuditEvent`。
3. 多来源最严交集 ACL（`authorize_faq_for_principal`）：
   - 必须先通过 KnowledgeSpace 入口校验；
   - 再遍历当前 `FAQVersion` 下所有 `FAQSegment` 关联的 `FAQSourceRef`，对每条来源文档调用
     `AuthoritativeACLEngine.check_document_access`；
   - **全部依赖文档均允许访问时方才放行**（任何一条来源无权即拒绝，绝不扩大来源权限）。
4. 来源过期标记（`mark_faqs_stale_for_document`）：
   - 仅将 `source_health_state` 置为 `STALE`（绝不覆写 `publication_state="PUBLISHED"`），
     同事务写 `faq.source.stale` 与 `faq.cache.invalidate` Outbox，并递增 `cache_generation`。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.ids import new_uuid7
from app.db.session import org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.audit.service import record_audit_event, record_security_audit_event
from app.modules.documents.models import Document, DocumentVersion
from app.modules.faq.models import (
    FAQ,
    FAQACL,
    FAQCacheIndex,
    FAQPublishRecord,
    FAQReview,
    FAQSegment,
    FAQSourceRef,
    FAQVersion,
)
from app.modules.faq.schemas import (
    FaqCandidateItem,
    FaqItemResponse,
    FaqReviewRequest,
    ManualFaqCreateRequest,
)
from app.modules.identity.deps import OrgContext, Principal
from app.modules.knowledge_gaps.models import FAQCandidate, QuestionCluster
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.outbox.service import enqueue_outbox_event

# 合法 Lifecycle 状态迁移矩阵
_ALLOWED_LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    "CANDIDATE": frozenset({"PENDING_REVIEW", "REJECTED", "ARCHIVED"}),
    "PENDING_REVIEW": frozenset({"ARCHIVED", "REJECTED", "CANDIDATE"}),
    "REJECTED": frozenset({"PENDING_REVIEW", "ARCHIVED"}),
    "ARCHIVED": frozenset({"PENDING_REVIEW"}),
}

_acl_engine = AuthoritativeACLEngine()


@dataclass(frozen=True, slots=True)
class FAQAuthDecision:
    """FAQ 多来源交集权威鉴权结果。"""

    allowed: bool
    reason_code: str
    faq_id: uuid.UUID
    space_id: uuid.UUID
    acl_revision: int = 1
    max_sensitivity_level: str = "L1"
    source_document_ids: tuple[uuid.UUID, ...] = ()


def _sensitivity_rank(level: str) -> int:
    """将 L0~L3 映射为有序敏感度，取全部来源的最高敏感等级。"""
    return {"L0": 0, "L1": 1, "L2": 2, "L3": 3}.get(level.upper(), 2)


def _compute_answer_hash(question: str, segments_text: list[str]) -> str:
    """计算问题与答案片段的稳定 SHA-256 摘要。"""
    joined = f"{question.strip()}\n---\n" + "\n".join(s.strip() for s in segments_text)
    return hashlib.sha256(joined.encode()).hexdigest()


async def authorize_faq_for_principal(
    session: AsyncSession,
    *,
    principal: Principal | OrgContext,
    organization_id: uuid.UUID,
    faq_id: uuid.UUID,
) -> FAQAuthDecision:
    """在 PostgreSQL 实时复核 FAQ 权限（Space 入口 + FAQACL + 全部来源文档交集 ACL）。"""
    org_ctx = principal if isinstance(principal, OrgContext) else None
    user_id = org_ctx.principal.user_id if org_ctx else principal.user_id

    async with org_rls_session(session, organization_id, user_id=user_id):
        faq = (
            await session.execute(
                select(FAQ).where(
                    FAQ.id == faq_id,
                    FAQ.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if faq is None:
            return FAQAuthDecision(
                allowed=False,
                reason_code="FAQ_NOT_FOUND",
                faq_id=faq_id,
                space_id=uuid.UUID(int=0),
            )

        # 1. 校验 Space 入口策略
        space_decision = await _acl_engine.check_space_access(
            session,
            principal,
            faq.space_id,
            organization_id=organization_id,
        )
        if not space_decision.allowed:
            return FAQAuthDecision(
                allowed=False,
                reason_code=space_decision.reason_code,
                faq_id=faq.id,
                space_id=faq.space_id,
            )

        faq_acl = (
            await session.execute(
                select(FAQACL).where(
                    FAQACL.organization_id == organization_id,
                    FAQACL.faq_id == faq.id,
                )
            )
        ).scalar_one_or_none()
        acl_rev = faq_acl.revision if faq_acl else 1

        if faq.current_version_id is None:
            return FAQAuthDecision(
                allowed=False,
                reason_code="FAQ_VERSION_MISSING",
                faq_id=faq.id,
                space_id=faq.space_id,
                acl_revision=acl_rev,
            )

        # 2. 查询当前 FAQVersion 下所有分段关联的全部来源文档
        source_rows = (
            await session.execute(
                select(
                    FAQSourceRef.document_id,
                    Document.sensitivity_level,
                )
                .join(FAQSegment, FAQSegment.id == FAQSourceRef.faq_segment_id)
                .join(Document, Document.id == FAQSourceRef.document_id)
                .where(
                    FAQSourceRef.organization_id == organization_id,
                    FAQSegment.organization_id == organization_id,
                    FAQSegment.faq_version_id == faq.current_version_id,
                    Document.organization_id == organization_id,
                )
            )
        ).all()

        # 若手工 FAQ 无结构化来源文档引用，检查是否要求至少具备 faq:read
        if not source_rows:
            if org_ctx is not None and not org_ctx.has_permission("faq:read"):
                return FAQAuthDecision(
                    allowed=False,
                    reason_code="MISSING_FAQ_READ_PERMISSION",
                    faq_id=faq.id,
                    space_id=faq.space_id,
                    acl_revision=acl_rev,
                )
            return FAQAuthDecision(
                allowed=True,
                reason_code="FAQ_MANUAL_AUTHORIZED",
                faq_id=faq.id,
                space_id=faq.space_id,
                acl_revision=acl_rev,
                max_sensitivity_level="L1",
            )

        # 3. 多来源最严交集：遍历每篇来源文档调用权威 AuthoritativeACLEngine
        unique_doc_ids: list[uuid.UUID] = []
        max_sens = "L0"
        seen_docs: set[uuid.UUID] = set()
        for doc_id, sens_level in source_rows:
            if _sensitivity_rank(sens_level) > _sensitivity_rank(max_sens):
                max_sens = sens_level
            if doc_id not in seen_docs:
                seen_docs.add(doc_id)
                unique_doc_ids.append(doc_id)

        for doc_id in unique_doc_ids:
            doc_decision = await _acl_engine.check_document_access(
                session,
                principal,
                doc_id,
                organization_id=organization_id,
            )
            if not doc_decision.allowed:
                record_security_audit_event(
                    session,
                    organization_id=organization_id,
                    actor_user_id=user_id,
                    event_type="faq.source_intersection.denied",
                    severity="LOW",
                    resource_type="faq",
                    resource_id=faq.id,
                    details={
                        "denied_reason": doc_decision.reason_code,
                        "source_count": len(unique_doc_ids),
                    },
                )
                return FAQAuthDecision(
                    allowed=False,
                    reason_code=f"FAQ_SOURCE_ACL_DENIED:{doc_decision.reason_code}",
                    faq_id=faq.id,
                    space_id=faq.space_id,
                    acl_revision=acl_rev,
                    max_sensitivity_level=max_sens,
                    source_document_ids=tuple(unique_doc_ids),
                )

    return FAQAuthDecision(
        allowed=True,
        reason_code="FAQ_SOURCE_INTERSECTION_GRANTED",
        faq_id=faq.id,
        space_id=faq.space_id,
        acl_revision=acl_rev,
        max_sensitivity_level=max_sens,
        source_document_ids=tuple(unique_doc_ids),
    )


async def list_faq_candidates(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> list[FaqCandidateItem]:
    """查询当前组织内的 FAQ 挖掘候选队列。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        rows = (
            await session.execute(
                select(FAQCandidate, QuestionCluster, FAQ.lifecycle_state, FAQ.created_by)
                .join(QuestionCluster, QuestionCluster.id == FAQCandidate.cluster_id)
                .outerjoin(FAQ, FAQ.id == FAQCandidate.faq_id)
                .where(FAQCandidate.organization_id == org_id)
                .order_by(FAQCandidate.created_at.desc())
            )
        ).all()

    items: list[FaqCandidateItem] = []
    for cand, cluster, faq_life, faq_creator in rows:
        ans_json = cand.generated_answer_json or {}
        conf_json = cand.confidence_json or {}
        metrics_json = cluster.metrics_json or {}
        citations = ans_json.get("citations") or []

        life_state = faq_life or (
            "PENDING_REVIEW" if cand.candidate_status == "SUBMITTED" else "CANDIDATE"
        )
        items.append(
            FaqCandidateItem(
                id=cand.id,
                faq_id=cand.faq_id,
                cluster_id=cand.cluster_id,
                space_id=cand.space_id,
                question=cand.generated_question,
                answer_draft=str(ans_json.get("answer_draft") or ""),
                frequency_count=int(
                    conf_json.get("effective_frequency")
                    or metrics_json.get("effective_frequency")
                    or 0
                ),
                unique_user_count=int(
                    conf_json.get("unique_user_count") or metrics_json.get("unique_user_count") or 0
                ),
                confidence_score=float(conf_json.get("overall_score") or 0.0),
                lifecycle_state=life_state,  # type: ignore[arg-type]
                candidate_status=cand.candidate_status,
                source_citation_count=len(citations) if isinstance(citations, list) else 0,
                created_by=faq_creator,
                created_at=cand.created_at,
            )
        )
    return items


async def submit_candidate_for_review(
    *,
    org_ctx: OrgContext,
    candidate_id: uuid.UUID,
    session: AsyncSession,
) -> FaqItemResponse:
    """将挖掘候选转化为待审核 FAQ + FAQVersion + FAQSegment + FAQSourceRef。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        cand = (
            await session.execute(
                select(FAQCandidate)
                .where(
                    FAQCandidate.id == candidate_id,
                    FAQCandidate.organization_id == org_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if cand is None:
            raise NotFoundError(
                code="FAQ_CANDIDATE_NOT_FOUND",
                message="目标 FAQ 候选不存在或无权访问",
            )

        if cand.faq_id is not None:
            faq_existing = await session.get(FAQ, cand.faq_id)
            if faq_existing is not None:
                return await _build_faq_item_response_in_tx(
                    session,
                    organization_id=org_id,
                    faq=faq_existing,
                )

        if cand.space_id is None:
            raise ValidationError(code="CANDIDATE_MISSING_SPACE", message="候选缺少所属知识空间 ID")

        ans_json = cand.generated_answer_json or {}
        answer_text = str(ans_json.get("answer_draft") or "").strip()
        citations: list[dict[str, Any]] = list(ans_json.get("citations") or [])
        if not citations:
            raise ConflictError(
                code="FAQ_CITATION_REQUIRED",
                message="候选答案缺少已授权来源文档引用，禁止提交进入审核发布队列",
            )

        faq = FAQ(
            id=new_uuid7(),
            organization_id=org_id,
            space_id=cand.space_id,
            lifecycle_state="PENDING_REVIEW",
            publication_state="UNPUBLISHED",
            source_health_state="HEALTHY",
            is_manual=False,
            created_by=org_ctx.principal.user_id,
            maintainer_id=org_ctx.principal.user_id,
        )
        session.add(faq)
        await session.flush()

        ver = FAQVersion(
            id=new_uuid7(),
            organization_id=org_id,
            faq_id=faq.id,
            version_no=1,
            question=cand.generated_question,
            answer_hash=_compute_answer_hash(cand.generated_question, [answer_text]),
            fact_basis="HDBSCAN 聚类挖掘 + 空间内授权文档引用生成",
            created_by=org_ctx.principal.user_id,
        )
        session.add(ver)
        await session.flush()

        seg = FAQSegment(
            id=new_uuid7(),
            organization_id=org_id,
            faq_version_id=ver.id,
            segment_no=0,
            content_type="MARKDOWN",
            content_text=answer_text,
            metadata_json={"cluster_id": str(cand.cluster_id)},
        )
        session.add(seg)
        await session.flush()

        for cite in citations:
            doc_id = uuid.UUID(str(cite["document_id"]))
            doc_ver_id = uuid.UUID(str(cite["document_version_id"]))
            chunk_id = uuid.UUID(str(cite["chunk_id"])) if cite.get("chunk_id") else None
            session.add(
                FAQSourceRef(
                    organization_id=org_id,
                    faq_segment_id=seg.id,
                    document_id=doc_id,
                    document_version_id=doc_ver_id,
                    chunk_id=chunk_id,
                    citation_ref=str(cite.get("document_title") or ""),
                )
            )

        faq.current_version_id = ver.id
        session.add(
            FAQACL(
                organization_id=org_id,
                faq_id=faq.id,
                revision=1,
                policy_snapshot={"mode": "STRICT_SOURCE_INTERSECTION"},
            )
        )
        cand.faq_id = faq.id
        cand.candidate_status = "SUBMITTED"

        record_audit_event(
            session,
            organization_id=org_id,
            actor_user_id=org_ctx.principal.user_id,
            action="faq.candidate.submitted_for_review",
            resource_type="faq",
            resource_id=faq.id,
            details={"candidate_id": str(cand.id)},
        )

        return await _build_faq_item_response_in_tx(
            session,
            organization_id=org_id,
            faq=faq,
        )


async def create_manual_faq(
    *,
    org_ctx: OrgContext,
    payload: ManualFaqCreateRequest,
    session: AsyncSession,
) -> FaqItemResponse:
    """手工创建 MANUAL FAQ（强制校验 fact_basis、有效期与维护人）。"""
    org_id = org_ctx.organization_id
    basis = (payload.evidence or payload.fact_basis or "").strip()
    if not basis:
        raise ValidationError(
            code="FACT_BASIS_REQUIRED",
            message="手工创建 FAQ 必须填写事实依据说明",
        )

    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        space_id = payload.space_id
        if space_id is None:
            first_space = (
                await session.execute(
                    select(KnowledgeSpace.id)
                    .where(
                        KnowledgeSpace.organization_id == org_id,
                        KnowledgeSpace.status == "ACTIVE",
                    )
                    .order_by(KnowledgeSpace.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if first_space is None:
                raise ValidationError(code="NO_ACTIVE_SPACE", message="当前组织暂无可用的知识空间")
            space_id = first_space
        else:
            sp = (
                await session.execute(
                    select(KnowledgeSpace.id).where(
                        KnowledgeSpace.id == space_id,
                        KnowledgeSpace.organization_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if sp is None:
                raise NotFoundError(code="SPACE_NOT_FOUND", message="目标知识空间不存在")

        faq = FAQ(
            id=new_uuid7(),
            organization_id=org_id,
            space_id=space_id,
            lifecycle_state="PENDING_REVIEW",
            publication_state="UNPUBLISHED",
            source_health_state="HEALTHY",
            is_manual=True,
            created_by=org_ctx.principal.user_id,
            maintainer_id=org_ctx.principal.user_id,
            expires_at=payload.expires_at,
        )
        session.add(faq)
        await session.flush()

        ver = FAQVersion(
            id=new_uuid7(),
            organization_id=org_id,
            faq_id=faq.id,
            version_no=1,
            question=payload.question.strip(),
            answer_hash=_compute_answer_hash(payload.question, [payload.answer]),
            fact_basis=f"{basis} | 维护责任人: {payload.maintainer.strip()}",
            created_by=org_ctx.principal.user_id,
        )
        session.add(ver)
        await session.flush()

        seg = FAQSegment(
            id=new_uuid7(),
            organization_id=org_id,
            faq_version_id=ver.id,
            segment_no=0,
            content_type="MARKDOWN",
            content_text=payload.answer.strip(),
            metadata_json={"maintainer": payload.maintainer.strip()},
        )
        session.add(seg)
        await session.flush()

        for ref in payload.source_refs:
            doc_ok = (
                await session.execute(
                    select(Document.id).where(
                        Document.id == ref.document_id,
                        Document.organization_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            ver_ok = (
                await session.execute(
                    select(DocumentVersion.id).where(
                        DocumentVersion.id == ref.document_version_id,
                        DocumentVersion.document_id == ref.document_id,
                        DocumentVersion.organization_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if doc_ok is None or ver_ok is None:
                raise ValidationError(
                    code="INVALID_FAQ_SOURCE_REF",
                    message="关联来源文档或版本不存在于当前组织",
                )
            session.add(
                FAQSourceRef(
                    organization_id=org_id,
                    faq_segment_id=seg.id,
                    document_id=ref.document_id,
                    document_version_id=ref.document_version_id,
                    chunk_id=ref.chunk_id,
                    citation_ref=ref.citation_ref,
                )
            )

        faq.current_version_id = ver.id
        session.add(
            FAQACL(
                organization_id=org_id,
                faq_id=faq.id,
                revision=1,
                policy_snapshot={"mode": "STRICT_SOURCE_INTERSECTION"},
            )
        )

        record_audit_event(
            session,
            organization_id=org_id,
            actor_user_id=org_ctx.principal.user_id,
            action="faq.manual.created",
            resource_type="faq",
            resource_id=faq.id,
            details={"space_id": str(space_id), "maintainer": payload.maintainer.strip()},
        )

        return await _build_faq_item_response_in_tx(
            session,
            organization_id=org_id,
            faq=faq,
        )


def _assert_lifecycle_transition(current_state: str, target_state: str) -> None:
    """校验 FAQ Lifecycle 状态迁移合法性。"""
    if current_state == target_state:
        return
    allowed = _ALLOWED_LIFECYCLE_TRANSITIONS.get(current_state, frozenset())
    if target_state not in allowed:
        raise ConflictError(
            code="INVALID_FAQ_LIFECYCLE_TRANSITION",
            message=f"不允许的 FAQ 生命周期迁移: {current_state} -> {target_state}",
        )


async def submit_review_decision(
    *,
    org_ctx: OrgContext,
    faq_id: uuid.UUID,
    payload: FaqReviewRequest,
    session: AsyncSession,
) -> FaqItemResponse:
    """提交 FAQ 审核结论（严格执行“创建者严禁审核自己创建的 FAQ”硬规则）。"""
    org_id = org_ctx.organization_id
    reviewer_id = org_ctx.principal.user_id
    normalized_decision = (
        "APPROVED" if payload.decision.upper() in {"APPROVE", "APPROVED"} else "REJECTED"
    )

    # 先进行自审检测：若违规立即记录安全审计并抛出 403
    self_review_violation = False
    async with org_rls_session(session, org_id, user_id=reviewer_id):
        faq = (
            await session.execute(
                select(FAQ).where(FAQ.id == faq_id, FAQ.organization_id == org_id).with_for_update()
            )
        ).scalar_one_or_none()
        if faq is None:
            raise NotFoundError(code="FAQ_NOT_FOUND", message="目标 FAQ 不存在或无权访问")

        if faq.current_version_id is None:
            raise ConflictError(code="FAQ_VERSION_MISSING", message="FAQ 缺少待审核版本")

        ver = await session.get(FAQVersion, faq.current_version_id)
        if ver is None:
            raise ConflictError(code="FAQ_VERSION_MISSING", message="FAQ 缺少待审核版本")

        if faq.created_by == reviewer_id or ver.created_by == reviewer_id:
            self_review_violation = True
            record_security_audit_event(
                session,
                organization_id=org_id,
                actor_user_id=reviewer_id,
                event_type="faq.review.self_review_blocked",
                severity="HIGH",
                resource_type="faq",
                resource_id=faq.id,
                details={"faq_version_id": str(ver.id)},
            )
        else:
            target_life = "ARCHIVED" if normalized_decision == "APPROVED" else "REJECTED"
            _assert_lifecycle_transition(faq.lifecycle_state, target_life)
            faq.lifecycle_state = target_life

            session.add(
                FAQReview(
                    organization_id=org_id,
                    faq_version_id=ver.id,
                    faq_id=faq.id,
                    reviewer_id=reviewer_id,
                    decision=normalized_decision,
                    review_type="CONTENT_AND_ACL",
                    comments=payload.comments.strip() or None,
                    policy_version="v1",
                )
            )
            record_audit_event(
                session,
                organization_id=org_id,
                actor_user_id=reviewer_id,
                action="faq.reviewed",
                resource_type="faq",
                resource_id=faq.id,
                details={"decision": normalized_decision, "faq_version_id": str(ver.id)},
            )
            result = await _build_faq_item_response_in_tx(
                session,
                organization_id=org_id,
                faq=faq,
            )

    if self_review_violation:
        raise ForbiddenError(
            code="SELF_REVIEW_FORBIDDEN",
            message="安全合规约束：创建者严禁审核自己提交或创建的 FAQ",
        )
    return result


async def publish_faq(
    *,
    org_ctx: OrgContext,
    faq_id: uuid.UUID,
    session: AsyncSession,
) -> FaqItemResponse:
    """发布已通过审核的 FAQVersion（独立更新 Publication 轴为 PUBLISHED，写入 Outbox）。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        faq = (
            await session.execute(
                select(FAQ).where(FAQ.id == faq_id, FAQ.organization_id == org_id).with_for_update()
            )
        ).scalar_one_or_none()
        if faq is None:
            raise NotFoundError(code="FAQ_NOT_FOUND", message="目标 FAQ 不存在或无权访问")

        if faq.current_version_id is None:
            raise ConflictError(code="FAQ_VERSION_MISSING", message="FAQ 缺少可发布版本")

        approved_review = (
            await session.execute(
                select(FAQReview.id).where(
                    FAQReview.organization_id == org_id,
                    FAQReview.faq_id == faq.id,
                    FAQReview.faq_version_id == faq.current_version_id,
                    FAQReview.decision == "APPROVED",
                )
            )
        ).scalar_one_or_none()
        if approved_review is None:
            raise ConflictError(
                code="FAQ_REVIEW_REQUIRED_BEFORE_PUBLISH",
                message="必须先由非创建者完成审核通过（APPROVED），方可发布 FAQ",
            )

        ver = await session.get(FAQVersion, faq.current_version_id)
        assert ver is not None

        faq.publication_state = "PUBLISHED"
        session.add(
            FAQPublishRecord(
                organization_id=org_id,
                faq_id=faq.id,
                faq_version_id=ver.id,
                published_by=org_ctx.principal.user_id,
                status="PUBLISHED",
            )
        )

        # 维护或递增缓存版本映射表
        cache_idx = (
            await session.execute(
                select(FAQCacheIndex).where(
                    FAQCacheIndex.organization_id == org_id,
                    FAQCacheIndex.faq_id == faq.id,
                )
            )
        ).scalar_one_or_none()
        faq_acl = (
            await session.execute(
                select(FAQACL).where(
                    FAQACL.organization_id == org_id,
                    FAQACL.faq_id == faq.id,
                )
            )
        ).scalar_one_or_none()
        acl_rev = faq_acl.revision if faq_acl else 1

        if cache_idx is None:
            cache_idx = FAQCacheIndex(
                organization_id=org_id,
                faq_id=faq.id,
                faq_version_id=ver.id,
                acl_revision=acl_rev,
                source_version_hash=ver.answer_hash,
                cache_generation=1,
            )
            session.add(cache_idx)
        else:
            cache_idx.faq_version_id = ver.id
            cache_idx.acl_revision = acl_rev
            cache_idx.source_version_hash = ver.answer_hash
            cache_idx.cache_generation += 1

        enqueue_outbox_event(
            session,
            organization_id=org_id,
            aggregate_type="faq",
            aggregate_id=faq.id,
            aggregate_revision=ver.version_no,
            event_type="faq.version.published",
            payload={
                "faq_id": str(faq.id),
                "faq_version_id": str(ver.id),
                "space_id": str(faq.space_id),
                "cache_generation": cache_idx.cache_generation,
            },
        )
        record_audit_event(
            session,
            organization_id=org_id,
            actor_user_id=org_ctx.principal.user_id,
            action="faq.published",
            resource_type="faq",
            resource_id=faq.id,
            details={"faq_version_id": str(ver.id)},
        )
        resp = await _build_faq_item_response_in_tx(
            session,
            organization_id=org_id,
            faq=faq,
        )

    from app.modules.faq.cache import invalidate_faq_cache

    await invalidate_faq_cache(organization_id=org_id, space_id=faq.space_id, faq_id=faq.id)
    return resp


async def disable_faq(
    *,
    org_ctx: OrgContext,
    faq_id: uuid.UUID,
    session: AsyncSession,
) -> FaqItemResponse:
    """停用已发布 FAQ（Publication -> DISABLED）并驱逐 Redis Fast Path 缓存。"""
    org_id = org_ctx.organization_id
    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        faq = (
            await session.execute(
                select(FAQ).where(FAQ.id == faq_id, FAQ.organization_id == org_id).with_for_update()
            )
        ).scalar_one_or_none()
        if faq is None:
            raise NotFoundError(code="FAQ_NOT_FOUND", message="目标 FAQ 不存在或无权访问")

        faq.publication_state = "DISABLED"
        cache_idx = (
            await session.execute(
                select(FAQCacheIndex).where(
                    FAQCacheIndex.organization_id == org_id,
                    FAQCacheIndex.faq_id == faq.id,
                )
            )
        ).scalar_one_or_none()
        if cache_idx is not None:
            cache_idx.cache_generation += 1

        enqueue_outbox_event(
            session,
            organization_id=org_id,
            aggregate_type="faq",
            aggregate_id=faq.id,
            aggregate_revision=cache_idx.cache_generation if cache_idx else 1,
            event_type="faq.cache.invalidate",
            payload={
                "faq_id": str(faq.id),
                "space_id": str(faq.space_id),
                "reason": "DISABLED",
            },
        )
        record_audit_event(
            session,
            organization_id=org_id,
            actor_user_id=org_ctx.principal.user_id,
            action="faq.disabled",
            resource_type="faq",
            resource_id=faq.id,
            details={},
        )
        resp = await _build_faq_item_response_in_tx(
            session,
            organization_id=org_id,
            faq=faq,
        )

    from app.modules.faq.cache import invalidate_faq_cache

    await invalidate_faq_cache(organization_id=org_id, space_id=faq.space_id, faq_id=faq.id)
    return resp


async def mark_faqs_stale_for_document(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    reason: str = "SOURCE_DOCUMENT_OR_ACL_CHANGED",
) -> list[uuid.UUID]:
    """当源文档版本更新或 ACL 变更时，将引用该文档的 FAQ 标为 STALE 并驱逐缓存。

    严格遵循 ADR-001：绝不覆盖 `publication_state="PUBLISHED"`，仅将 `source_health_state`
    设为 `STALE`，并同事务写入 `faq.source.stale` 与 `faq.cache.invalidate` Outbox 事件。
    """
    affected_faqs: list[tuple[uuid.UUID, uuid.UUID]] = []

    async with org_rls_session(session, organization_id):
        faq_ids = list(
            set(
                (
                    await session.execute(
                        select(FAQ.id)
                        .join(FAQVersion, FAQVersion.faq_id == FAQ.id)
                        .join(FAQSegment, FAQSegment.faq_version_id == FAQVersion.id)
                        .join(FAQSourceRef, FAQSourceRef.faq_segment_id == FAQSegment.id)
                        .where(
                            FAQ.organization_id == organization_id,
                            FAQVersion.organization_id == organization_id,
                            FAQSegment.organization_id == organization_id,
                            FAQSourceRef.organization_id == organization_id,
                            FAQSourceRef.document_id == document_id,
                            FAQ.current_version_id == FAQVersion.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        )

        for fid in faq_ids:
            faq = (
                await session.execute(
                    select(FAQ)
                    .where(FAQ.id == fid, FAQ.organization_id == organization_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if faq is None:
                continue

            # 严禁覆盖 publication_state，仅独立修改 source_health_state 为 STALE
            faq.source_health_state = "STALE"
            cache_idx = (
                await session.execute(
                    select(FAQCacheIndex).where(
                        FAQCacheIndex.organization_id == organization_id,
                        FAQCacheIndex.faq_id == faq.id,
                    )
                )
            ).scalar_one_or_none()
            new_gen = 1
            if cache_idx is not None:
                cache_idx.cache_generation += 1
                new_gen = cache_idx.cache_generation

            enqueue_outbox_event(
                session,
                organization_id=organization_id,
                aggregate_type="faq",
                aggregate_id=faq.id,
                aggregate_revision=new_gen,
                event_type="faq.source.stale",
                payload={
                    "faq_id": str(faq.id),
                    "space_id": str(faq.space_id),
                    "document_id": str(document_id),
                    "reason": reason,
                },
            )
            enqueue_outbox_event(
                session,
                organization_id=organization_id,
                aggregate_type="faq",
                aggregate_id=faq.id,
                aggregate_revision=new_gen,
                event_type="faq.cache.invalidate",
                payload={
                    "faq_id": str(faq.id),
                    "space_id": str(faq.space_id),
                    "cache_generation": new_gen,
                    "reason": reason,
                },
            )
            affected_faqs.append((faq.space_id, faq.id))

    if affected_faqs:
        from app.modules.faq.cache import invalidate_faq_cache

        for space_id, fid in affected_faqs:
            await invalidate_faq_cache(
                organization_id=organization_id,
                space_id=space_id,
                faq_id=fid,
            )

    return [fid for _, fid in affected_faqs]


async def _build_faq_item_response_in_tx(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    faq: FAQ,
) -> FaqItemResponse:
    """在活跃 RLS 事务内组装 FAQ 三轴台账条目。"""
    ver = await session.get(FAQVersion, faq.current_version_id) if faq.current_version_id else None
    segs = (
        (
            await session.execute(
                select(FAQSegment)
                .where(
                    FAQSegment.organization_id == organization_id,
                    FAQSegment.faq_version_id == ver.id,
                )
                .order_by(FAQSegment.segment_no.asc())
            )
        )
        .scalars()
        .all()
        if ver is not None
        else []
    )
    answer_summary = "\n".join(s.content_text for s in segs)[:500]
    maintainer_name: str | None = None
    if segs and isinstance(segs[0].metadata_json, dict):
        maintainer_name = segs[0].metadata_json.get("maintainer")

    faq_acl = (
        await session.execute(
            select(FAQACL).where(
                FAQACL.organization_id == organization_id,
                FAQACL.faq_id == faq.id,
            )
        )
    ).scalar_one_or_none()
    cache_idx = (
        await session.execute(
            select(FAQCacheIndex).where(
                FAQCacheIndex.organization_id == organization_id,
                FAQCacheIndex.faq_id == faq.id,
            )
        )
    ).scalar_one_or_none()

    cache_status = (
        "INVALIDATED"
        if faq.source_health_state != "HEALTHY"
        else ("SYNCED" if cache_idx and faq.publication_state == "PUBLISHED" else "BYPASSED")
    )
    return FaqItemResponse(
        id=faq.id,
        organization_id=faq.organization_id,
        space_id=faq.space_id,
        question=ver.question if ver else "未命名 FAQ",
        answer_summary=answer_summary,
        version_no=ver.version_no if ver else 1,
        lifecycle_state=faq.lifecycle_state,  # type: ignore[arg-type]
        publication_state=faq.publication_state,  # type: ignore[arg-type]
        source_health_state=faq.source_health_state,  # type: ignore[arg-type]
        is_manual=faq.is_manual,
        maintainer=maintainer_name,
        expires_at=faq.expires_at,
        cache_status=cache_status,
        cache_hit_count=faq.hit_count,
        acl_revision=faq_acl.revision if faq_acl else 1,
        created_by=faq.created_by,
        created_at=faq.created_at,
    )


async def list_faqs(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> list[FaqItemResponse]:
    """查询当前组织 FAQ 台账（管理员见全部，普通成员仅见通过交集 ACL 的已发布 FAQ）。"""
    org_id = org_ctx.organization_id
    can_manage = org_ctx.has_permission("faq:manage")

    async with org_rls_session(session, org_id, user_id=org_ctx.principal.user_id):
        stmt = select(FAQ).where(FAQ.organization_id == org_id).order_by(FAQ.created_at.desc())
        if not can_manage:
            stmt = stmt.where(FAQ.publication_state == "PUBLISHED")
        faqs = list((await session.execute(stmt)).scalars().all())

        items: list[FaqItemResponse] = []
        for faq in faqs:
            if not can_manage:
                auth_dec = await authorize_faq_for_principal(
                    session,
                    principal=org_ctx,
                    organization_id=org_id,
                    faq_id=faq.id,
                )
                if not auth_dec.allowed:
                    continue
            items.append(
                await _build_faq_item_response_in_tx(
                    session,
                    organization_id=org_id,
                    faq=faq,
                )
            )
    return items
