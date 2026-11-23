"""Knowledge Space HTTP 路由（`/organizations/{org_id}/spaces` 等）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.identity.deps import OrgContext, require_permission
from app.modules.knowledge_spaces.schemas import (
    KnowledgeSpaceCreateRequest,
    KnowledgeSpaceItem,
    KnowledgeSpaceUpdateRequest,
    SpaceMemberItem,
    SpaceMembersPatchRequest,
)
from app.modules.knowledge_spaces.service import (
    create_space,
    list_space_members,
    list_spaces,
    patch_space_members,
    update_space,
)

org_spaces_router = APIRouter(
    prefix="/organizations/{org_id}/spaces",
    tags=["Knowledge Spaces"],
)
spaces_router = APIRouter(
    prefix="/spaces",
    tags=["Knowledge Spaces"],
)


@org_spaces_router.get(
    "",
    response_model=list[KnowledgeSpaceItem],
    summary="查询当前组织内可见的知识空间列表",
)
async def get_org_spaces(
    org_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("space:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[KnowledgeSpaceItem]:
    """返回当前组织内用户有权进入的知识空间列表。"""
    _ = org_id
    return await list_spaces(org_ctx=org_ctx, session=session)


@org_spaces_router.post(
    "",
    response_model=KnowledgeSpaceItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识空间",
)
async def post_org_space(
    org_id: uuid.UUID,
    payload: KnowledgeSpaceCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("space:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> KnowledgeSpaceItem:
    """在当前组织创建公开或私有知识空间。"""
    _ = org_id
    return await create_space(org_ctx=org_ctx, payload=payload, session=session)


@org_spaces_router.patch(
    "/{space_id}",
    response_model=KnowledgeSpaceItem,
    summary="更新知识空间配置与可见性策略",
)
async def patch_org_space(
    org_id: uuid.UUID,
    space_id: uuid.UUID,
    payload: KnowledgeSpaceUpdateRequest,
    org_ctx: OrgContext = Depends(require_permission("space:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> KnowledgeSpaceItem:
    """修改空间配置并发布 `space.policy.changed` Outbox 事件。"""
    _ = org_id
    return await update_space(
        org_ctx=org_ctx,
        space_id=space_id,
        payload=payload,
        session=session,
    )


@spaces_router.get(
    "/{space_id}/members",
    response_model=list[SpaceMemberItem],
    summary="查询私有知识空间显式成员列表",
)
async def get_space_members(
    space_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("space:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> list[SpaceMemberItem]:
    """返回私有空间的显式成员及生效/到期时间窗口。"""
    return await list_space_members(org_ctx=org_ctx, space_id=space_id, session=session)


@spaces_router.patch(
    "/{space_id}/members",
    response_model=list[SpaceMemberItem],
    summary="批量维护私有知识空间显式成员",
)
async def update_space_members(
    space_id: uuid.UUID,
    payload: SpaceMembersPatchRequest,
    org_ctx: OrgContext = Depends(require_permission("space:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> list[SpaceMemberItem]:
    """批量增删改私有空间成员并发布策略变更事件。"""
    return await patch_space_members(
        org_ctx=org_ctx,
        space_id=space_id,
        payload=payload,
        session=session,
    )
