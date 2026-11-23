"""Knowledge Space 与私有空间成员管理业务服务（强制运行于 PostgreSQL FORCE RLS 上下文）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.session import org_rls_session
from app.modules.audit.service import record_audit_event
from app.modules.documents.models import Document
from app.modules.identity.deps import OrgContext
from app.modules.identity.models import User
from app.modules.indexing.models import EmbeddingProfile
from app.modules.knowledge_spaces.models import (
    KnowledgeSpace,
    SpaceMembership,
    SpacePolicy,
)
from app.modules.knowledge_spaces.schemas import (
    KnowledgeSpaceCreateRequest,
    KnowledgeSpaceItem,
    KnowledgeSpaceUpdateRequest,
    SpaceMemberItem,
    SpaceMembersPatchRequest,
)
from app.modules.organizations.models import OrganizationMember
from app.modules.outbox.service import enqueue_outbox_event


async def list_spaces(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
) -> list[KnowledgeSpaceItem]:
    """列举当前组织内用户可见的知识空间（管理员见全量，普通成员仅见 PUBLIC 及已加入的 PRIVATE）。"""
    now = datetime.now(UTC)
    can_manage = org_ctx.has_permission("space:manage")

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        spaces = (
            (
                await session.execute(
                    select(KnowledgeSpace)
                    .where(KnowledgeSpace.organization_id == org_ctx.organization_id)
                    .order_by(KnowledgeSpace.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

        if not can_manage:
            my_private_ids = set(
                (
                    await session.execute(
                        select(SpaceMembership.space_id).where(
                            SpaceMembership.organization_id == org_ctx.organization_id,
                            SpaceMembership.user_id == org_ctx.principal.user_id,
                            SpaceMembership.status == "ACTIVE",
                            SpaceMembership.valid_from <= now,
                            or_(
                                SpaceMembership.valid_until.is_(None),
                                SpaceMembership.valid_until > now,
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            spaces = [
                sp
                for sp in spaces
                if sp.status == "ACTIVE" and (sp.visibility == "PUBLIC" or sp.id in my_private_ids)
            ]

        profile_rows = (
            await session.execute(
                select(EmbeddingProfile.id, EmbeddingProfile.code).where(
                    EmbeddingProfile.organization_id == org_ctx.organization_id
                )
            )
        ).all()
        profile_code_map = {row.id: row.code for row in profile_rows}

        member_counts = {
            row[0]: int(row[1])
            for row in (
                await session.execute(
                    select(SpaceMembership.space_id, func.count(SpaceMembership.id))
                    .where(
                        SpaceMembership.organization_id == org_ctx.organization_id,
                        SpaceMembership.status == "ACTIVE",
                        SpaceMembership.valid_from <= now,
                        or_(
                            SpaceMembership.valid_until.is_(None),
                            SpaceMembership.valid_until > now,
                        ),
                    )
                    .group_by(SpaceMembership.space_id)
                )
            ).all()
        }

        doc_counts = {
            row[0]: int(row[1])
            for row in (
                await session.execute(
                    select(Document.space_id, func.count(Document.id))
                    .where(Document.organization_id == org_ctx.organization_id)
                    .group_by(Document.space_id)
                )
            ).all()
        }

    return [
        KnowledgeSpaceItem(
            id=sp.id,
            organization_id=sp.organization_id,
            name=sp.name,
            slug=sp.slug,
            description=sp.description,
            visibility=sp.visibility,  # type: ignore[arg-type]
            answer_policy=sp.answer_policy,  # type: ignore[arg-type]
            default_embedding_profile_id=sp.default_embedding_profile_id,
            default_embedding_profile_code=profile_code_map.get(sp.default_embedding_profile_id)
            if sp.default_embedding_profile_id
            else None,
            status=sp.status,
            config_json=sp.config_json,
            config_version=sp.config_version,
            member_count=member_counts.get(sp.id, 0),
            document_count=doc_counts.get(sp.id, 0),
            created_at=sp.created_at,
        )
        for sp in spaces
    ]


async def create_space(
    *,
    org_ctx: OrgContext,
    payload: KnowledgeSpaceCreateRequest,
    session: AsyncSession,
) -> KnowledgeSpaceItem:
    """创建知识空间、默认策略，并在私有空间自动将创建者加入显式成员。"""
    normalized_slug = payload.slug.strip().lower()

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        dup = (
            await session.execute(
                select(KnowledgeSpace.id).where(
                    KnowledgeSpace.organization_id == org_ctx.organization_id,
                    KnowledgeSpace.slug == normalized_slug,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise ConflictError(
                code="SPACE_SLUG_EXISTS", message="当前组织内已存在相同短标识的空间"
            )

        profile_code: str | None = None
        if payload.default_embedding_profile_id is not None:
            profile = (
                await session.execute(
                    select(EmbeddingProfile).where(
                        EmbeddingProfile.id == payload.default_embedding_profile_id,
                        EmbeddingProfile.organization_id == org_ctx.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if profile is None:
                raise ValidationError(
                    code="EMBEDDING_PROFILE_NOT_FOUND",
                    message="指定的默认 Embedding Profile 不属于当前组织",
                )
            profile_code = profile.code

        space = KnowledgeSpace(
            organization_id=org_ctx.organization_id,
            name=payload.name.strip(),
            slug=normalized_slug,
            description=payload.description.strip() if payload.description else None,
            visibility=payload.visibility,
            answer_policy=payload.answer_policy,
            default_embedding_profile_id=payload.default_embedding_profile_id,
            status="ACTIVE",
            config_json=payload.config_json,
            config_version=1,
        )
        session.add(space)
        await session.flush()

        session.add(
            SpacePolicy(
                organization_id=org_ctx.organization_id,
                space_id=space.id,
                policy_json={
                    "visibility": space.visibility,
                    "answer_policy": space.answer_policy,
                },
                revision=1,
            )
        )

        # 私有空间默认将创建者加入显式成员，避免创建后立即无法进入
        member_count = 0
        if space.visibility == "PRIVATE":
            session.add(
                SpaceMembership(
                    organization_id=org_ctx.organization_id,
                    space_id=space.id,
                    user_id=org_ctx.principal.user_id,
                    role="ADMIN",
                    status="ACTIVE",
                )
            )
            member_count = 1

        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="space.created",
            resource_type="knowledge_space",
            resource_id=space.id,
            details={"slug": space.slug, "visibility": space.visibility},
        )

    return KnowledgeSpaceItem(
        id=space.id,
        organization_id=space.organization_id,
        name=space.name,
        slug=space.slug,
        description=space.description,
        visibility=space.visibility,  # type: ignore[arg-type]
        answer_policy=space.answer_policy,  # type: ignore[arg-type]
        default_embedding_profile_id=space.default_embedding_profile_id,
        default_embedding_profile_code=profile_code,
        status=space.status,
        config_json=space.config_json,
        config_version=space.config_version,
        member_count=member_count,
        document_count=0,
        created_at=space.created_at,
    )


async def update_space(
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
    payload: KnowledgeSpaceUpdateRequest,
    session: AsyncSession,
) -> KnowledgeSpaceItem:
    """更新知识空间配置、递增版本号并同事务发布 `space.policy.changed` Outbox 事件。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        space = (
            await session.execute(
                select(KnowledgeSpace).where(
                    KnowledgeSpace.id == space_id,
                    KnowledgeSpace.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if space is None:
            raise NotFoundError(code="SPACE_NOT_FOUND", message="目标知识空间不存在或无权访问")

        if payload.name is not None:
            space.name = payload.name.strip()
        if payload.description is not None:
            space.description = payload.description.strip()
        if payload.visibility is not None:
            space.visibility = payload.visibility
        if payload.answer_policy is not None:
            space.answer_policy = payload.answer_policy
        if payload.status is not None:
            space.status = payload.status
        if payload.config_json is not None:
            space.config_json = payload.config_json
        if payload.default_embedding_profile_id is not None:
            profile = (
                await session.execute(
                    select(EmbeddingProfile).where(
                        EmbeddingProfile.id == payload.default_embedding_profile_id,
                        EmbeddingProfile.organization_id == org_ctx.organization_id,
                    )
                )
            ).scalar_one_or_none()
            if profile is None:
                raise ValidationError(
                    code="EMBEDDING_PROFILE_NOT_FOUND",
                    message="指定的默认 Embedding Profile 不属于当前组织",
                )
            space.default_embedding_profile_id = payload.default_embedding_profile_id

        space.config_version += 1
        policy = (
            await session.execute(
                select(SpacePolicy).where(
                    SpacePolicy.organization_id == org_ctx.organization_id,
                    SpacePolicy.space_id == space.id,
                )
            )
        ).scalar_one_or_none()
        if policy is not None:
            policy.policy_json = {
                "visibility": space.visibility,
                "answer_policy": space.answer_policy,
                "status": space.status,
            }
            policy.revision += 1

        enqueue_outbox_event(
            session,
            organization_id=org_ctx.organization_id,
            aggregate_type="knowledge_space",
            aggregate_id=space.id,
            aggregate_revision=space.config_version,
            event_type="space.policy.changed",
            payload={
                "space_id": str(space.id),
                "visibility": space.visibility,
                "answer_policy": space.answer_policy,
                "status": space.status,
                "config_version": space.config_version,
            },
        )
        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="space.updated",
            resource_type="knowledge_space",
            resource_id=space.id,
            details={"visibility": space.visibility, "status": space.status},
        )

    spaces = await list_spaces(org_ctx=org_ctx, session=session)
    for item in spaces:
        if item.id == space_id:
            return item
    raise NotFoundError(code="SPACE_NOT_FOUND", message="目标知识空间不存在或无权访问")


async def list_space_members(
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
    session: AsyncSession,
) -> list[SpaceMemberItem]:
    """查询指定空间（受当前组织 RLS 约束）的显式成员列表。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        space = (
            await session.execute(
                select(KnowledgeSpace).where(
                    KnowledgeSpace.id == space_id,
                    KnowledgeSpace.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if space is None:
            raise NotFoundError(code="SPACE_NOT_FOUND", message="目标知识空间不存在或无权访问")

        rows = (
            await session.execute(
                select(SpaceMembership, User)
                .join(User, User.id == SpaceMembership.user_id)
                .where(
                    SpaceMembership.organization_id == org_ctx.organization_id,
                    SpaceMembership.space_id == space_id,
                )
                .order_by(SpaceMembership.created_at.asc())
            )
        ).all()

    return [
        SpaceMemberItem(
            id=membership.id,
            organization_id=membership.organization_id,
            space_id=membership.space_id,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=membership.role,
            status=membership.status,
            valid_from=membership.valid_from,
            valid_until=membership.valid_until,
        )
        for membership, user in rows
    ]


async def patch_space_members(
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
    payload: SpaceMembersPatchRequest,
    session: AsyncSession,
) -> list[SpaceMemberItem]:
    """批量增删改 Private Space 显式成员与有效期，并发布 `space.policy.changed` Outbox 事件。"""
    now = datetime.now(UTC)
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        space = (
            await session.execute(
                select(KnowledgeSpace).where(
                    KnowledgeSpace.id == space_id,
                    KnowledgeSpace.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if space is None:
            raise NotFoundError(code="SPACE_NOT_FOUND", message="目标知识空间不存在或无权访问")

        for entry in payload.upsert:
            org_member = (
                await session.execute(
                    select(OrganizationMember).where(
                        OrganizationMember.organization_id == org_ctx.organization_id,
                        OrganizationMember.user_id == entry.user_id,
                        OrganizationMember.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if org_member is None:
                raise ValidationError(
                    code="USER_NOT_IN_ORGANIZATION",
                    message="只能将当前组织的有效成员加入知识空间",
                )

            valid_from = entry.valid_from or now
            if entry.valid_until is not None and entry.valid_until <= valid_from:
                raise ValidationError(
                    code="INVALID_VALIDITY_RANGE",
                    message="成员到期时间 valid_until 必须晚于生效时间 valid_from",
                )

            existing = (
                await session.execute(
                    select(SpaceMembership).where(
                        SpaceMembership.organization_id == org_ctx.organization_id,
                        SpaceMembership.space_id == space_id,
                        SpaceMembership.user_id == entry.user_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    SpaceMembership(
                        organization_id=org_ctx.organization_id,
                        space_id=space_id,
                        user_id=entry.user_id,
                        role=entry.role,
                        status=entry.status,
                        valid_from=valid_from,
                        valid_until=entry.valid_until,
                    )
                )
            else:
                existing.role = entry.role
                existing.status = entry.status
                existing.valid_from = valid_from
                existing.valid_until = entry.valid_until

        for remove_uid in payload.remove_user_ids:
            existing = (
                await session.execute(
                    select(SpaceMembership).where(
                        SpaceMembership.organization_id == org_ctx.organization_id,
                        SpaceMembership.space_id == space_id,
                        SpaceMembership.user_id == remove_uid,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.status = "REVOKED"

        space.config_version += 1
        enqueue_outbox_event(
            session,
            organization_id=org_ctx.organization_id,
            aggregate_type="knowledge_space",
            aggregate_id=space.id,
            aggregate_revision=space.config_version,
            event_type="space.policy.changed",
            payload={
                "space_id": str(space.id),
                "updated_members": len(payload.upsert),
                "removed_members": len(payload.remove_user_ids),
            },
        )
        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="space.members.updated",
            resource_type="knowledge_space",
            resource_id=space.id,
            details={
                "upserted_count": len(payload.upsert),
                "removed_count": len(payload.remove_user_ids),
            },
        )

    return await list_space_members(org_ctx=org_ctx, space_id=space_id, session=session)
