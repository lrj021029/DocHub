"""企业组织与成员管理业务服务（全部租户操作强制在 PostgreSQL FORCE RLS 上下文下执行）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.ids import new_uuid7
from app.core.pagination import OffsetPage
from app.core.security import create_access_token, hash_password
from app.db.session import org_rls_session
from app.modules.audit.service import record_audit_event, record_security_audit_event
from app.modules.identity.deps import OrgContext, Principal
from app.modules.identity.models import User, UserSession
from app.modules.identity.service import build_auth_me_response
from app.modules.organizations.models import Organization, OrganizationMember
from app.modules.organizations.schemas import (
    OrganizationCreateRequest,
    OrganizationMemberCreateRequest,
    OrganizationMemberItem,
    OrganizationMemberUpdateRequest,
    OrganizationResponse,
    OrganizationSwitchResponse,
)
from app.modules.roles.catalog import DEFAULT_ORGANIZATION_ROLE_SPECS
from app.modules.roles.models import (
    OrganizationRole,
    OrganizationRolePermission,
    Permission,
)


async def create_organization(
    *,
    payload: OrganizationCreateRequest,
    principal: Principal,
    session: AsyncSession,
) -> OrganizationResponse:
    """创建新的企业组织，加入创建者为有效成员并自动初始化标准组织角色。"""
    normalized_slug = payload.slug.strip().lower()
    org_id = new_uuid7()

    async with session.begin():
        existing = (
            await session.execute(
                select(Organization).where(Organization.slug == normalized_slug)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError(code="ORG_SLUG_EXISTS", message="组织短标识已被占用")

        org = Organization(
            id=org_id,
            name=payload.name.strip(),
            slug=normalized_slug,
            status="ACTIVE",
        )
        session.add(org)
        await session.flush()

        # 读取平台权限映射，准备为新组织初始化默认角色
        perm_rows = (await session.execute(select(Permission))).scalars().all()
        perm_by_code = {p.code: p for p in perm_rows}

    # 在新组织的 RLS 上下文下写入成员、默认角色与审计记录
    async with org_rls_session(session, org_id, user_id=principal.user_id):
        session.add(
            OrganizationMember(
                organization_id=org_id,
                user_id=principal.user_id,
                status="ACTIVE",
            )
        )
        for spec in DEFAULT_ORGANIZATION_ROLE_SPECS:
            role = OrganizationRole(
                organization_id=org_id,
                code=spec.code,
                name=spec.name,
                description=spec.description,
                status="ACTIVE",
            )
            session.add(role)
            await session.flush()
            for p_code in spec.permission_codes:
                perm = perm_by_code.get(p_code)
                if perm is not None and perm.scope == "ORGANIZATION":
                    session.add(
                        OrganizationRolePermission(
                            organization_id=org_id,
                            organization_role_id=role.id,
                            permission_id=perm.id,
                        )
                    )

        record_audit_event(
            session,
            organization_id=org_id,
            actor_user_id=principal.user_id,
            action="org.created",
            resource_type="organization",
            resource_id=org_id,
            details={"slug": normalized_slug, "name": payload.name.strip()},
        )

    return OrganizationResponse(
        id=org.id,
        name=org.name,
        slug=org.slug,
        status=org.status,
        created_at=org.created_at,
    )


async def switch_active_organization(
    *,
    target_org_id: uuid.UUID,
    principal: Principal,
    session: AsyncSession,
    settings: Settings,
) -> OrganizationSwitchResponse:
    """校验当前用户对目标组织的成员资格，更新会话活跃组织并签发新 Access JWT。"""
    # 在目标组织 RLS 上下文下核验有效成员身份，防止越权切换到未加入组织
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
                message="当前账号不是目标组织的有效成员，禁止切换",
            )

    if principal.session_id is not None:
        async with session.begin():
            await session.execute(
                update(UserSession)
                .where(
                    UserSession.id == principal.session_id,
                    UserSession.user_id == principal.user_id,
                    UserSession.revoked_at.is_(None),
                )
                .values(active_organization_id=target_org_id)
            )

    extra_claims = {"sid": str(principal.session_id)} if principal.session_id else None
    new_access_token = create_access_token(
        principal.user_id,
        organization_id=target_org_id,
        extra_claims=extra_claims,
        settings=settings,
    )

    me_snapshot = await build_auth_me_response(
        principal=principal,
        requested_org_id=target_org_id,
        session=session,
    )
    assert me_snapshot.active_organization is not None

    return OrganizationSwitchResponse(
        access_token=new_access_token,
        token_type="bearer",
        expires_in=settings.jwt_access_token_expire_minutes * 60,
        active_organization_id=target_org_id,
        active_organization=me_snapshot.active_organization,
        departments=me_snapshot.departments,
        organization_roles=me_snapshot.organization_roles,
        permissions=me_snapshot.permissions,
    )


async def list_organization_members(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
    q: str | None = None,
    status_filter: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> OffsetPage[OrganizationMemberItem]:
    """分页查询当前组织成员列表（受 FORCE RLS 保护）。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        base_stmt = (
            select(OrganizationMember, User)
            .join(User, User.id == OrganizationMember.user_id)
            .where(OrganizationMember.organization_id == org_ctx.organization_id)
        )
        if status_filter:
            base_stmt = base_stmt.where(OrganizationMember.status == status_filter.strip().upper())
        if q and q.strip():
            pattern = f"%{q.strip()}%"
            base_stmt = base_stmt.where(
                or_(User.email.ilike(pattern), User.display_name.ilike(pattern))
            )

        count_stmt = select(func.count()).select_from(base_stmt.subquery())
        total = int((await session.execute(count_stmt)).scalar_one())

        rows = (
            await session.execute(
                base_stmt.order_by(OrganizationMember.joined_at.asc())
                .offset(offset)
                .limit(limit)
            )
        ).all()

    items = [
        OrganizationMemberItem(
            id=member.id,
            organization_id=member.organization_id,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            user_status=user.status,
            status=member.status,
            joined_at=member.joined_at,
            removed_at=member.removed_at,
        )
        for member, user in rows
    ]
    return OffsetPage.create(items, total=total, offset=offset, limit=limit)


async def add_organization_member(
    *,
    org_ctx: OrgContext,
    payload: OrganizationMemberCreateRequest,
    session: AsyncSession,
) -> OrganizationMemberItem:
    """向当前组织添加成员（支持已有用户直接加入或新用户同步创建）。"""
    normalized_email = str(payload.email).strip().lower()

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        user = (
            await session.execute(select(User).where(User.email == normalized_email))
        ).scalar_one_or_none()

        if user is None:
            if not payload.display_name or not payload.password:
                raise ValidationError(
                    code="NEW_USER_FIELDS_REQUIRED",
                    message="邮箱尚未注册，需提供 display_name 与初始 password 以创建账号",
                )
            user = User(
                email=normalized_email,
                display_name=payload.display_name.strip(),
                password_hash=hash_password(payload.password),
                status="ACTIVE",
            )
            session.add(user)
            await session.flush()

        existing_member = (
            await session.execute(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == org_ctx.organization_id,
                    OrganizationMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()

        if existing_member is not None:
            if existing_member.status == "ACTIVE":
                raise ConflictError(
                    code="MEMBER_ALREADY_EXISTS",
                    message="该用户已是当前组织的有效成员",
                )
            existing_member.status = "ACTIVE"
            existing_member.removed_at = None
            member = existing_member
        else:
            member = OrganizationMember(
                organization_id=org_ctx.organization_id,
                user_id=user.id,
                status="ACTIVE",
            )
            session.add(member)
            await session.flush()

        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="org.member.added",
            resource_type="organization_member",
            resource_id=member.id,
            details={"target_user_id": str(user.id), "target_email": user.email},
        )

    return OrganizationMemberItem(
        id=member.id,
        organization_id=member.organization_id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        user_status=user.status,
        status=member.status,
        joined_at=member.joined_at,
        removed_at=member.removed_at,
    )


async def update_organization_member(
    *,
    org_ctx: OrgContext,
    member_id: uuid.UUID,
    payload: OrganizationMemberUpdateRequest,
    session: AsyncSession,
) -> OrganizationMemberItem:
    """变更组织成员状态（启用 / 停用 / 移除）并记录审计。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        row = (
            await session.execute(
                select(OrganizationMember, User)
                .join(User, User.id == OrganizationMember.user_id)
                .where(
                    OrganizationMember.id == member_id,
                    OrganizationMember.organization_id == org_ctx.organization_id,
                )
            )
        ).one_or_none()

        if row is None:
            raise NotFoundError(code="MEMBER_NOT_FOUND", message="目标组织成员不存在或无权访问")

        member, user = row
        previous_status = member.status
        member.status = payload.status
        member.removed_at = datetime.now(UTC) if payload.status == "REMOVED" else None

        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="org.member.status_updated",
            resource_type="organization_member",
            resource_id=member.id,
            details={
                "target_user_id": str(user.id),
                "from_status": previous_status,
                "to_status": payload.status,
            },
        )
        if payload.status in ("SUSPENDED", "REMOVED"):
            record_security_audit_event(
                session,
                organization_id=org_ctx.organization_id,
                actor_user_id=org_ctx.principal.user_id,
                event_type="org.member.access_revoked",
                severity="HIGH",
                resource_type="organization_member",
                resource_id=member.id,
                details={"target_user_id": str(user.id), "new_status": payload.status},
            )

    return OrganizationMemberItem(
        id=member.id,
        organization_id=member.organization_id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        user_status=user.status,
        status=member.status,
        joined_at=member.joined_at,
        removed_at=member.removed_at,
    )
