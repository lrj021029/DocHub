"""部门邻接树、Closure Table 事务性维护与多部门兼职服务。

核心保证：
1. 全部读写均运行于 `org_rls_session` 事务内，受 PostgreSQL FORCE RLS 强保护。
2. 创建子部门自动维护 `department_closures`（含 `organization_id`）。
3. 移动部门子树前强制执行防环检测（检查目标父节点是否为自身或自身后代），
   随后在同一事务内原子更新邻接指针与所有跨子树 Closure 路径。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import new_uuid7
from app.db.session import org_rls_session
from app.modules.audit.service import record_audit_event
from app.modules.departments.models import (
    Department,
    DepartmentClosure,
    DepartmentMembership,
)
from app.modules.departments.schemas import (
    DepartmentCreateRequest,
    DepartmentItem,
    DepartmentMembershipCreateRequest,
    DepartmentMembershipItem,
    DepartmentUpdateRequest,
)
from app.modules.identity.deps import OrgContext
from app.modules.identity.models import User
from app.modules.organizations.models import OrganizationMember


async def create_department_in_tx(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    parent_id: uuid.UUID | None,
) -> Department:
    """在已开启的 org_rls_session 事务内创建部门并原子写入 Closure Table。"""
    cleaned_name = name.strip()
    if parent_id is not None:
        parent = (
            await session.execute(
                select(Department).where(
                    Department.id == parent_id,
                    Department.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if parent is None:
            raise NotFoundError(code="PARENT_DEPARTMENT_NOT_FOUND", message="指定的上级部门不存在")

    # 检查同父节点下是否重名
    dup_stmt = select(Department.id).where(
        Department.organization_id == organization_id,
        Department.name == cleaned_name,
        Department.parent_id.is_(None) if parent_id is None else Department.parent_id == parent_id,
    )
    if (await session.execute(dup_stmt)).scalar_one_or_none() is not None:
        raise ConflictError(code="DEPARTMENT_NAME_EXISTS", message="同级目录下已存在同名部门")

    dept_id = new_uuid7()
    dept = Department(
        id=dept_id,
        organization_id=organization_id,
        parent_id=parent_id,
        name=cleaned_name,
        status="ACTIVE",
    )
    session.add(dept)
    await session.flush()

    # 1. 写入自身闭包记录 (dept, dept, 0)
    session.add(
        DepartmentClosure(
            organization_id=organization_id,
            ancestor_id=dept_id,
            descendant_id=dept_id,
            depth=0,
        )
    )

    # 2. 继承父节点全部祖先闭包路径 (ancestor, dept, depth + 1)
    if parent_id is not None:
        parent_ancestors = (
            await session.execute(
                select(DepartmentClosure.ancestor_id, DepartmentClosure.depth).where(
                    DepartmentClosure.organization_id == organization_id,
                    DepartmentClosure.descendant_id == parent_id,
                )
            )
        ).all()
        for row in parent_ancestors:
            session.add(
                DepartmentClosure(
                    organization_id=organization_id,
                    ancestor_id=row.ancestor_id,
                    descendant_id=dept_id,
                    depth=row.depth + 1,
                )
            )

    await session.flush()
    return dept


async def _move_department_subtree_in_tx(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    dept: Department,
    new_parent_id: uuid.UUID | None,
) -> None:
    """在事务内执行防环校验并原子迁移整个部门子树的 Closure Table 路径。"""
    if new_parent_id == dept.parent_id:
        return

    if new_parent_id == dept.id:
        raise ConflictError(
            code="DEPARTMENT_CYCLE_DETECTED",
            message="不能将部门的上级设置为其自身",
        )

    if new_parent_id is not None:
        target_parent = (
            await session.execute(
                select(Department).where(
                    Department.id == new_parent_id,
                    Department.organization_id == organization_id,
                )
            )
        ).scalar_one_or_none()
        if target_parent is None:
            raise NotFoundError(code="PARENT_DEPARTMENT_NOT_FOUND", message="目标上级部门不存在")

        # 环路检测：若 new_parent_id 已是 dept 的后代节点，则移动会形成环路
        cycle_check = (
            await session.execute(
                select(DepartmentClosure.depth).where(
                    DepartmentClosure.organization_id == organization_id,
                    DepartmentClosure.ancestor_id == dept.id,
                    DepartmentClosure.descendant_id == new_parent_id,
                )
            )
        ).scalar_one_or_none()
        if cycle_check is not None:
            raise ConflictError(
                code="DEPARTMENT_CYCLE_DETECTED",
                message="禁止将部门移动到其后代部门下形成树环路",
            )

    # 1. 收集被移动子树全部节点（含自身）及其相对 dept 的深度
    subtree_rows = (
        await session.execute(
            select(DepartmentClosure.descendant_id, DepartmentClosure.depth).where(
                DepartmentClosure.organization_id == organization_id,
                DepartmentClosure.ancestor_id == dept.id,
            )
        )
    ).all()
    subtree_ids = [row.descendant_id for row in subtree_rows]

    # 2. 收集 dept 原有的全部严格祖先节点（depth > 0）
    old_ancestor_ids = (
        await session.execute(
            select(DepartmentClosure.ancestor_id).where(
                DepartmentClosure.organization_id == organization_id,
                DepartmentClosure.descendant_id == dept.id,
                DepartmentClosure.depth > 0,
            )
        )
    ).scalars().all()

    # 3. 删除“原外部祖先 -> 子树节点”的旧路径，保留子树内部闭合路径
    if old_ancestor_ids and subtree_ids:
        await session.execute(
            delete(DepartmentClosure).where(
                DepartmentClosure.organization_id == organization_id,
                DepartmentClosure.ancestor_id.in_(old_ancestor_ids),
                DepartmentClosure.descendant_id.in_(subtree_ids),
            )
        )

    # 4. 挂载到新父节点：对新父节点全部祖先与被移动子树做笛卡尔拼接
    if new_parent_id is not None:
        new_ancestors = (
            await session.execute(
                select(DepartmentClosure.ancestor_id, DepartmentClosure.depth).where(
                    DepartmentClosure.organization_id == organization_id,
                    DepartmentClosure.descendant_id == new_parent_id,
                )
            )
        ).all()
        for anc in new_ancestors:
            for sub in subtree_rows:
                session.add(
                    DepartmentClosure(
                        organization_id=organization_id,
                        ancestor_id=anc.ancestor_id,
                        descendant_id=sub.descendant_id,
                        depth=anc.depth + sub.depth + 1,
                    )
                )

    dept.parent_id = new_parent_id
    await session.flush()


async def list_departments(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> list[DepartmentItem]:
    """查询当前组织完整部门树（附带基于 Closure Table 计算的层级深度与完整路径）。"""
    now = datetime.now(UTC)
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        depts = (
            await session.execute(
                select(Department)
                .where(Department.organization_id == org_ctx.organization_id)
                .order_by(Department.created_at.asc())
            )
        ).scalars().all()

        closures = (
            await session.execute(
                select(
                    DepartmentClosure.ancestor_id,
                    DepartmentClosure.descendant_id,
                    DepartmentClosure.depth,
                ).where(DepartmentClosure.organization_id == org_ctx.organization_id)
            )
        ).all()

        member_counts_rows = (
            await session.execute(
                select(
                    DepartmentMembership.department_id,
                    func.count(DepartmentMembership.id),
                )
                .where(
                    DepartmentMembership.organization_id == org_ctx.organization_id,
                    DepartmentMembership.status == "ACTIVE",
                    DepartmentMembership.valid_from <= now,
                    or_(
                        DepartmentMembership.valid_until.is_(None),
                        DepartmentMembership.valid_until > now,
                    ),
                )
                .group_by(DepartmentMembership.department_id)
            )
        ).all()

    dept_by_id = {d.id: d for d in depts}
    count_map = {row[0]: int(row[1]) for row in member_counts_rows}

    ancestors_by_desc: dict[uuid.UUID, list[tuple[int, uuid.UUID]]] = {}
    for row in closures:
        ancestors_by_desc.setdefault(row.descendant_id, []).append((row.depth, row.ancestor_id))

    result: list[DepartmentItem] = []
    for dept in depts:
        chain = sorted(ancestors_by_desc.get(dept.id, [(0, dept.id)]), key=lambda x: -x[0])
        depth = max((item[0] for item in chain), default=0)
        path_names = [dept_by_id[anc_id].name for _, anc_id in chain if anc_id in dept_by_id]
        result.append(
            DepartmentItem(
                id=dept.id,
                organization_id=dept.organization_id,
                parent_id=dept.parent_id,
                name=dept.name,
                status=dept.status,
                depth=depth,
                path_names=path_names,
                member_count=count_map.get(dept.id, 0),
                created_at=dept.created_at,
            )
        )

    # 按路径顺序排列，前端可直接按 depth 缩进展示树形结构
    result.sort(key=lambda item: (item.path_names, item.created_at))
    return result


async def create_department(
    *,
    org_ctx: OrgContext,
    payload: DepartmentCreateRequest,
    session: AsyncSession,
) -> DepartmentItem:
    """新建部门并维护 Closure Table 与业务审计。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        dept = await create_department_in_tx(
            session,
            organization_id=org_ctx.organization_id,
            name=payload.name,
            parent_id=payload.parent_id,
        )
        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="department.created",
            resource_type="department",
            resource_id=dept.id,
            details={
                "name": dept.name,
                "parent_id": str(dept.parent_id) if dept.parent_id else None,
            },
        )
        created_id = dept.id

    all_items = await list_departments(org_ctx=org_ctx, session=session)
    for item in all_items:
        if item.id == created_id:
            return item
    raise NotFoundError(code="DEPARTMENT_NOT_FOUND", message="新建部门读取失败")


async def update_department(
    *,
    org_ctx: OrgContext,
    dept_id: uuid.UUID,
    payload: DepartmentUpdateRequest,
    session: AsyncSession,
) -> DepartmentItem:
    """更新部门名称、状态或安全移动子树（含防环与 Closure Table 重建）。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        dept = (
            await session.execute(
                select(Department).where(
                    Department.id == dept_id,
                    Department.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if dept is None:
            raise NotFoundError(code="DEPARTMENT_NOT_FOUND", message="目标部门不存在或无权访问")

        # 计算是否请求移动父节点
        parent_fields_set = payload.model_fields_set
        target_parent_id = dept.parent_id
        should_move = False
        if payload.move_to_root:
            target_parent_id = None
            should_move = True
        elif "parent_id" in parent_fields_set:
            target_parent_id = payload.parent_id
            should_move = True

        if should_move:
            await _move_department_subtree_in_tx(
                session,
                organization_id=org_ctx.organization_id,
                dept=dept,
                new_parent_id=target_parent_id,
            )

        if payload.name is not None:
            new_name = payload.name.strip()
            dup_stmt = select(Department.id).where(
                Department.organization_id == org_ctx.organization_id,
                Department.id != dept.id,
                Department.name == new_name,
                Department.parent_id.is_(None)
                if dept.parent_id is None
                else Department.parent_id == dept.parent_id,
            )
            if (await session.execute(dup_stmt)).scalar_one_or_none() is not None:
                raise ConflictError(
                    code="DEPARTMENT_NAME_EXISTS",
                    message="目标父级下已存在同名部门",
                )
            dept.name = new_name

        if payload.status is not None:
            dept.status = payload.status

        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="department.updated",
            resource_type="department",
            resource_id=dept.id,
            details={
                "name": dept.name,
                "parent_id": str(dept.parent_id) if dept.parent_id else None,
                "status": dept.status,
                "moved": should_move,
            },
        )

    all_items = await list_departments(org_ctx=org_ctx, session=session)
    for item in all_items:
        if item.id == dept_id:
            return item
    raise NotFoundError(code="DEPARTMENT_NOT_FOUND", message="目标部门不存在或无权访问")


async def list_department_memberships(
    *,
    org_ctx: OrgContext,
    dept_id: uuid.UUID,
    include_descendants: bool,
    active_only: bool,
    session: AsyncSession,
) -> list[DepartmentMembershipItem]:
    """查询指定部门（可选包含全部后代子部门）的成员归属记录。"""
    now = datetime.now(UTC)
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        dept = (
            await session.execute(
                select(Department).where(
                    Department.id == dept_id,
                    Department.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if dept is None:
            raise NotFoundError(code="DEPARTMENT_NOT_FOUND", message="目标部门不存在或无权访问")

        if include_descendants:
            target_dept_ids = (
                await session.execute(
                    select(DepartmentClosure.descendant_id).where(
                        DepartmentClosure.organization_id == org_ctx.organization_id,
                        DepartmentClosure.ancestor_id == dept_id,
                    )
                )
            ).scalars().all()
        else:
            target_dept_ids = [dept_id]

        stmt = (
            select(DepartmentMembership, Department.name, User)
            .join(Department, Department.id == DepartmentMembership.department_id)
            .join(User, User.id == DepartmentMembership.user_id)
            .where(
                DepartmentMembership.organization_id == org_ctx.organization_id,
                DepartmentMembership.department_id.in_(target_dept_ids),
            )
        )
        if active_only:
            stmt = stmt.where(
                DepartmentMembership.status == "ACTIVE",
                DepartmentMembership.valid_from <= now,
                or_(
                    DepartmentMembership.valid_until.is_(None),
                    DepartmentMembership.valid_until > now,
                ),
            )

        rows = (
            await session.execute(
                stmt.order_by(
                    DepartmentMembership.is_primary.desc(),
                    DepartmentMembership.created_at.asc(),
                )
            )
        ).all()

    return [
        DepartmentMembershipItem(
            id=membership.id,
            organization_id=membership.organization_id,
            department_id=membership.department_id,
            department_name=dept_name,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            membership_type=membership.membership_type,
            is_primary=membership.is_primary,
            status=membership.status,
            valid_from=membership.valid_from,
            valid_until=membership.valid_until,
        )
        for membership, dept_name, user in rows
    ]


async def create_department_membership(
    *,
    org_ctx: OrgContext,
    dept_id: uuid.UUID,
    payload: DepartmentMembershipCreateRequest,
    session: AsyncSession,
) -> DepartmentMembershipItem:
    """为用户分配部门或多部门兼职（校验目标用户必须是当前组织有效成员）。"""
    now = datetime.now(UTC)
    valid_from = payload.valid_from or now
    if payload.valid_until is not None and payload.valid_until <= valid_from:
        raise ValidationError(
            code="INVALID_VALIDITY_RANGE",
            message="到期时间 valid_until 必须晚于生效时间 valid_from",
        )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        dept = (
            await session.execute(
                select(Department).where(
                    Department.id == dept_id,
                    Department.organization_id == org_ctx.organization_id,
                    Department.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if dept is None:
            raise NotFoundError(code="DEPARTMENT_NOT_FOUND", message="目标部门不存在或已归档")

        member = (
            await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org_ctx.organization_id,
                    OrganizationMember.user_id == payload.user_id,
                    OrganizationMember.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise ValidationError(
                code="USER_NOT_IN_ORGANIZATION",
                message="目标用户不是当前组织的有效成员，无法分配部门",
            )

        user = await session.get(User, payload.user_id)
        if user is None:
            raise NotFoundError(code="USER_NOT_FOUND", message="目标用户不存在")

        # 若标记为主部门，自动将该用户在本组织其他主部门标志置为 false
        if payload.is_primary:
            await session.execute(
                update(DepartmentMembership)
                .where(
                    DepartmentMembership.organization_id == org_ctx.organization_id,
                    DepartmentMembership.user_id == payload.user_id,
                    DepartmentMembership.is_primary.is_(True),
                )
                .values(is_primary=False)
            )

        membership = DepartmentMembership(
            organization_id=org_ctx.organization_id,
            department_id=dept_id,
            user_id=payload.user_id,
            membership_type=payload.membership_type,
            is_primary=payload.is_primary,
            status="ACTIVE",
            valid_from=valid_from,
            valid_until=payload.valid_until,
        )
        session.add(membership)
        await session.flush()

        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="department.membership.assigned",
            resource_type="department_membership",
            resource_id=membership.id,
            details={
                "department_id": str(dept_id),
                "target_user_id": str(payload.user_id),
                "membership_type": payload.membership_type,
                "is_primary": payload.is_primary,
            },
        )

    return DepartmentMembershipItem(
        id=membership.id,
        organization_id=membership.organization_id,
        department_id=membership.department_id,
        department_name=dept.name,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        membership_type=membership.membership_type,
        is_primary=membership.is_primary,
        status=membership.status,
        valid_from=membership.valid_from,
        valid_until=membership.valid_until,
    )


async def revoke_department_membership(
    *,
    org_ctx: OrgContext,
    dept_id: uuid.UUID,
    membership_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """撤销指定部门成员归属记录。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        membership = (
            await session.execute(
                select(DepartmentMembership).where(
                    DepartmentMembership.id == membership_id,
                    DepartmentMembership.department_id == dept_id,
                    DepartmentMembership.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise NotFoundError(
                code="DEPARTMENT_MEMBERSHIP_NOT_FOUND",
                message="目标部门归属记录不存在或无权访问",
            )

        membership.status = "REVOKED"
        membership.is_primary = False

        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="department.membership.revoked",
            resource_type="department_membership",
            resource_id=membership.id,
            details={
                "department_id": str(dept_id),
                "target_user_id": str(membership.user_id),
            },
        )
