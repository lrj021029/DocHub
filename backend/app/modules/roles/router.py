"""角色与权限树 HTTP 路由（`/api/v1/permissions` 与 `/api/v1/organizations/{org_id}/roles/*`）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.identity.deps import (
    OrgContext,
    Principal,
    get_current_principal,
    require_permission,
)
from app.modules.roles.schemas import (
    OrganizationRoleCreateRequest,
    OrganizationRoleItem,
    PermissionCatalogResponse,
    RoleAssignmentItem,
    RoleAssignRequest,
)
from app.modules.roles.service import (
    assign_organization_role,
    create_organization_role,
    get_permission_catalog,
    list_organization_roles,
    revoke_organization_role_assignment,
)

permissions_router = APIRouter(prefix="/permissions", tags=["Roles & Permissions"])
org_roles_router = APIRouter(
    prefix="/organizations/{org_id}/roles",
    tags=["Roles & Permissions"],
)


@permissions_router.get(
    "",
    response_model=PermissionCatalogResponse,
    summary="查询权限字典与平台 SystemRole 概览",
)
async def list_permissions(
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_db_session),
) -> PermissionCatalogResponse:
    """返回完整权限树（区分 SYSTEM 与 ORGANIZATION 作用域）及平台系统角色。"""
    _ = principal
    return await get_permission_catalog(session)


@org_roles_router.get(
    "",
    response_model=list[OrganizationRoleItem],
    summary="查询组织角色列表",
)
async def get_org_roles(
    org_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("org:role:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[OrganizationRoleItem]:
    """查询当前组织内的全部 OrganizationRole 及已授予成员。"""
    _ = org_id
    return await list_organization_roles(org_ctx=org_ctx, session=session)


@org_roles_router.post(
    "",
    response_model=OrganizationRoleItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建自定义组织角色",
)
async def post_org_role(
    org_id: uuid.UUID,
    payload: OrganizationRoleCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("org:role:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> OrganizationRoleItem:
    """在当前组织内创建新角色并绑定 ORGANIZATION 级功能权限。"""
    _ = org_id
    return await create_organization_role(
        org_ctx=org_ctx,
        payload=payload,
        session=session,
    )


@org_roles_router.post(
    "/{role_id}/assign",
    response_model=RoleAssignmentItem,
    status_code=status.HTTP_201_CREATED,
    summary="为组织成员授予组织角色",
)
async def post_assign_org_role(
    org_id: uuid.UUID,
    role_id: uuid.UUID,
    payload: RoleAssignRequest,
    org_ctx: OrgContext = Depends(require_permission("org:role:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> RoleAssignmentItem:
    """授予指定成员组织角色（支持生效/到期有效期）。"""
    _ = org_id
    return await assign_organization_role(
        org_ctx=org_ctx,
        role_id=role_id,
        payload=payload,
        session=session,
    )


@org_roles_router.delete(
    "/{role_id}/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="撤销成员的组织角色授予",
)
async def delete_org_role_assignment(
    org_id: uuid.UUID,
    role_id: uuid.UUID,
    assignment_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("org:role:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """撤销成员的角色授权。"""
    _ = org_id
    await revoke_organization_role_assignment(
        org_ctx=org_ctx,
        role_id=role_id,
        assignment_id=assignment_id,
        session=session,
    )
