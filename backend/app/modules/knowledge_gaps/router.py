"""Knowledge Gap 闭环治理、信号准入与整改任务回归验证 HTTP 路由。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session, org_rls_session
from app.modules.identity.deps import OrgContext, require_permission
from app.modules.knowledge_gaps.schemas import (
    GapSignalIngestionResult,
    GapSignalInput,
    KnowledgeGapItem,
    KnowledgeGapListResponse,
    KnowledgeGapStatusUpdateRequest,
    KnowledgeGapTaskCreateRequest,
    KnowledgeGapTaskItem,
    KnowledgeGapTaskUpdateRequest,
)
from app.modules.knowledge_gaps.service import (
    create_knowledge_gap_task,
    ingest_gap_signal,
    list_knowledge_gaps,
    update_knowledge_gap_status,
    update_knowledge_gap_task,
)

router = APIRouter(tags=["Knowledge Gaps"])
gaps_router = router


@router.get(
    "/knowledge-gaps",
    response_model=KnowledgeGapListResponse,
    summary="查询知识缺口与整改任务列表",
)
async def get_knowledge_gaps(
    space_id: uuid.UUID | None = Query(default=None),
    gap_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    org_ctx: OrgContext = Depends(require_permission("gap:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> KnowledgeGapListResponse:
    """分页查询当前组织内的 KnowledgeGap 与关联任务详情。"""
    async with org_rls_session(
        session,
        org_ctx.organization_id,
        user_id=org_ctx.principal.user_id,
    ):
        return await list_knowledge_gaps(
            session,
            organization_id=org_ctx.organization_id,
            space_id=space_id,
            status=gap_status,
            limit=limit,
            offset=offset,
        )


@router.post(
    "/knowledge-gaps/signals",
    response_model=GapSignalIngestionResult,
    status_code=status.HTTP_201_CREATED,
    summary="提交问答缺口准入分类信号",
)
async def post_gap_signal(
    payload: GapSignalInput,
    org_ctx: OrgContext = Depends(require_permission("gap:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> GapSignalIngestionResult:
    """按准入分类器处理潜在缺口信号（拦截 ACL_DENIED 与服务故障）。"""
    async with org_rls_session(
        session,
        org_ctx.organization_id,
        user_id=org_ctx.principal.user_id,
    ):
        return await ingest_gap_signal(
            session,
            organization_id=org_ctx.organization_id,
            signal=payload,
        )


@router.patch(
    "/knowledge-gaps/{gap_id}/status",
    response_model=KnowledgeGapItem,
    summary="更新知识缺口 11 态状态（禁止直接设为 RESOLVED）",
)
async def patch_knowledge_gap_status(
    gap_id: uuid.UUID,
    payload: KnowledgeGapStatusUpdateRequest,
    org_ctx: OrgContext = Depends(require_permission("gap:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> KnowledgeGapItem:
    """变更 KnowledgeGap 状态；标记 RESOLVED 必须经任务回归门禁。"""
    async with org_rls_session(
        session,
        org_ctx.organization_id,
        user_id=org_ctx.principal.user_id,
    ):
        await update_knowledge_gap_status(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            gap_id=gap_id,
            target_status=payload.status,
            reason=payload.reason,
        )
        listed = await list_knowledge_gaps(
            session,
            organization_id=org_ctx.organization_id,
            limit=200,
        )
        for item in listed.items:
            if item.id == gap_id:
                return item
        raise AssertionError("Updated KnowledgeGap missing in same RLS transaction")


@router.post(
    "/knowledge-gaps/{gap_id}/tasks",
    response_model=KnowledgeGapTaskItem,
    status_code=status.HTTP_201_CREATED,
    summary="为知识缺口指派补充整改任务",
)
async def post_knowledge_gap_task(
    gap_id: uuid.UUID,
    payload: KnowledgeGapTaskCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("gap:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> KnowledgeGapTaskItem:
    """创建 KnowledgeGapTask 并自动推进缺口状态至 ASSIGNED。"""
    async with org_rls_session(
        session,
        org_ctx.organization_id,
        user_id=org_ctx.principal.user_id,
    ):
        return await create_knowledge_gap_task(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            gap_id=gap_id,
            payload=payload,
        )


@router.patch(
    "/knowledge-gap-tasks/{task_id}",
    response_model=KnowledgeGapTaskItem,
    summary="更新整改任务状态并执行回归验证",
)
async def patch_knowledge_gap_task(
    task_id: uuid.UUID,
    payload: KnowledgeGapTaskUpdateRequest,
    org_ctx: OrgContext = Depends(require_permission("gap:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> KnowledgeGapTaskItem:
    """更新任务状态；若设为 RESOLVED 则强制执行真实文档/FAQ 回归验证门禁。"""
    async with org_rls_session(
        session,
        org_ctx.organization_id,
        user_id=org_ctx.principal.user_id,
    ):
        return await update_knowledge_gap_task(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            task_id=task_id,
            payload=payload,
        )
