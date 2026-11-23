"""身份认证核心业务服务：登录、Refresh Token 轮换、重放攻击家族吊销、登出与身份全景查询。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthError, ForbiddenError
from app.core.ids import new_uuid7
from app.core.security import (
    create_access_token,
    generate_csrf_token,
    generate_refresh_token,
    hash_refresh_token,
    verify_csrf_token,
    verify_password,
)
from app.db.session import org_rls_session, user_rls_session
from app.modules.audit.service import record_audit_event, record_security_audit_event
from app.modules.departments.models import Department, DepartmentMembership
from app.modules.identity.deps import Principal
from app.modules.identity.models import User, UserSession
from app.modules.identity.schemas import (
    AuthMeResponse,
    AuthTokenResponse,
    DepartmentMembershipBrief,
    LoginRequest,
    OrganizationBrief,
    RoleBrief,
    UserSummary,
)
from app.modules.organizations.models import Organization, OrganizationMember
from app.modules.roles.models import (
    OrganizationRole,
    OrganizationRolePermission,
    Permission,
    SystemRole,
    UserRoleMembership,
    UserSystemRole,
)

REFRESH_COOKIE_NAME = "dochub_refresh_token"
CSRF_COOKIE_NAME = "dochub_csrf"


def _set_auth_cookies(
    response: Response,
    *,
    raw_refresh_token: str,
    csrf_token: str,
    settings: Settings,
) -> None:
    """下发 HttpOnly Refresh Cookie 与非 HttpOnly CSRF Cookie（供前端双提交读取）。"""
    max_age = settings.jwt_refresh_token_expire_days * 86400
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_refresh_token,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/api/v1/auth",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
        path="/",
    )


def _clear_auth_cookies(response: Response, *, settings: Settings) -> None:
    """清除认证相关的 Refresh 与 CSRF Cookie。"""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path="/api/v1/auth",
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        path="/",
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )


async def list_user_organizations(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> list[OrganizationBrief]:
    """在 `user_rls_session` 下安全查询用户所属的全部有效组织。"""
    async with user_rls_session(session, user_id):
        rows = (
            await session.execute(
                select(
                    Organization.id,
                    Organization.name,
                    Organization.slug,
                    Organization.status,
                    OrganizationMember.status.label("member_status"),
                )
                .join(
                    OrganizationMember,
                    OrganizationMember.organization_id == Organization.id,
                )
                .where(
                    OrganizationMember.user_id == user_id,
                    OrganizationMember.status == "ACTIVE",
                    Organization.status == "ACTIVE",
                )
                .order_by(Organization.created_at.asc())
            )
        ).all()

    return [
        OrganizationBrief(
            id=row.id,
            name=row.name,
            slug=row.slug,
            status=row.status,
            member_status=row.member_status,
        )
        for row in rows
    ]


async def login_with_password(
    *,
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession,
    settings: Settings,
) -> AuthTokenResponse:
    """执行邮箱+Argon2id 密码登录，签发 Access JWT、Refresh Cookie 与 CSRF Token。"""
    normalized_email = str(payload.email).strip().lower()
    async with session.begin():
        user = (
            await session.execute(select(User).where(User.email == normalized_email))
        ).scalar_one_or_none()

    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthError(code="INVALID_CREDENTIALS", message="邮箱或密码错误")

    if user.status != "ACTIVE":
        raise AuthError(code="USER_INACTIVE", message="账号已停用，请联系管理员")

    organizations = await list_user_organizations(session, user_id=user.id)
    org_ids = {org.id for org in organizations}

    active_org_id: uuid.UUID | None
    if payload.organization_id is not None:
        if payload.organization_id not in org_ids:
            raise ForbiddenError(
                code="ORG_ACCESS_DENIED",
                message="当前账号不是所选组织的有效成员",
            )
        active_org_id = payload.organization_id
    else:
        active_org_id = organizations[0].id if organizations else None

    raw_refresh = generate_refresh_token()
    refresh_hash = hash_refresh_token(raw_refresh)
    family_id = new_uuid7()
    session_id = new_uuid7()
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=settings.jwt_refresh_token_expire_days)
    client_ip = request.client.host if request.client else None
    user_agent = (request.headers.get("User-Agent") or "")[:512] or None

    async with session.begin():
        user_session = UserSession(
            id=session_id,
            user_id=user.id,
            active_organization_id=active_org_id,
            family_id=family_id,
            refresh_token_hash=refresh_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=client_ip,
        )
        session.add(user_session)

    if active_org_id is not None:
        async with org_rls_session(session, active_org_id, user_id=user.id):
            record_audit_event(
                session,
                organization_id=active_org_id,
                actor_user_id=user.id,
                action="auth.login.succeeded",
                resource_type="user_session",
                resource_id=session_id,
                details={"ip_address": client_ip},
            )

    access_token = create_access_token(
        user.id,
        organization_id=active_org_id,
        extra_claims={"sid": str(session_id)},
        settings=settings,
    )
    csrf_token = generate_csrf_token(session_binding=str(family_id), settings=settings)
    _set_auth_cookies(
        response,
        raw_refresh_token=raw_refresh,
        csrf_token=csrf_token,
        settings=settings,
    )

    return AuthTokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        csrf_token=csrf_token,
        user=UserSummary(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=user.status,
        ),
        organizations=organizations,
        active_organization_id=active_org_id,
    )


async def rotate_refresh_token(
    *,
    request: Request,
    response: Response,
    session: AsyncSession,
    settings: Settings,
) -> AuthTokenResponse:
    """强制校验 CSRF 并执行 Refresh Token Rotation；发现重放立即吊销整个家族并写安全审计。"""
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_refresh:
        raise AuthError(code="REFRESH_TOKEN_MISSING", message="缺少刷新会话 Cookie")

    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
    header_csrf = request.headers.get("X-CSRF-Token")
    if not cookie_csrf or not header_csrf:
        raise ForbiddenError(code="CSRF_MISSING", message="缺少必要的 CSRF 校验令牌")

    refresh_hash = hash_refresh_token(raw_refresh)
    now = datetime.now(UTC)
    client_ip = request.client.host if request.client else None
    user_agent = (request.headers.get("User-Agent") or "")[:512] or None

    async with session.begin():
        existing_session = (
            await session.execute(
                select(UserSession)
                .where(UserSession.refresh_token_hash == refresh_hash)
                .with_for_update()
            )
        ).scalar_one_or_none()

        if existing_session is None:
            _clear_auth_cookies(response, settings=settings)
            raise AuthError(code="INVALID_REFRESH_TOKEN", message="刷新令牌无效，请重新登录")

        # 严格校验双提交 CSRF 与当前会话家族绑定一致
        if not verify_csrf_token(
            header_csrf,
            expected_token=cookie_csrf,
            session_binding=str(existing_session.family_id),
            settings=settings,
        ):
            raise ForbiddenError(code="CSRF_INVALID", message="CSRF 令牌校验失败")

        # 重放攻击检测：凡是已轮换或已被吊销的 Refresh Token 再次被提交，立即整族吊销
        if (
            existing_session.revoked_at is not None
            or existing_session.replaced_by_session_id is not None
        ):
            await session.execute(
                update(UserSession)
                .where(
                    UserSession.family_id == existing_session.family_id,
                    UserSession.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoke_reason="REPLAY_DETECTED")
            )
            replay_user_id = existing_session.user_id
            replay_family_id = existing_session.family_id
            replay_session_id = existing_session.id
            replay_org_id = existing_session.active_organization_id
        else:
            replay_user_id = None
            replay_family_id = None
            replay_session_id = None
            replay_org_id = None

    # 若触发重放攻击：落盘安全审计事件（在 org_rls_session 下）并拒绝请求
    if replay_user_id is not None:
        _clear_auth_cookies(response, settings=settings)
        audit_org_id = replay_org_id
        if audit_org_id is None:
            user_orgs = await list_user_organizations(session, user_id=replay_user_id)
            audit_org_id = user_orgs[0].id if user_orgs else None
        if audit_org_id is not None:
            async with org_rls_session(session, audit_org_id, user_id=replay_user_id):
                record_security_audit_event(
                    session,
                    organization_id=audit_org_id,
                    actor_user_id=replay_user_id,
                    event_type="auth.refresh.replay_detected",
                    severity="CRITICAL",
                    resource_type="user_session_family",
                    resource_id=replay_family_id,
                    ip_address=client_ip,
                    details={
                        "replayed_session_id": str(replay_session_id),
                        "family_id": str(replay_family_id),
                    },
                )
        raise AuthError(
            code="REFRESH_TOKEN_REUSED",
            message="检测到刷新令牌重放攻击，关联会话家族已全部吊销",
        )

    # 非重放路径：校验过期与用户状态，然后执行原子轮换
    if existing_session.expires_at <= now:
        async with session.begin():
            await session.execute(
                update(UserSession)
                .where(UserSession.id == existing_session.id)
                .values(revoked_at=now, revoke_reason="EXPIRED")
            )
        _clear_auth_cookies(response, settings=settings)
        raise AuthError(code="REFRESH_TOKEN_EXPIRED", message="登录会话已过期，请重新登录")

    async with session.begin():
        user = await session.get(User, existing_session.user_id)
    if user is None or user.status != "ACTIVE":
        _clear_auth_cookies(response, settings=settings)
        raise AuthError(code="USER_INACTIVE", message="账号不存在或已停用")

    organizations = await list_user_organizations(session, user_id=user.id)
    valid_org_ids = {org.id for org in organizations}
    active_org_id = existing_session.active_organization_id
    if active_org_id not in valid_org_ids:
        active_org_id = organizations[0].id if organizations else None

    new_raw_refresh = generate_refresh_token()
    new_refresh_hash = hash_refresh_token(new_raw_refresh)
    new_session_id = new_uuid7()
    new_expires_at = now + timedelta(days=settings.jwt_refresh_token_expire_days)

    async with session.begin():
        new_session = UserSession(
            id=new_session_id,
            user_id=user.id,
            active_organization_id=active_org_id,
            family_id=existing_session.family_id,
            refresh_token_hash=new_refresh_hash,
            expires_at=new_expires_at,
            user_agent=user_agent,
            ip_address=client_ip,
        )
        session.add(new_session)
        await session.flush()

        await session.execute(
            update(UserSession)
            .where(UserSession.id == existing_session.id)
            .values(
                revoked_at=now,
                revoke_reason="ROTATED",
                replaced_by_session_id=new_session_id,
            )
        )

    access_token = create_access_token(
        user.id,
        organization_id=active_org_id,
        extra_claims={"sid": str(new_session_id)},
        settings=settings,
    )
    new_csrf_token = generate_csrf_token(
        session_binding=str(existing_session.family_id),
        settings=settings,
    )
    _set_auth_cookies(
        response,
        raw_refresh_token=new_raw_refresh,
        csrf_token=new_csrf_token,
        settings=settings,
    )

    return AuthTokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        csrf_token=new_csrf_token,
        user=UserSummary(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=user.status,
        ),
        organizations=organizations,
        active_organization_id=active_org_id,
    )


async def logout_current_session(
    *,
    request: Request,
    response: Response,
    session: AsyncSession,
    settings: Settings,
    principal: Principal | None = None,
) -> None:
    """吊销当前会话并清除 Refresh / CSRF Cookie。"""
    now = datetime.now(UTC)
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)

    if raw_refresh:
        cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
        header_csrf = request.headers.get("X-CSRF-Token")
        refresh_hash = hash_refresh_token(raw_refresh)

        async with session.begin():
            existing = (
                await session.execute(
                    select(UserSession).where(UserSession.refresh_token_hash == refresh_hash)
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 若存在有效 Cookie，则同步核验 CSRF 防止跨站强制登出
                if (
                    cookie_csrf
                    and header_csrf
                    and not verify_csrf_token(
                        header_csrf,
                        expected_token=cookie_csrf,
                        session_binding=str(existing.family_id),
                        settings=settings,
                    )
                ):
                    raise ForbiddenError(code="CSRF_INVALID", message="CSRF 令牌校验失败")
                if existing.revoked_at is None:
                    existing.revoked_at = now
                    existing.revoke_reason = "LOGOUT"

    if principal is not None and principal.session_id is not None:
        async with session.begin():
            await session.execute(
                update(UserSession)
                .where(
                    UserSession.id == principal.session_id,
                    UserSession.user_id == principal.user_id,
                    UserSession.revoked_at.is_(None),
                )
                .values(revoked_at=now, revoke_reason="LOGOUT")
            )

    _clear_auth_cookies(response, settings=settings)


async def build_auth_me_response(
    *,
    principal: Principal,
    requested_org_id: uuid.UUID | None,
    session: AsyncSession,
) -> AuthMeResponse:
    """查询当前用户全景身份：所属组织、活跃组织、有效部门、SystemRole、OrgRole 与权限集合。"""
    organizations = await list_user_organizations(session, user_id=principal.user_id)
    org_map = {org.id: org for org in organizations}

    active_org_id: uuid.UUID | None = requested_org_id or principal.token_org_id
    if active_org_id is not None and active_org_id not in org_map:
        if requested_org_id is not None:
            raise ForbiddenError(
                code="ORG_ACCESS_DENIED",
                message="当前账号不是请求头所指定组织的有效成员",
            )
        active_org_id = organizations[0].id if organizations else None
    elif active_org_id is None and organizations:
        active_org_id = organizations[0].id

    # 查询平台级 SystemRole 详情
    async with session.begin():
        sys_role_rows = (
            await session.execute(
                select(SystemRole.id, SystemRole.code, SystemRole.name)
                .join(UserSystemRole, UserSystemRole.system_role_id == SystemRole.id)
                .where(
                    UserSystemRole.user_id == principal.user_id,
                    UserSystemRole.status == "ACTIVE",
                )
                .order_by(SystemRole.code.asc())
            )
        ).all()

    system_roles = [
        RoleBrief(id=row.id, code=row.code, name=row.name, scope="SYSTEM")
        for row in sys_role_rows
    ]

    departments: list[DepartmentMembershipBrief] = []
    organization_roles: list[RoleBrief] = []
    effective_permissions: set[str] = set(principal.system_permissions)
    now = datetime.now(UTC)

    if active_org_id is not None:
        async with org_rls_session(session, active_org_id, user_id=principal.user_id):
            dept_rows = (
                await session.execute(
                    select(
                        DepartmentMembership.id.label("membership_id"),
                        Department.id.label("department_id"),
                        Department.name.label("department_name"),
                        DepartmentMembership.membership_type,
                        DepartmentMembership.is_primary,
                        DepartmentMembership.valid_from,
                        DepartmentMembership.valid_until,
                    )
                    .join(
                        Department,
                        and_(
                            Department.id == DepartmentMembership.department_id,
                            Department.organization_id == active_org_id,
                        ),
                    )
                    .where(
                        DepartmentMembership.organization_id == active_org_id,
                        DepartmentMembership.user_id == principal.user_id,
                        DepartmentMembership.status == "ACTIVE",
                        Department.status == "ACTIVE",
                        DepartmentMembership.valid_from <= now,
                        or_(
                            DepartmentMembership.valid_until.is_(None),
                            DepartmentMembership.valid_until > now,
                        ),
                    )
                    .order_by(DepartmentMembership.is_primary.desc(), Department.name.asc())
                )
            ).all()

            departments = [
                DepartmentMembershipBrief(
                    membership_id=row.membership_id,
                    department_id=row.department_id,
                    department_name=row.department_name,
                    membership_type=row.membership_type,
                    is_primary=row.is_primary,
                    valid_from=row.valid_from,
                    valid_until=row.valid_until,
                )
                for row in dept_rows
            ]

            org_role_rows = (
                await session.execute(
                    select(
                        OrganizationRole.id,
                        OrganizationRole.code,
                        OrganizationRole.name,
                    )
                    .join(
                        UserRoleMembership,
                        UserRoleMembership.organization_role_id == OrganizationRole.id,
                    )
                    .where(
                        OrganizationRole.organization_id == active_org_id,
                        OrganizationRole.status == "ACTIVE",
                        UserRoleMembership.organization_id == active_org_id,
                        UserRoleMembership.user_id == principal.user_id,
                        UserRoleMembership.status == "ACTIVE",
                        UserRoleMembership.valid_from <= now,
                        or_(
                            UserRoleMembership.valid_until.is_(None),
                            UserRoleMembership.valid_until > now,
                        ),
                    )
                    .order_by(OrganizationRole.code.asc())
                )
            ).all()

            seen_role_ids: set[uuid.UUID] = set()
            for row in org_role_rows:
                if row.id in seen_role_ids:
                    continue
                seen_role_ids.add(row.id)
                organization_roles.append(
                    RoleBrief(id=row.id, code=row.code, name=row.name, scope="ORGANIZATION")
                )

            if seen_role_ids:
                org_perm_codes = (
                    await session.execute(
                        select(Permission.code)
                        .join(
                            OrganizationRolePermission,
                            OrganizationRolePermission.permission_id == Permission.id,
                        )
                        .where(
                            OrganizationRolePermission.organization_id == active_org_id,
                            OrganizationRolePermission.organization_role_id.in_(seen_role_ids),
                        )
                    )
                ).scalars().all()
                effective_permissions.update(org_perm_codes)

    return AuthMeResponse(
        user=UserSummary(
            id=principal.user_id,
            email=principal.email,
            display_name=principal.display_name,
            status=principal.status,
        ),
        active_organization_id=active_org_id,
        active_organization=org_map.get(active_org_id) if active_org_id else None,
        organizations=organizations,
        departments=departments,
        system_roles=system_roles,
        organization_roles=organization_roles,
        permissions=sorted(effective_permissions),
    )
