"""FAQ 挖掘、审核、三轴发布停用与 Fast Path HTTP 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.faq.cache import match_faq_fast_path
from app.modules.faq.mining import run_hdbscan_recluster
from app.modules.faq.schemas import (
    FaqCandidateItem,
    FaqFastPathMatchRequest,
    FaqFastPathMatchResponse,
    FaqItemResponse,
    FaqMiningRunRequest,
    FaqMiningRunResponse,
    FaqReviewRequest,
    ManualFaqCreateRequest,
)
from app.modules.faq.service import (
    create_manual_faq,
    disable_faq,
    list_faq_candidates,
    list_faqs,
    publish_faq,
    submit_candidate_for_review,
    submit_review_decision,
)
from app.modules.identity.deps import OrgContext, require_permission

router = APIRouter(tags=["FAQ & Mining"])
faq_router = router


@router.post(
    "/faq-mining/runs",
    response_model=FaqMiningRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="手动触发 HDBSCAN FAQ 问题聚类挖掘",
)
async def post_faq_mining_run(
    payload: FaqMiningRunRequest = FaqMiningRunRequest(),
    org_ctx: OrgContext = Depends(require_permission("faq:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqMiningRunResponse:
    """按 Space 隔离执行 PII 脱敏与 HDBSCAN 批量重聚类，生成 FAQ 候选。"""
    stats = await run_hdbscan_recluster(
        session,
        organization_id=org_ctx.organization_id,
        space_id=payload.space_id,
        window_days=payload.window_days,
        min_cluster_size=payload.min_cluster_size,
    )
    return FaqMiningRunResponse(
        run_id=stats.run_id,
        status="COMPLETED",
        processed_events=stats.processed_events,
        clusters_created=stats.clusters_created,
        candidates_created=stats.candidates_created,
    )


@router.get(
    "/faq-candidates",
    response_model=list[FaqCandidateItem],
    summary="查询 FAQ 挖掘候选列表",
)
async def get_faq_candidates(
    org_ctx: OrgContext = Depends(require_permission("faq:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> list[FaqCandidateItem]:
    """查询当前组织内的 FAQ 候选问题与聚类置信度统计。"""
    return await list_faq_candidates(org_ctx=org_ctx, session=session)


@router.post(
    "/faq-candidates/{candidate_id}/submit-review",
    response_model=FaqItemResponse,
    summary="将 FAQ 候选提交进入人工审核队列",
)
async def post_submit_candidate_review(
    candidate_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("faq:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqItemResponse:
    """将候选问题与来源引用固化为待审核 FAQ 版本。"""
    return await submit_candidate_for_review(
        org_ctx=org_ctx,
        candidate_id=candidate_id,
        session=session,
    )


@router.post(
    "/faq/{faq_id}/reviews",
    response_model=FaqItemResponse,
    summary="提交 FAQ 审核决策（禁止创建者自审）",
)
async def post_faq_review(
    faq_id: uuid.UUID,
    payload: FaqReviewRequest,
    org_ctx: OrgContext = Depends(require_permission("faq:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqItemResponse:
    """审核 FAQ 版本；若审核人与创建人相同将返回 403 并记安全审计。"""
    return await submit_review_decision(
        org_ctx=org_ctx,
        faq_id=faq_id,
        payload=payload,
        session=session,
    )


@router.post(
    "/faq/{faq_id}/publish",
    response_model=FaqItemResponse,
    summary="发布已审核通过的 FAQ 版本",
)
async def post_publish_faq(
    faq_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("faq:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqItemResponse:
    """激活 FAQ 发布态（PUBLISHED）并写入 Outbox 事件。"""
    return await publish_faq(org_ctx=org_ctx, faq_id=faq_id, session=session)


@router.post(
    "/faq/{faq_id}/disable",
    response_model=FaqItemResponse,
    summary="停用已发布 FAQ 并驱逐缓存",
)
async def post_disable_faq(
    faq_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("faq:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqItemResponse:
    """停用 FAQ 并同步清理 Redis Fast Path 缓存。"""
    return await disable_faq(org_ctx=org_ctx, faq_id=faq_id, session=session)


@router.get(
    "/faq",
    response_model=list[FaqItemResponse],
    summary="查询 FAQ 三轴状态台账",
)
async def get_faq_list(
    org_ctx: OrgContext = Depends(require_permission("faq:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[FaqItemResponse]:
    """查询当前组织 FAQ 台账（普通用户仅见通过多来源交集 ACL 的已发布 FAQ）。"""
    return await list_faqs(org_ctx=org_ctx, session=session)


@router.post(
    "/faq",
    response_model=FaqItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="手工创建 MANUAL FAQ",
)
async def post_manual_faq(
    payload: ManualFaqCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("faq:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqItemResponse:
    """手工创建 FAQ（需必填事实依据、有效期与维护人，随后仍需非创建者审核）。"""
    return await create_manual_faq(
        org_ctx=org_ctx,
        payload=payload,
        session=session,
    )


@router.post(
    "/faq/match",
    response_model=FaqFastPathMatchResponse,
    summary="执行 FAQ Fast Path 匹配与实时权限复核",
)
async def post_faq_fast_path_match(
    payload: FaqFastPathMatchRequest,
    org_ctx: OrgContext = Depends(require_permission("faq:read")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqFastPathMatchResponse:
    """校验健康度、多来源交集 ACL 与 L0/L1 Redis 缓存，未命中则指示回落完整 RAG。"""
    return await match_faq_fast_path(
        session,
        principal=org_ctx,
        organization_id=org_ctx.organization_id,
        space_ids=payload.space_ids,
        question=payload.question,
        min_score=payload.min_score,
    )
