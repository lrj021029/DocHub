"""企业组织与成员管理 HTTP 路由（`/api/v1/organizations/*`）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.pagination import OffsetPage
from app.db.session import get_db_session
from app.modules.identity.deps import (
    OrgContext,
    Principal,
    get_current_principal,
    require_permission,
    require_system_permission,
)
from app.modules.identity.schemas import OrganizationBrief
from app.modules.identity.service import list_user_organizations
from app.modules.organizations.schemas import (
    OrganizationCreateRequest,
    OrganizationMemberCreateRequest,
    OrganizationMemberItem,
    OrganizationMemberUpdateRequest,
    OrganizationResponse,
    OrganizationSwitchResponse,
)
from app.modules.organizations.service import (
    add_organization_member,
    create_organization,
    list_organization_members,
    switch_active_organization,
    update_organization_member,
)

router = APIRouter(prefix="/organizations", tags=["Organizations"])


@router.get(
    "",
    response_model=list[OrganizationBrief],
    summary="列举当前用户所属有效组织",
)
async def get_my_organizations(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_db_session),
) -> list[OrganizationBrief]:
    """仅返回当前登录用户本人有效加入的活跃组织列表。"""
    return await list_user_organizations(session, user_id=principal.user_id)


@router.post(
    "",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建企业组织（需系统管理员权限）",
)
async def create_org(
    payload: OrganizationCreateRequest,
    principal: Principal = Depends(require_system_permission("system:org:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationResponse:
    """创建新企业组织并初始化默认组织角色。"""
    return await create_organization(
        payload=payload,
        principal=principal,
        session=session,
    )


@router.post(
    "/{org_id}/switch",
    response_model=OrganizationSwitchResponse,
    summary="切换当前活跃组织上下文",
)
async def switch_org(
    org_id: uuid.UUID,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> OrganizationSwitchResponse:
    """校验成员有效性后切换至目标组织，返回最新权限视图与新 Access JWT。"""
    return await switch_active_organization(
        target_org_id=org_id,
        principal=principal,
        session=session,
        settings=settings,
    )


@router.get(
    "/{org_id}/members",
    response_model=OffsetPage[OrganizationMemberItem],
    summary="查询组织成员列表",
)
async def get_org_members(
    org_id: uuid.UUID,
    q: str | None = Query(default=None, max_length=120, description="按邮箱或姓名模糊检索"),
    member_status: str | None = Query(
        default=None,
        alias="status",
        description="成员状态筛选（ACTIVE / SUSPENDED / REMOVED）",
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    org_ctx: OrgContext = Depends(require_permission("org:member:read")),
    session: AsyncSession = Depends(get_db_session),
) -> OffsetPage[OrganizationMemberItem]:
    """在 FORCE RLS 保护下分页查询指定组织的成员列表。"""
    _ = org_id
    return await list_organization_members(
        org_ctx=org_ctx,
        session=session,
        q=q,
        status_filter=member_status,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/{org_id}/members",
    response_model=OrganizationMemberItem,
    status_code=status.HTTP_201_CREATED,
    summary="添加组织成员",
)
async def post_org_member(
    org_id: uuid.UUID,
    payload: OrganizationMemberCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("org:member:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationMemberItem:
    """将已有或新建用户加入当前组织。"""
    _ = org_id
    return await add_organization_member(
        org_ctx=org_ctx,
        payload=payload,
        session=session,
    )


@router.patch(
    "/{org_id}/members/{member_id}",
    response_model=OrganizationMemberItem,
    summary="更新组织成员状态",
)
async def patch_org_member(
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    payload: OrganizationMemberUpdateRequest,
    org_ctx: OrgContext = Depends(require_permission("org:member:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationMemberItem:
    """启用、停用或移除组织成员。"""
    _ = org_id
    return await update_organization_member(
        org_ctx=org_ctx,
        member_id=member_id,
        payload=payload,
        session=session,
    )
