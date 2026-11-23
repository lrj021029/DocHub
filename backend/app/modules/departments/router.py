"""部门树与多部门兼职 HTTP 路由（`/api/v1/organizations/{org_id}/departments/*`）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.departments.schemas import (
    DepartmentCreateRequest,
    DepartmentItem,
    DepartmentMembershipCreateRequest,
    DepartmentMembershipItem,
    DepartmentUpdateRequest,
)
from app.modules.departments.service import (
    create_department,
    create_department_membership,
    list_department_memberships,
    list_departments,
    revoke_department_membership,
    update_department,
)
from app.modules.identity.deps import OrgContext, require_permission

router = APIRouter(prefix="/organizations/{org_id}/departments", tags=["Departments"])


@router.get(
    "",
    response_model=list[DepartmentItem],
    summary="查询组织部门树",
)
async def get_departments(
    org_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("org:department:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DepartmentItem]:
    """返回当前组织部门树节点及 Closure 深度/路径信息。"""
    _ = org_id
    return await list_departments(org_ctx=org_ctx, session=session)


@router.post(
    "",
    response_model=DepartmentItem,
    status_code=status.HTTP_201_CREATED,
    summary="创建部门节点",
)
async def post_department(
    org_id: uuid.UUID,
    payload: DepartmentCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("org:department:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> DepartmentItem:
    """创建部门并事务性维护 `department_closures`。"""
    _ = org_id
    return await create_department(org_ctx=org_ctx, payload=payload, session=session)


@router.patch(
    "/{dept_id}",
    response_model=DepartmentItem,
    summary="更新部门名称、状态或移动子树",
)
async def patch_department(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    payload: DepartmentUpdateRequest,
    org_ctx: OrgContext = Depends(require_permission("org:department:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> DepartmentItem:
    """修改部门信息或安全迁移子树（自动防环并重建 Closure Table）。"""
    _ = org_id
    return await update_department(
        org_ctx=org_ctx,
        dept_id=dept_id,
        payload=payload,
        session=session,
    )


@router.get(
    "/{dept_id}/memberships",
    response_model=list[DepartmentMembershipItem],
    summary="查询部门成员归属列表",
)
async def get_department_memberships(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    include_descendants: bool = Query(default=False, description="是否递归包含下级子部门成员"),
    active_only: bool = Query(default=False, description="是否仅返回当前处于有效期内的归属"),
    org_ctx: OrgContext = Depends(require_permission("org:department:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DepartmentMembershipItem]:
    """查询指定部门（可选含子树）的成员多部门归属。"""
    _ = org_id
    return await list_department_memberships(
        org_ctx=org_ctx,
        dept_id=dept_id,
        include_descendants=include_descendants,
        active_only=active_only,
        session=session,
    )


@router.post(
    "/{dept_id}/memberships",
    response_model=DepartmentMembershipItem,
    status_code=status.HTTP_201_CREATED,
    summary="为用户分配部门或兼职部门",
)
async def post_department_membership(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    payload: DepartmentMembershipCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("org:department:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> DepartmentMembershipItem:
    """为用户绑定主部门、兼职部门或临时有效期部门。"""
    _ = org_id
    return await create_department_membership(
        org_ctx=org_ctx,
        dept_id=dept_id,
        payload=payload,
        session=session,
    )


@router.delete(
    "/{dept_id}/memberships/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="撤销部门成员归属",
)
async def delete_department_membership(
    org_id: uuid.UUID,
    dept_id: uuid.UUID,
    membership_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("org:department:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """撤销指定部门归属关系。"""
    _ = org_id
    await revoke_department_membership(
        org_ctx=org_ctx,
        dept_id=dept_id,
        membership_id=membership_id,
        session=session,
    )
