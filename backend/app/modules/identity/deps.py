"""后端认证、多组织上下文与细粒度 RBAC 权限依赖（FastAPI Depends）。

核心安全红线：
1. 严格解析并验证 Access JWT 与对应 `UserSession` 未被吊销，严禁信任未签名 Header。
2. `require_org_context` 强制在 `org_rls_session` 下实时复核当前用户在目标组织的
   `OrganizationMember(status='ACTIVE')`，彻底阻断跨组织 IDOR。
3. `require_permission` 不对 `SYSTEM_ADMIN` 开放任何隐式知识读取旁路，所有权限必须
   由 `SystemRole` 或当前活跃组织的未过期 `OrganizationRole` 显式授予。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthError, ForbiddenError
from app.core.security import decode_access_token
from app.db.session import get_db_session, org_rls_session
from app.modules.audit.service import record_security_audit_event
from app.modules.departments.models import Department, DepartmentMembership
from app.modules.identity.models import User, UserSession
from app.modules.organizations.models import Organization, OrganizationMember
from app.modules.roles.models import (
    OrganizationRole,
    OrganizationRolePermission,
    Permission,
    SystemRole,
    SystemRolePermission,
    UserRoleMembership,
    UserSystemRole,
)

_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    """已通过身份认证的平台主体上下文。"""

    user_id: uuid.UUID
    email: str
    display_name: str
    status: str
    session_id: uuid.UUID | None
    token_org_id: uuid.UUID | None
    system_roles: tuple[str, ...]
    system_permissions: frozenset[str]


@dataclass(frozen=True, slots=True)
class OrgContext:
    """绑定单一活跃 Organization 的权威鉴权上下文。"""

    principal: Principal
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    organization_roles: tuple[str, ...]
    department_ids: tuple[uuid.UUID, ...]
    permissions: frozenset[str]

    def has_permission(self, permission_code: str) -> bool:
        """检查当前组织上下文内是否具备指定功能权限（无隐式超级管理员旁路）。"""
        return permission_code in self.permissions


def _parse_optional_uuid(raw: str | None, *, header_name: str) -> uuid.UUID | None:
    """解析可选 UUID 字符串，非法格式抛出 AuthError/ForbiddenError。"""
    if raw is None or not raw.strip():
        return None
    try:
        return uuid.UUID(raw.strip())
    except ValueError as exc:
        raise ForbiddenError(
            code="INVALID_ORGANIZATION_HEADER",
            message=f"请求头 {header_name} 不是有效的 UUID",
        ) from exc


async def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> Principal:
    """校验 Bearer Access JWT，复核用户与会话状态并加载平台 SystemRole 权限。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError(code="AUTH_REQUIRED", message="请先登录提供有效的访问令牌")

    payload = decode_access_token(credentials.credentials)

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise AuthError(code="INVALID_TOKEN", message="访问令牌主体标识无效") from exc

    token_org_id: uuid.UUID | None = None
    if payload.get("org_id"):
        try:
            token_org_id = uuid.UUID(str(payload["org_id"]))
        except ValueError as exc:
            raise AuthError(code="INVALID_TOKEN", message="访问令牌组织标识无效") from exc

    session_id: uuid.UUID | None = None
    if payload.get("sid"):
        try:
            session_id = uuid.UUID(str(payload["sid"]))
        except ValueError as exc:
            raise AuthError(code="INVALID_TOKEN", message="访问令牌会话标识无效") from exc

    async with session.begin():
        user = await session.get(User, user_id)
        if user is None or user.status != "ACTIVE":
            raise AuthError(code="USER_INACTIVE", message="账号不存在或已停用")

        # 若令牌关联会话 ID，则实时校验会话未被登出或因重放攻击吊销
        if session_id is not None:
            user_session = await session.get(UserSession, session_id)
            now = datetime.now(UTC)
            if (
                user_session is None
                or user_session.user_id != user_id
                or user_session.revoked_at is not None
                or user_session.expires_at <= now
            ):
                raise AuthError(code="SESSION_REVOKED", message="当前登录会话已失效，请重新登录")

        # 查询平台级 SystemRole 与其关联的平台功能权限
        sys_roles_rows = (
            await session.execute(
                select(SystemRole.code, SystemRole.id)
                .join(UserSystemRole, UserSystemRole.system_role_id == SystemRole.id)
                .where(
                    UserSystemRole.user_id == user_id,
                    UserSystemRole.status == "ACTIVE",
                )
            )
        ).all()

        system_role_codes = tuple(sorted({row.code for row in sys_roles_rows}))
        system_role_ids = [row.id for row in sys_roles_rows]

        system_perm_codes: set[str] = set()
        if system_role_ids:
            perm_rows = (
                await session.execute(
                    select(Permission.code)
                    .join(
                        SystemRolePermission,
                        SystemRolePermission.permission_id == Permission.id,
                    )
                    .where(SystemRolePermission.system_role_id.in_(system_role_ids))
                )
            ).scalars().all()
            system_perm_codes = set(perm_rows)

    return Principal(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        session_id=session_id,
        token_org_id=token_org_id,
        system_roles=system_role_codes,
        system_permissions=frozenset(system_perm_codes),
    )


async def resolve_org_context(
    *,
    session: AsyncSession,
    principal: Principal,
    target_org_id: uuid.UUID,
) -> OrgContext:
    """在 PostgreSQL FORCE RLS 保护下实时校验并构建用户在指定组织的权威权限上下文。"""
    now = datetime.now(UTC)

    async with org_rls_session(session, target_org_id, user_id=principal.user_id):
        org = await session.get(Organization, target_org_id)
        if org is None or org.status != "ACTIVE":
            raise ForbiddenError(
                code="ORG_ACCESS_DENIED",
                message="目标组织不存在、未启用或无权访问",
            )

        member = (
            await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == target_org_id,
                    OrganizationMember.user_id == principal.user_id,
                    OrganizationMember.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if member is None:
            raise ForbiddenError(
                code="ORG_ACCESS_DENIED",
                message="当前账号不是目标组织的有效成员",
            )

        # 加载当前组织内有效且在有效期内的 OrganizationRole
        active_org_roles = (
            await session.execute(
                select(OrganizationRole.id, OrganizationRole.code)
                .join(
                    UserRoleMembership,
                    UserRoleMembership.organization_role_id == OrganizationRole.id,
                )
                .where(
                    OrganizationRole.organization_id == target_org_id,
                    OrganizationRole.status == "ACTIVE",
                    UserRoleMembership.organization_id == target_org_id,
                    UserRoleMembership.user_id == principal.user_id,
                    UserRoleMembership.status == "ACTIVE",
                    UserRoleMembership.valid_from <= now,
                    or_(
                        UserRoleMembership.valid_until.is_(None),
                        UserRoleMembership.valid_until > now,
                    ),
                )
            )
        ).all()

        org_role_codes = tuple(sorted({row.code for row in active_org_roles}))
        org_role_ids = [row.id for row in active_org_roles]

        org_perm_codes: set[str] = set()
        if org_role_ids:
            org_perm_rows = (
                await session.execute(
                    select(Permission.code)
                    .join(
                        OrganizationRolePermission,
                        OrganizationRolePermission.permission_id == Permission.id,
                    )
                    .where(
                        OrganizationRolePermission.organization_id == target_org_id,
                        OrganizationRolePermission.organization_role_id.in_(org_role_ids),
                    )
                )
            ).scalars().all()
            org_perm_codes = set(org_perm_rows)

        # 加载有效且未过期的部门归属 ID 列表
        dept_rows = (
            await session.execute(
                select(DepartmentMembership.department_id)
                .join(
                    Department,
                    and_(
                        Department.id == DepartmentMembership.department_id,
                        Department.organization_id == target_org_id,
                    ),
                )
                .where(
                    DepartmentMembership.organization_id == target_org_id,
                    DepartmentMembership.user_id == principal.user_id,
                    DepartmentMembership.status == "ACTIVE",
                    Department.status == "ACTIVE",
                    DepartmentMembership.valid_from <= now,
                    or_(
                        DepartmentMembership.valid_until.is_(None),
                        DepartmentMembership.valid_until > now,
                    ),
                )
            )
        ).scalars().all()

    effective_permissions = principal.system_permissions | frozenset(org_perm_codes)
    return OrgContext(
        principal=principal,
        organization_id=org.id,
        organization_name=org.name,
        organization_slug=org.slug,
        organization_roles=org_role_codes,
        department_ids=tuple(dict.fromkeys(dept_rows)),
        permissions=effective_permissions,
    )


async def require_org_context(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    x_organization_id: str | None = Header(default=None, alias="X-Organization-Id"),
    session: AsyncSession = Depends(get_db_session),
) -> OrgContext:
    """解析并强制绑定当前请求的单一 Organization 上下文。

    解析优先级与一致性守卫：
    - 若路径参数包含 `org_id`，以路径 `org_id` 为目标组织；若同时携带了 `X-Organization-Id`
      且二者不一致，立即拒绝（防跨租户参数混淆）。
    - 若路径无 `org_id`，依次取 `X-Organization-Id` 请求头或 Access JWT 中的 `org_id`。
    """
    path_org_raw = request.path_params.get("org_id")
    header_org_id = _parse_optional_uuid(x_organization_id, header_name="X-Organization-Id")

    target_org_id: uuid.UUID | None = None
    if path_org_raw is not None:
        try:
            path_org_id = uuid.UUID(str(path_org_raw))
        except ValueError as exc:
            raise ForbiddenError(code="INVALID_ORG_ID", message="路径中的组织 ID 无效") from exc
        if header_org_id is not None and header_org_id != path_org_id:
            raise ForbiddenError(
                code="ORG_CONTEXT_MISMATCH",
                message="请求头 X-Organization-Id 与目标路径组织不一致",
            )
        target_org_id = path_org_id
    else:
        target_org_id = header_org_id or principal.token_org_id

    if target_org_id is None:
        raise ForbiddenError(
            code="ORG_CONTEXT_REQUIRED",
            message="当前请求缺少有效的活跃组织上下文",
        )

    return await resolve_org_context(
        session=session,
        principal=principal,
        target_org_id=target_org_id,
    )


def require_system_permission(permission_code: str) -> Callable[..., Principal]:
    """要求当前认证主体具备指定的平台级 SystemRole 权限（不依赖组织上下文）。"""

    async def _dependency(
        principal: Principal = Depends(get_current_principal),
    ) -> Principal:
        if permission_code not in principal.system_permissions:
            raise ForbiddenError(
                code="PERMISSION_DENIED",
                message="无权执行该平台管理操作",
            )
        return principal

    return _dependency


def require_permission(permission_code: str) -> Callable[..., OrgContext]:
    """要求当前活跃组织上下文具备指定功能权限（无 SystemAdmin 隐式旁路）。

    越权尝试自动写入当前组织的 `security_audit_events` 并返回 403。
    """

    async def _dependency(
        request: Request,
        org_ctx: OrgContext = Depends(require_org_context),
        session: AsyncSession = Depends(get_db_session),
    ) -> OrgContext:
        if org_ctx.has_permission(permission_code):
            return org_ctx

        # 独立记录越权访问安全审计事件（脱敏）
        client_ip = request.client.host if request.client else None
        async with org_rls_session(
            session,
            org_ctx.organization_id,
            user_id=org_ctx.principal.user_id,
        ):
            record_security_audit_event(
                session,
                organization_id=org_ctx.organization_id,
                actor_user_id=org_ctx.principal.user_id,
                event_type="authz.permission_denied",
                severity="MEDIUM",
                resource_type="endpoint",
                resource_id=request.url.path,
                ip_address=client_ip,
                details={
                    "required_permission": permission_code,
                    "method": request.method,
                },
            )

        raise ForbiddenError(
            code="PERMISSION_DENIED",
            message="当前角色缺少执行该操作的权限",
        )

    return _dependency
