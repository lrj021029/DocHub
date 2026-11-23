"""角色与功能权限管理服务（物理分离 SystemRole 与 OrganizationRole，强制 FORCE RLS）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.session import org_rls_session
from app.modules.audit.service import record_audit_event, record_security_audit_event
from app.modules.identity.deps import OrgContext
from app.modules.identity.models import User
from app.modules.organizations.models import OrganizationMember
from app.modules.roles.models import (
    OrganizationRole,
    OrganizationRolePermission,
    Permission,
    SystemRole,
    SystemRolePermission,
    UserRoleMembership,
)
from app.modules.roles.schemas import (
    OrganizationRoleCreateRequest,
    OrganizationRoleItem,
    PermissionCatalogResponse,
    PermissionItem,
    RoleAssignmentItem,
    RoleAssignRequest,
    SystemRoleItem,
)


async def get_permission_catalog(session: AsyncSession) -> PermissionCatalogResponse:
    """查询全平台权限树及 SystemRole 只读概览。"""
    async with session.begin():
        perms = (
            await session.execute(
                select(Permission).order_by(Permission.scope.asc(), Permission.code.asc())
            )
        ).scalars().all()
        sys_roles = (
            await session.execute(select(SystemRole).order_by(SystemRole.code.asc()))
        ).scalars().all()
        sys_links = (
            await session.execute(
                select(SystemRolePermission.system_role_id, Permission.code).join(
                    Permission, Permission.id == SystemRolePermission.permission_id
                )
            )
        ).all()

    codes_by_sys_role: dict[uuid.UUID, list[str]] = {}
    for role_id, code in sys_links:
        codes_by_sys_role.setdefault(role_id, []).append(code)

    return PermissionCatalogResponse(
        permissions=[
            PermissionItem(
                id=p.id,
                code=p.code,
                scope=p.scope,
                description=p.description,
            )
            for p in perms
        ],
        system_roles=[
            SystemRoleItem(
                id=sr.id,
                code=sr.code,
                name=sr.name,
                description=sr.description,
                permission_codes=sorted(codes_by_sys_role.get(sr.id, [])),
            )
            for sr in sys_roles
        ],
    )


async def list_organization_roles(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> list[OrganizationRoleItem]:
    """在 FORCE RLS 保护下查询当前组织的角色、权限集合及成员授予列表。"""
    now = datetime.now(UTC)
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        roles = (
            await session.execute(
                select(OrganizationRole)
                .where(OrganizationRole.organization_id == org_ctx.organization_id)
                .order_by(OrganizationRole.created_at.asc())
            )
        ).scalars().all()

        perm_links = (
            await session.execute(
                select(
                    OrganizationRolePermission.organization_role_id,
                    Permission.code,
                )
                .join(Permission, Permission.id == OrganizationRolePermission.permission_id)
                .where(OrganizationRolePermission.organization_id == org_ctx.organization_id)
            )
        ).all()

        assignment_rows = (
            await session.execute(
                select(UserRoleMembership, User)
                .join(User, User.id == UserRoleMembership.user_id)
                .where(
                    UserRoleMembership.organization_id == org_ctx.organization_id,
                    UserRoleMembership.status == "ACTIVE",
                )
                .order_by(UserRoleMembership.created_at.asc())
            )
        ).all()

    perms_by_role: dict[uuid.UUID, list[str]] = {}
    for role_id, perm_code in perm_links:
        perms_by_role.setdefault(role_id, []).append(perm_code)

    assignments_by_role: dict[uuid.UUID, list[RoleAssignmentItem]] = {}
    active_count_by_role: dict[uuid.UUID, int] = {}
    for membership, user in assignment_rows:
        item = RoleAssignmentItem(
            id=membership.id,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=membership.status,
            valid_from=membership.valid_from,
            valid_until=membership.valid_until,
        )
        assignments_by_role.setdefault(membership.organization_role_id, []).append(item)
        if membership.valid_from <= now and (
            membership.valid_until is None or membership.valid_until > now
        ):
            active_count_by_role[membership.organization_role_id] = (
                active_count_by_role.get(membership.organization_role_id, 0) + 1
            )

    return [
        OrganizationRoleItem(
            id=role.id,
            organization_id=role.organization_id,
            code=role.code,
            name=role.name,
            description=role.description,
            status=role.status,
            permission_codes=sorted(perms_by_role.get(role.id, [])),
            member_count=active_count_by_role.get(role.id, 0),
            assignments=assignments_by_role.get(role.id, []),
            created_at=role.created_at,
        )
        for role in roles
    ]


async def create_organization_role(
    *,
    org_ctx: OrgContext,
    payload: OrganizationRoleCreateRequest,
    session: AsyncSession,
) -> OrganizationRoleItem:
    """创建自定义组织角色并绑定 ORGANIZATION 作用域权限（严禁绑定 SYSTEM 权限）。"""
    unique_codes = sorted(set(payload.permission_codes))

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        existing = (
            await session.execute(
                select(OrganizationRole.id).where(
                    OrganizationRole.organization_id == org_ctx.organization_id,
                    OrganizationRole.code == payload.code,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError(code="ROLE_CODE_EXISTS", message="当前组织内已存在相同编码的角色")

        matched_perms: list[Permission] = []
        if unique_codes:
            matched_perms = list(
                (
                    await session.execute(
                        select(Permission).where(Permission.code.in_(unique_codes))
                    )
                )
                .scalars()
                .all()
            )
            matched_by_code = {p.code: p for p in matched_perms}
            missing = [c for c in unique_codes if c not in matched_by_code]
            if missing:
                raise ValidationError(
                    code="UNKNOWN_PERMISSION_CODES",
                    message="包含不存在的权限编码",
                    detail={"unknown_codes": missing},
                )
            forbidden_system = [p.code for p in matched_perms if p.scope != "ORGANIZATION"]
            if forbidden_system:
                raise ValidationError(
                    code="SYSTEM_PERMISSION_FORBIDDEN_IN_ORG_ROLE",
                    message="组织角色严禁绑定平台级 SYSTEM 权限",
                    detail={"forbidden_codes": forbidden_system},
                )

        role = OrganizationRole(
            organization_id=org_ctx.organization_id,
            code=payload.code,
            name=payload.name.strip(),
            description=payload.description.strip() if payload.description else None,
            status="ACTIVE",
        )
        session.add(role)
        await session.flush()

        for perm in matched_perms:
            session.add(
                OrganizationRolePermission(
                    organization_id=org_ctx.organization_id,
                    organization_role_id=role.id,
                    permission_id=perm.id,
                )
            )

        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="org.role.created",
            resource_type="organization_role",
            resource_id=role.id,
            details={"code": role.code, "permission_codes": unique_codes},
        )
        record_security_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            event_type="rbac.org_role.created",
            severity="MEDIUM",
            resource_type="organization_role",
            resource_id=role.id,
            details={"code": role.code, "permission_codes": unique_codes},
        )

    return OrganizationRoleItem(
        id=role.id,
        organization_id=role.organization_id,
        code=role.code,
        name=role.name,
        description=role.description,
        status=role.status,
        permission_codes=unique_codes,
        member_count=0,
        assignments=[],
        created_at=role.created_at,
    )


async def assign_organization_role(
    *,
    org_ctx: OrgContext,
    role_id: uuid.UUID,
    payload: RoleAssignRequest,
    session: AsyncSession,
) -> RoleAssignmentItem:
    """向当前组织的有效成员授予组织角色（支持生效/到期时间窗口）并写安全审计。"""
    now = datetime.now(UTC)
    valid_from = payload.valid_from or now
    if payload.valid_until is not None and payload.valid_until <= valid_from:
        raise ValidationError(
            code="INVALID_VALIDITY_RANGE",
            message="到期时间 valid_until 必须晚于生效时间 valid_from",
        )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        role = (
            await session.execute(
                select(OrganizationRole).where(
                    OrganizationRole.id == role_id,
                    OrganizationRole.organization_id == org_ctx.organization_id,
                    OrganizationRole.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if role is None:
            raise NotFoundError(code="ROLE_NOT_FOUND", message="目标组织角色不存在或已停用")

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
                message="目标用户不是当前组织的有效成员，无法授予组织角色",
            )

        user = await session.get(User, payload.user_id)
        if user is None:
            raise NotFoundError(code="USER_NOT_FOUND", message="目标用户不存在")

        assignment = UserRoleMembership(
            organization_id=org_ctx.organization_id,
            user_id=payload.user_id,
            organization_role_id=role_id,
            status="ACTIVE",
            valid_from=valid_from,
            valid_until=payload.valid_until,
        )
        session.add(assignment)
        await session.flush()

        record_security_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            event_type="rbac.role.assigned",
            severity="HIGH",
            resource_type="user_role_membership",
            resource_id=assignment.id,
            details={
                "role_code": role.code,
                "target_user_id": str(payload.user_id),
                "valid_from": valid_from.isoformat(),
                "valid_until": payload.valid_until.isoformat() if payload.valid_until else None,
            },
        )

    return RoleAssignmentItem(
        id=assignment.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=assignment.status,
        valid_from=assignment.valid_from,
        valid_until=assignment.valid_until,
    )


async def revoke_organization_role_assignment(
    *,
    org_ctx: OrgContext,
    role_id: uuid.UUID,
    assignment_id: uuid.UUID,
    session: AsyncSession,
) -> None:
    """撤销指定成员的组织角色授予并记录安全审计。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        assignment = (
            await session.execute(
                select(UserRoleMembership).where(
                    UserRoleMembership.id == assignment_id,
                    UserRoleMembership.organization_role_id == role_id,
                    UserRoleMembership.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if assignment is None:
            raise NotFoundError(
                code="ROLE_ASSIGNMENT_NOT_FOUND",
                message="目标角色授予记录不存在或无权访问",
            )

        assignment.status = "REVOKED"
        record_security_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            event_type="rbac.role.revoked",
            severity="HIGH",
            resource_type="user_role_membership",
            resource_id=assignment.id,
            details={
                "organization_role_id": str(role_id),
                "target_user_id": str(assignment.user_id),
            },
        )
