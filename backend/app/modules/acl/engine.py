"""四维 Document ACL 权威鉴权引擎（AuthoritativeACLEngine）。

安全架构铁律（TECHNICAL_SPEC §6 & ADR-001/002）：
1. 本引擎是 RAG 检索后置复核、FAQ Fast Path 来源鉴权、历史会话重鉴权与引用访问的
   **唯一权威 PostgreSQL 鉴权入口**。
2. 判定顺序严格遵循 Fail-Closed 原则（任意环节缺失或不确定一律拒绝，无 Deny Override）：
   (1) 核验用户为当前 Organization 有效成员（`OrganizationMember.status == 'ACTIVE'`）；
   (2) 核验主功能权限 `document:read`（无 `SYSTEM_ADMIN` 隐式旁路）；
   (3) 核验 Space 处于 `ACTIVE` 且满足可见性入口（`PUBLIC` 对本组织有效成员开放，
       `PRIVATE` 仅对处于有效时间窗口内的 `SpaceMembership` 成员开放）；
   (4) 核验 Document 处于 `ACTIVE`（`QUARANTINED` 与 `ARCHIVED` 一律拒绝）；
   (5) 计算四维 Allow OR：
       `GLOBAL（仅限本 Space）`
       `OR DEPARTMENT（DYNAMIC 查闭包表 / SNAPSHOT 查快照，且部门成员在有效期内）`
       `OR ROLE（有效期内 OrganizationRole）`
       `OR USER（有效期内显式用户授权）`。
3. 预留 `BreakGlassGrant` 校验接口：默认关闭/拒绝，仅当存在 `APPROVED`、未过期且经独立审批人
   核准的显式记录时方可通过，并记录高敏感安全审计事件。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import org_rls_session
from app.modules.acl.models import (
    ACLGrant,
    ACLGrantDepartmentSnapshot,
    BreakGlassGrant,
    DocumentACL,
)
from app.modules.audit.service import record_security_audit_event
from app.modules.departments.models import Department, DepartmentClosure, DepartmentMembership
from app.modules.documents.models import Chunk, Document, DocumentVersion
from app.modules.identity.deps import OrgContext, Principal
from app.modules.knowledge_spaces.models import KnowledgeSpace, SpaceMembership
from app.modules.organizations.models import Organization, OrganizationMember
from app.modules.roles.models import OrganizationRole, UserRoleMembership


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """单个 Space / Document 授权判定结果。"""

    allowed: bool
    reason_code: str
    acl_revision: int | None = None
    via_break_glass: bool = False


@dataclass(frozen=True, slots=True)
class DeniedChunkRecord:
    """被权威 ACL 拒绝的候选 Chunk 审计记录（不含任何文档正文或标题）。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    reason_code: str
    acl_revision: int | None = None


@dataclass(frozen=True, slots=True)
class BatchACLResult:
    """批量 Chunk 权威复核结果。"""

    allowed_chunk_ids: list[uuid.UUID] = field(default_factory=list)
    denied_records: list[DeniedChunkRecord] = field(default_factory=list)
    doc_acl_revisions: dict[uuid.UUID, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentACLProjection:
    """供 Qdrant Payload 预过滤同步使用的轻量 ACL 投影标签。"""

    document_id: uuid.UUID
    acl_revision: int
    acl_global: bool
    acl_dept_ids: list[str]
    acl_role_ids: list[str]
    acl_user_ids: list[str]


@dataclass(frozen=True, slots=True)
class _PrincipalAuthContext:
    """从 PostgreSQL 实时加载的当前用户组织内有效身份与权限快照。"""

    org_active: bool
    has_document_read: bool
    active_space_ids: frozenset[uuid.UUID]
    active_dept_ids: frozenset[uuid.UUID]
    active_role_ids: frozenset[uuid.UUID]


class AuthoritativeACLEngine:
    """PostgreSQL 权威四维 ACL 鉴权引擎。"""

    async def _load_principal_context(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        now: datetime,
        org_ctx: OrgContext | None = None,
    ) -> _PrincipalAuthContext:
        """在当前 org_rls_session 事务内重新核验用户在组织、角色、部门与空间中的有效身份。"""
        # 1. 校验组织与组织成员有效（绝不跳过 Worker 或上游调用方携带的未复核结论）
        org_row = await session.get(Organization, organization_id)
        if org_row is None or org_row.status != "ACTIVE":
            return _PrincipalAuthContext(
                org_active=False,
                has_document_read=False,
                active_space_ids=frozenset(),
                active_dept_ids=frozenset(),
                active_role_ids=frozenset(),
            )

        member = (
            await session.execute(
                select(OrganizationMember.id).where(
                    OrganizationMember.organization_id == organization_id,
                    OrganizationMember.user_id == user_id,
                    OrganizationMember.status == "ACTIVE",
                )
            )
        ).scalar_one_or_none()
        if member is None:
            return _PrincipalAuthContext(
                org_active=False,
                has_document_read=False,
                active_space_ids=frozenset(),
                active_dept_ids=frozenset(),
                active_role_ids=frozenset(),
            )

        # 2. 从数据库实时加载用户在当前组织的有效 OrganizationRole（支持有效期过滤）
        active_role_ids = set(
            (
                await session.execute(
                    select(OrganizationRole.id)
                    .join(
                        UserRoleMembership,
                        UserRoleMembership.organization_role_id == OrganizationRole.id,
                    )
                    .where(
                        OrganizationRole.organization_id == organization_id,
                        OrganizationRole.status == "ACTIVE",
                        UserRoleMembership.organization_id == organization_id,
                        UserRoleMembership.user_id == user_id,
                        UserRoleMembership.status == "ACTIVE",
                        UserRoleMembership.valid_from <= now,
                        or_(
                            UserRoleMembership.valid_until.is_(None),
                            UserRoleMembership.valid_until > now,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

        # 3. 核验是否显式具备 document:read 权限（无 SystemAdmin 隐式旁路）
        has_doc_read = False
        if org_ctx is not None:
            has_doc_read = org_ctx.has_permission("document:read")
        else:
            from app.modules.roles.models import OrganizationRolePermission, Permission

            if active_role_ids:
                perm_hit = (
                    await session.execute(
                        select(Permission.id)
                        .join(
                            OrganizationRolePermission,
                            OrganizationRolePermission.permission_id == Permission.id,
                        )
                        .where(
                            OrganizationRolePermission.organization_id == organization_id,
                            OrganizationRolePermission.organization_role_id.in_(active_role_ids),
                            Permission.code == "document:read",
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                has_doc_read = perm_hit is not None

        # 4. 实时加载当前组织内有效部门归属（过滤撤销与过期兼职）
        active_dept_ids = set(
            (
                await session.execute(
                    select(DepartmentMembership.department_id)
                    .join(
                        Department,
                        and_(
                            Department.id == DepartmentMembership.department_id,
                            Department.organization_id == organization_id,
                        ),
                    )
                    .where(
                        DepartmentMembership.organization_id == organization_id,
                        DepartmentMembership.user_id == user_id,
                        DepartmentMembership.status == "ACTIVE",
                        Department.status == "ACTIVE",
                        DepartmentMembership.valid_from <= now,
                        or_(
                            DepartmentMembership.valid_until.is_(None),
                            DepartmentMembership.valid_until > now,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

        # 5. 实时加载全部处于 ACTIVE 且用户满足入口条件的 KnowledgeSpace
        all_active_spaces = (
            await session.execute(
                select(KnowledgeSpace.id, KnowledgeSpace.visibility).where(
                    KnowledgeSpace.organization_id == organization_id,
                    KnowledgeSpace.status == "ACTIVE",
                )
            )
        ).all()

        private_member_space_ids = set(
            (
                await session.execute(
                    select(SpaceMembership.space_id).where(
                        SpaceMembership.organization_id == organization_id,
                        SpaceMembership.user_id == user_id,
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

        allowed_spaces = {
            row.id
            for row in all_active_spaces
            if row.visibility == "PUBLIC" or row.id in private_member_space_ids
        }

        return _PrincipalAuthContext(
            org_active=True,
            has_document_read=has_doc_read,
            active_space_ids=frozenset(allowed_spaces),
            active_dept_ids=frozenset(active_dept_ids),
            active_role_ids=frozenset(active_role_ids),
        )

    async def _evaluate_break_glass(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        now: datetime,
    ) -> bool:
        """检查是否存在显式审批通过且未过期的紧急访问授权（默认拒绝）。"""
        scope_target = f"document:{document_id}"
        grant = (
            await session.execute(
                select(BreakGlassGrant).where(
                    BreakGlassGrant.organization_id == organization_id,
                    BreakGlassGrant.user_id == user_id,
                    BreakGlassGrant.scope == scope_target,
                    BreakGlassGrant.status == "APPROVED",
                    BreakGlassGrant.approved_by.is_not(None),
                    BreakGlassGrant.approved_by != user_id,
                    BreakGlassGrant.expires_at > now,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            return False

        record_security_audit_event(
            session,
            organization_id=organization_id,
            actor_user_id=user_id,
            event_type="acl.break_glass.used",
            severity="CRITICAL",
            resource_type="document",
            resource_id=document_id,
            details={"break_glass_grant_id": str(grant.id), "reason": grant.reason},
        )
        return True

    async def _evaluate_document_grants(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        auth_ctx: _PrincipalAuthContext,
        doc_rows: list[tuple[uuid.UUID, uuid.UUID, str, uuid.UUID | None, int | None]],
        now: datetime,
    ) -> dict[uuid.UUID, AccessDecision]:
        """批量计算一组文档的四维 Allow OR 授权结论。"""
        decisions: dict[uuid.UUID, AccessDecision] = {}
        if not doc_rows:
            return decisions

        if not auth_ctx.org_active:
            for doc_id, _, _, _, acl_rev in doc_rows:
                decisions[doc_id] = AccessDecision(
                    allowed=False,
                    reason_code="ORG_MEMBERSHIP_INACTIVE",
                    acl_revision=acl_rev,
                )
            return decisions

        if not auth_ctx.has_document_read:
            for doc_id, _, _, _, acl_rev in doc_rows:
                decisions[doc_id] = AccessDecision(
                    allowed=False,
                    reason_code="MISSING_DOCUMENT_READ_PERMISSION",
                    acl_revision=acl_rev,
                )
            return decisions

        # 预筛 Space 入口与 Document 状态（QUARANTINED / ARCHIVED 直接拒绝）
        acl_id_to_doc_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
        doc_revision_map: dict[uuid.UUID, int | None] = {}
        candidate_doc_ids: list[uuid.UUID] = []

        for doc_id, space_id, doc_status, acl_id, acl_rev in doc_rows:
            doc_revision_map[doc_id] = acl_rev
            if space_id not in auth_ctx.active_space_ids:
                decisions[doc_id] = AccessDecision(
                    allowed=False,
                    reason_code="SPACE_ACCESS_DENIED",
                    acl_revision=acl_rev,
                )
                continue
            if doc_status != "ACTIVE":
                decisions[doc_id] = AccessDecision(
                    allowed=False,
                    reason_code=f"DOCUMENT_NOT_ACTIVE:{doc_status}",
                    acl_revision=acl_rev,
                )
                continue
            if acl_id is None or acl_rev is None or acl_rev <= 0:
                decisions[doc_id] = AccessDecision(
                    allowed=False,
                    reason_code="ACL_NOT_CONFIGURED",
                    acl_revision=acl_rev,
                )
                continue
            acl_id_to_doc_ids.setdefault(acl_id, []).append(doc_id)
            candidate_doc_ids.append(doc_id)

        if not acl_id_to_doc_ids:
            return decisions

        # 查询有效时间窗口内的全部 ACLGrant
        active_grants = (
            (
                await session.execute(
                    select(ACLGrant).where(
                        ACLGrant.organization_id == organization_id,
                        ACLGrant.acl_id.in_(list(acl_id_to_doc_ids.keys())),
                        ACLGrant.valid_from <= now,
                        or_(ACLGrant.valid_until.is_(None), ACLGrant.valid_until > now),
                    )
                )
            )
            .scalars()
            .all()
        )

        # 如存在 DYNAMIC 部门授权且用户属于至少一个部门，查其祖先部门集合（实时闭包）
        user_dynamic_ancestor_depts: set[uuid.UUID] = set(auth_ctx.active_dept_ids)
        has_dynamic_dept_grant = any(
            g.subject_type == "DEPARTMENT" and g.inheritance_mode == "DYNAMIC"
            for g in active_grants
        )
        if has_dynamic_dept_grant and auth_ctx.active_dept_ids:
            ancestor_rows = (
                (
                    await session.execute(
                        select(DepartmentClosure.ancestor_id).where(
                            DepartmentClosure.organization_id == organization_id,
                            DepartmentClosure.descendant_id.in_(list(auth_ctx.active_dept_ids)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            user_dynamic_ancestor_depts.update(ancestor_rows)

        # 如存在 SNAPSHOT 部门授权，批量加载快照表（与当前最新部门移动彻底隔离）
        snapshot_grant_ids = [
            g.id
            for g in active_grants
            if g.subject_type == "DEPARTMENT" and g.inheritance_mode == "SNAPSHOT"
        ]
        snapshot_depts_by_grant: dict[uuid.UUID, set[uuid.UUID]] = {}
        if snapshot_grant_ids:
            snap_rows = (
                await session.execute(
                    select(
                        ACLGrantDepartmentSnapshot.acl_grant_id,
                        ACLGrantDepartmentSnapshot.department_id,
                    ).where(
                        ACLGrantDepartmentSnapshot.organization_id == organization_id,
                        ACLGrantDepartmentSnapshot.acl_grant_id.in_(snapshot_grant_ids),
                    )
                )
            ).all()
            for grant_id, dept_id in snap_rows:
                snapshot_depts_by_grant.setdefault(grant_id, set()).add(dept_id)

        # 逐条计算四维 Allow OR
        allowed_acl_ids: set[uuid.UUID] = set()
        for grant in active_grants:
            if grant.acl_id in allowed_acl_ids:
                continue

            if grant.subject_type == "GLOBAL":
                # GLOBAL 仅表示当前 Space 内全部已满足 Space 入口条件的成员可读
                allowed_acl_ids.add(grant.acl_id)
            elif grant.subject_type == "USER":
                if grant.subject_id == user_id:
                    allowed_acl_ids.add(grant.acl_id)
            elif grant.subject_type == "ROLE":
                if grant.subject_id in auth_ctx.active_role_ids:
                    allowed_acl_ids.add(grant.acl_id)
            elif grant.subject_type == "DEPARTMENT" and grant.subject_id is not None:
                if not auth_ctx.active_dept_ids:
                    continue
                if grant.inheritance_mode == "DYNAMIC":
                    if grant.include_descendants:
                        if grant.subject_id in user_dynamic_ancestor_depts:
                            allowed_acl_ids.add(grant.acl_id)
                    elif grant.subject_id in auth_ctx.active_dept_ids:
                        allowed_acl_ids.add(grant.acl_id)
                elif grant.inheritance_mode == "SNAPSHOT":
                    snap_depts = snapshot_depts_by_grant.get(grant.id, set())
                    if auth_ctx.active_dept_ids & snap_depts:
                        allowed_acl_ids.add(grant.acl_id)

        for acl_id, linked_doc_ids in acl_id_to_doc_ids.items():
            is_allowed = acl_id in allowed_acl_ids
            for doc_id in linked_doc_ids:
                rev = doc_revision_map.get(doc_id)
                if is_allowed:
                    decisions[doc_id] = AccessDecision(
                        allowed=True,
                        reason_code="ACL_GRANTED",
                        acl_revision=rev,
                    )
                else:
                    # 尝试 Break-glass 兜底（默认关闭/拒绝，仅审批通过且未过期方可通过）
                    via_bg = await self._evaluate_break_glass(
                        session,
                        organization_id=organization_id,
                        user_id=user_id,
                        document_id=doc_id,
                        now=now,
                    )
                    if via_bg:
                        decisions[doc_id] = AccessDecision(
                            allowed=True,
                            reason_code="BREAK_GLASS_APPROVED",
                            acl_revision=rev,
                            via_break_glass=True,
                        )
                    else:
                        decisions[doc_id] = AccessDecision(
                            allowed=False,
                            reason_code="ACL_DENIED",
                            acl_revision=rev,
                        )

        return decisions

    async def check_space_access(
        self,
        session: AsyncSession,
        principal: Principal | OrgContext,
        space_id: uuid.UUID,
        *,
        organization_id: uuid.UUID | None = None,
    ) -> AccessDecision:
        """校验用户是否有权进入指定 Knowledge Space（严格区分 PUBLIC / PRIVATE）。"""
        org_ctx = principal if isinstance(principal, OrgContext) else None
        user_id = org_ctx.principal.user_id if org_ctx else principal.user_id
        target_org_id = (
            org_ctx.organization_id if org_ctx else (organization_id or principal.token_org_id)
        )
        if target_org_id is None:
            return AccessDecision(allowed=False, reason_code="ORG_CONTEXT_REQUIRED")

        now = datetime.now(UTC)
        async with org_rls_session(session, target_org_id, user_id=user_id):
            auth_ctx = await self._load_principal_context(
                session,
                organization_id=target_org_id,
                user_id=user_id,
                now=now,
                org_ctx=org_ctx,
            )
            if not auth_ctx.org_active:
                return AccessDecision(allowed=False, reason_code="ORG_MEMBERSHIP_INACTIVE")

            space = (
                await session.execute(
                    select(KnowledgeSpace).where(
                        KnowledgeSpace.id == space_id,
                        KnowledgeSpace.organization_id == target_org_id,
                    )
                )
            ).scalar_one_or_none()
            if space is None or space.status != "ACTIVE":
                return AccessDecision(allowed=False, reason_code="SPACE_NOT_FOUND_OR_INACTIVE")

            if space_id in auth_ctx.active_space_ids:
                return AccessDecision(allowed=True, reason_code="SPACE_ACCESS_GRANTED")
            return AccessDecision(allowed=False, reason_code="SPACE_MEMBERSHIP_REQUIRED")

    async def check_document_access(
        self,
        session: AsyncSession,
        principal: Principal | OrgContext,
        document_id: uuid.UUID,
        *,
        organization_id: uuid.UUID | None = None,
    ) -> AccessDecision:
        """校验用户对单篇文档的权威读取权限（Space + Document Status + 四维 ACL）。"""
        org_ctx = principal if isinstance(principal, OrgContext) else None
        user_id = org_ctx.principal.user_id if org_ctx else principal.user_id
        target_org_id = (
            org_ctx.organization_id if org_ctx else (organization_id or principal.token_org_id)
        )
        if target_org_id is None:
            return AccessDecision(allowed=False, reason_code="ORG_CONTEXT_REQUIRED")

        now = datetime.now(UTC)
        async with org_rls_session(session, target_org_id, user_id=user_id):
            auth_ctx = await self._load_principal_context(
                session,
                organization_id=target_org_id,
                user_id=user_id,
                now=now,
                org_ctx=org_ctx,
            )
            row = (
                await session.execute(
                    select(
                        Document.id,
                        Document.space_id,
                        Document.status,
                        DocumentACL.id,
                        DocumentACL.revision,
                    )
                    .outerjoin(
                        DocumentACL,
                        and_(
                            DocumentACL.document_id == Document.id,
                            DocumentACL.organization_id == target_org_id,
                        ),
                    )
                    .where(
                        Document.id == document_id,
                        Document.organization_id == target_org_id,
                    )
                )
            ).one_or_none()

            if row is None:
                return AccessDecision(allowed=False, reason_code="DOCUMENT_NOT_FOUND")

            decisions = await self._evaluate_document_grants(
                session,
                organization_id=target_org_id,
                user_id=user_id,
                auth_ctx=auth_ctx,
                doc_rows=[(row[0], row[1], row[2], row[3], row[4])],
                now=now,
            )
            return decisions[document_id]

    async def batch_authorize_chunks(
        self,
        session: AsyncSession,
        principal: Principal | OrgContext,
        chunk_ids: list[uuid.UUID],
        *,
        organization_id: uuid.UUID | None = None,
    ) -> BatchACLResult:
        """一次性批量复核候选 Chunk IDs 的权威读取权限（RAG 召回后、Reranker/LLM 前强制调用）。

        特性：
        - 保持输入顺序或确定性过滤；对不存在、跨组织、Quarantine 或无权 Chunk 一律拒之门外。
        - 返回 `BatchACLResult(allowed_chunk_ids, denied_records, doc_acl_revisions)`。
        """
        if not chunk_ids:
            return BatchACLResult()

        org_ctx = principal if isinstance(principal, OrgContext) else None
        user_id = org_ctx.principal.user_id if org_ctx else principal.user_id
        target_org_id = (
            org_ctx.organization_id if org_ctx else (organization_id or principal.token_org_id)
        )
        if target_org_id is None:
            return BatchACLResult(
                allowed_chunk_ids=[],
                denied_records=[
                    DeniedChunkRecord(
                        chunk_id=cid,
                        document_id=uuid.UUID(int=0),
                        reason_code="ORG_CONTEXT_REQUIRED",
                    )
                    for cid in chunk_ids
                ],
            )

        now = datetime.now(UTC)
        unique_chunk_ids = list(dict.fromkeys(chunk_ids))

        async with org_rls_session(session, target_org_id, user_id=user_id):
            auth_ctx = await self._load_principal_context(
                session,
                organization_id=target_org_id,
                user_id=user_id,
                now=now,
                org_ctx=org_ctx,
            )

            rows = (
                await session.execute(
                    select(
                        Chunk.id,
                        Document.id,
                        Document.space_id,
                        Document.status,
                        DocumentACL.id,
                        DocumentACL.revision,
                    )
                    .join(
                        DocumentVersion,
                        and_(
                            DocumentVersion.id == Chunk.document_version_id,
                            DocumentVersion.organization_id == target_org_id,
                        ),
                    )
                    .join(
                        Document,
                        and_(
                            Document.id == DocumentVersion.document_id,
                            Document.organization_id == target_org_id,
                        ),
                    )
                    .outerjoin(
                        DocumentACL,
                        and_(
                            DocumentACL.document_id == Document.id,
                            DocumentACL.organization_id == target_org_id,
                        ),
                    )
                    .where(
                        Chunk.organization_id == target_org_id,
                        Chunk.id.in_(unique_chunk_ids),
                    )
                )
            ).all()

            chunk_to_doc: dict[uuid.UUID, uuid.UUID] = {}
            unique_doc_rows: dict[
                uuid.UUID, tuple[uuid.UUID, uuid.UUID, str, uuid.UUID | None, int | None]
            ] = {}
            for chunk_id, doc_id, space_id, doc_status, acl_id, acl_rev in rows:
                chunk_to_doc[chunk_id] = doc_id
                unique_doc_rows[doc_id] = (doc_id, space_id, doc_status, acl_id, acl_rev)

            doc_decisions = await self._evaluate_document_grants(
                session,
                organization_id=target_org_id,
                user_id=user_id,
                auth_ctx=auth_ctx,
                doc_rows=list(unique_doc_rows.values()),
                now=now,
            )

            allowed_chunk_ids: list[uuid.UUID] = []
            denied_records: list[DeniedChunkRecord] = []
            doc_acl_revisions: dict[uuid.UUID, int] = {}

            for chunk_id in chunk_ids:
                doc_id = chunk_to_doc.get(chunk_id)
                if doc_id is None:
                    denied_records.append(
                        DeniedChunkRecord(
                            chunk_id=chunk_id,
                            document_id=uuid.UUID(int=0),
                            reason_code="CHUNK_NOT_FOUND_OR_CROSS_TENANT",
                        )
                    )
                    continue

                decision = doc_decisions[doc_id]
                if decision.allowed:
                    allowed_chunk_ids.append(chunk_id)
                    if decision.acl_revision is not None:
                        doc_acl_revisions[doc_id] = decision.acl_revision
                else:
                    denied_records.append(
                        DeniedChunkRecord(
                            chunk_id=chunk_id,
                            document_id=doc_id,
                            reason_code=decision.reason_code,
                            acl_revision=decision.acl_revision,
                        )
                    )

            # 若存在明确权限拒绝，记录安全审计聚合事件（仅记录拒绝计数与文档 ID，严禁记标题/正文）
            if denied_records:
                record_security_audit_event(
                    session,
                    organization_id=target_org_id,
                    actor_user_id=user_id,
                    event_type="acl.batch_check.denied",
                    severity="LOW",
                    resource_type="document_chunks",
                    resource_id=str(len(denied_records)),
                    details={
                        "denied_count": len(denied_records),
                        "allowed_count": len(allowed_chunk_ids),
                    },
                )

        return BatchACLResult(
            allowed_chunk_ids=allowed_chunk_ids,
            denied_records=denied_records,
            doc_acl_revisions=doc_acl_revisions,
        )

    async def get_document_acl_projection(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> DocumentACLProjection:
        """生成供 Qdrant Payload 预过滤异步同步使用的轻量 ACL 标签投影。"""
        now = datetime.now(UTC)
        async with org_rls_session(session, organization_id):
            acl = (
                await session.execute(
                    select(DocumentACL).where(
                        DocumentACL.organization_id == organization_id,
                        DocumentACL.document_id == document_id,
                    )
                )
            ).scalar_one_or_none()
            if acl is None:
                return DocumentACLProjection(
                    document_id=document_id,
                    acl_revision=0,
                    acl_global=False,
                    acl_dept_ids=[],
                    acl_role_ids=[],
                    acl_user_ids=[],
                )

            grants = (
                (
                    await session.execute(
                        select(ACLGrant).where(
                            ACLGrant.organization_id == organization_id,
                            ACLGrant.acl_id == acl.id,
                            ACLGrant.valid_from <= now,
                            or_(ACLGrant.valid_until.is_(None), ACLGrant.valid_until > now),
                        )
                    )
                )
                .scalars()
                .all()
            )

            acl_global = False
            dept_ids: set[str] = set()
            role_ids: set[str] = set()
            user_ids: set[str] = set()

            for grant in grants:
                if grant.subject_type == "GLOBAL":
                    acl_global = True
                elif grant.subject_type == "ROLE" and grant.subject_id is not None:
                    role_ids.add(str(grant.subject_id))
                elif grant.subject_type == "USER" and grant.subject_id is not None:
                    user_ids.add(str(grant.subject_id))
                elif grant.subject_type == "DEPARTMENT" and grant.subject_id is not None:
                    if grant.inheritance_mode == "SNAPSHOT":
                        snap_ids = (
                            (
                                await session.execute(
                                    select(ACLGrantDepartmentSnapshot.department_id).where(
                                        ACLGrantDepartmentSnapshot.organization_id
                                        == organization_id,
                                        ACLGrantDepartmentSnapshot.acl_grant_id == grant.id,
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        dept_ids.update(str(did) for did in snap_ids)
                    elif grant.include_descendants:
                        desc_ids = (
                            (
                                await session.execute(
                                    select(DepartmentClosure.descendant_id).where(
                                        DepartmentClosure.organization_id == organization_id,
                                        DepartmentClosure.ancestor_id == grant.subject_id,
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        dept_ids.update(str(did) for did in desc_ids)
                    else:
                        dept_ids.add(str(grant.subject_id))

            return DocumentACLProjection(
                document_id=document_id,
                acl_revision=acl.revision,
                acl_global=acl_global,
                acl_dept_ids=sorted(dept_ids),
                acl_role_ids=sorted(role_ids),
                acl_user_ids=sorted(user_ids),
            )
