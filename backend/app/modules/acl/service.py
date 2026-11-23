"""Document ACL 配置、快照展开、乐观锁版本控制与不可变修订历史服务。

遵循 ADR-001 裁决：
- 唯一写入口为 `POST /api/v1/documents/{document_id}/acl/revisions`。
- 事务内严格执行：
  1. `SELECT ... FOR UPDATE` 锁定 `document_acls`；
  2. 校验 `expected_revision == acl.revision`（冲突返回 409 ConflictError）；
  3. 校验 `Idempotency-Key` 幂等重放；
  4. 校验所有 DEPARTMENT / ROLE / USER 主体归属于当前 Organization（防跨组织引用）；
  5. 为 `SNAPSHOT` 部门授权自动展开并写入 `acl_grant_department_snapshots`；
  6. 写入不可变 `acl_revisions`（含快照与 Diff）；
  7. 同事务写入 `acl.revision.created` `OutboxEvent` 与 `SecurityAuditEvent`。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.session import org_rls_session
from app.modules.acl.models import (
    ACLGrant,
    ACLGrantDepartmentSnapshot,
    ACLRevision,
    DocumentACL,
)
from app.modules.acl.schemas import (
    ACLGrantInput,
    ACLGrantItem,
    ACLRevisionCreateRequest,
    ACLRevisionItem,
    DocumentACLResponse,
)
from app.modules.audit.service import record_security_audit_event
from app.modules.departments.models import Department, DepartmentClosure
from app.modules.documents.models import Document
from app.modules.identity.deps import OrgContext
from app.modules.identity.models import User
from app.modules.organizations.models import OrganizationMember
from app.modules.outbox.service import (
    enqueue_outbox_event,
    lookup_idempotency_record,
    save_idempotency_record,
)
from app.modules.roles.models import OrganizationRole


def _serialize_grant_for_snapshot(
    grant: ACLGrant, snapshot_dept_ids: list[uuid.UUID]
) -> dict[str, Any]:
    """序列化单条授权记录用于不可变 ACLRevision 快照与 Diff 对比。"""
    return {
        "subject_type": grant.subject_type,
        "subject_id": str(grant.subject_id) if grant.subject_id else None,
        "include_descendants": grant.include_descendants,
        "inheritance_mode": grant.inheritance_mode,
        "snapshot_department_ids": sorted(str(d) for d in snapshot_dept_ids),
        "valid_from": grant.valid_from.isoformat(),
        "valid_until": grant.valid_until.isoformat() if grant.valid_until else None,
    }


def _grant_identity_key(entry: dict[str, Any]) -> str:
    """生成授权项逻辑对比键用于计算结构化 Diff。"""
    return (
        f"{entry.get('subject_type')}|{entry.get('subject_id')}|"
        f"{entry.get('include_descendants')}|{entry.get('inheritance_mode')}|"
        f"{entry.get('valid_until')}"
    )


def _compute_acl_diff(
    previous_snapshot: list[dict[str, Any]],
    next_snapshot: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算前后两版 ACL 授权项的增删 Diff。"""
    prev_map = {_grant_identity_key(item): item for item in previous_snapshot}
    next_map = {_grant_identity_key(item): item for item in next_snapshot}

    added = [next_map[k] for k in sorted(next_map.keys() - prev_map.keys())]
    removed = [prev_map[k] for k in sorted(prev_map.keys() - next_map.keys())]
    return {
        "added": added,
        "removed": removed,
        "previous_count": len(previous_snapshot),
        "current_count": len(next_snapshot),
    }


async def _load_document_acl_response_in_tx(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    doc: Document,
    acl: DocumentACL,
) -> DocumentACLResponse:
    """在已开启的 org_rls_session 内组装完整的 DocumentACLResponse（含主体标签与快照）。"""
    grants = (
        (
            await session.execute(
                select(ACLGrant)
                .where(
                    ACLGrant.organization_id == organization_id,
                    ACLGrant.acl_id == acl.id,
                )
                .order_by(ACLGrant.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    dept_ids = [g.subject_id for g in grants if g.subject_type == "DEPARTMENT" and g.subject_id]
    role_ids = [g.subject_id for g in grants if g.subject_type == "ROLE" and g.subject_id]
    user_ids = [g.subject_id for g in grants if g.subject_type == "USER" and g.subject_id]
    snap_grant_ids = [
        g.id for g in grants if g.subject_type == "DEPARTMENT" and g.inheritance_mode == "SNAPSHOT"
    ]

    dept_labels: dict[uuid.UUID, str] = {}
    if dept_ids:
        for did, dname in (
            await session.execute(
                select(Department.id, Department.name).where(
                    Department.organization_id == organization_id,
                    Department.id.in_(dept_ids),
                )
            )
        ).all():
            dept_labels[did] = dname

    role_labels: dict[uuid.UUID, str] = {}
    if role_ids:
        for rid, rname, rcode in (
            await session.execute(
                select(OrganizationRole.id, OrganizationRole.name, OrganizationRole.code).where(
                    OrganizationRole.organization_id == organization_id,
                    OrganizationRole.id.in_(role_ids),
                )
            )
        ).all():
            role_labels[rid] = f"{rname} ({rcode})"

    user_labels: dict[uuid.UUID, str] = {}
    if user_ids:
        for uid, uemail, uname in (
            await session.execute(
                select(User.id, User.email, User.display_name).where(User.id.in_(user_ids))
            )
        ).all():
            user_labels[uid] = f"{uname} <{uemail}>"

    snap_map: dict[uuid.UUID, list[uuid.UUID]] = {}
    if snap_grant_ids:
        for gid, did in (
            await session.execute(
                select(
                    ACLGrantDepartmentSnapshot.acl_grant_id,
                    ACLGrantDepartmentSnapshot.department_id,
                ).where(
                    ACLGrantDepartmentSnapshot.organization_id == organization_id,
                    ACLGrantDepartmentSnapshot.acl_grant_id.in_(snap_grant_ids),
                )
            )
        ).all():
            snap_map.setdefault(gid, []).append(did)

    grant_items: list[ACLGrantItem] = []
    for g in grants:
        label: str | None = None
        if g.subject_type == "GLOBAL":
            label = "当前空间全体成员（仅限本 Knowledge Space）"
        elif g.subject_type == "DEPARTMENT" and g.subject_id:
            label = dept_labels.get(g.subject_id)
        elif g.subject_type == "ROLE" and g.subject_id:
            label = role_labels.get(g.subject_id)
        elif g.subject_type == "USER" and g.subject_id:
            label = user_labels.get(g.subject_id)

        grant_items.append(
            ACLGrantItem(
                id=g.id,
                subject_type=g.subject_type,
                subject_id=g.subject_id,
                subject_label=label,
                include_descendants=g.include_descendants,
                inheritance_mode=g.inheritance_mode,
                snapshot_department_ids=sorted(snap_map.get(g.id, [])),
                valid_from=g.valid_from,
                valid_until=g.valid_until,
            )
        )

    return DocumentACLResponse(
        acl_id=acl.id,
        organization_id=acl.organization_id,
        document_id=doc.id,
        document_title=doc.title,
        document_status=doc.status,
        revision=acl.revision,
        status=acl.status,
        grants=grant_items,
    )


async def get_document_acl(
    *,
    org_ctx: OrgContext,
    document_id: uuid.UUID,
    session: AsyncSession,
) -> DocumentACLResponse:
    """获取文档当前四维 ACL 授权详情。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        doc = (
            await session.execute(
                select(Document).where(
                    Document.id == document_id,
                    Document.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="目标文档不存在或无权访问")

        acl = (
            await session.execute(
                select(DocumentACL).where(
                    DocumentACL.organization_id == org_ctx.organization_id,
                    DocumentACL.document_id == document_id,
                )
            )
        ).scalar_one_or_none()
        if acl is None:
            acl = DocumentACL(
                organization_id=org_ctx.organization_id,
                document_id=document_id,
                revision=0,
                status="ACTIVE",
            )
            session.add(acl)
            await session.flush()

        return await _load_document_acl_response_in_tx(
            session,
            organization_id=org_ctx.organization_id,
            doc=doc,
            acl=acl,
        )


async def list_document_acl_revisions(
    *,
    org_ctx: OrgContext,
    document_id: uuid.UUID,
    session: AsyncSession,
) -> list[ACLRevisionItem]:
    """查询文档不可变 ACL 修订历史时间线（最新修订在前）。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        doc = (
            await session.execute(
                select(Document.id).where(
                    Document.id == document_id,
                    Document.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="目标文档不存在或无权访问")

        acl = (
            await session.execute(
                select(DocumentACL).where(
                    DocumentACL.organization_id == org_ctx.organization_id,
                    DocumentACL.document_id == document_id,
                )
            )
        ).scalar_one_or_none()
        if acl is None:
            return []

        rows = (
            await session.execute(
                select(ACLRevision, User.email)
                .outerjoin(User, User.id == ACLRevision.changed_by)
                .where(
                    ACLRevision.organization_id == org_ctx.organization_id,
                    ACLRevision.acl_id == acl.id,
                )
                .order_by(ACLRevision.revision.desc())
            )
        ).all()

    return [
        ACLRevisionItem(
            id=rev.id,
            acl_id=rev.acl_id,
            revision=rev.revision,
            reason=rev.reason,
            changed_by=rev.changed_by,
            changed_by_email=actor_email,
            snapshot_json=rev.snapshot_json,
            diff_json=rev.diff_json,
            created_at=rev.created_at,
        )
        for rev, actor_email in rows
    ]


async def _validate_acl_grants_in_org(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    grants: list[ACLGrantInput],
) -> None:
    """严格校验所有授权主体 UUID 均真实隶属当前组织（杜绝跨租户引用或 SystemRole 混入）。"""
    dept_ids = {g.subject_id for g in grants if g.subject_type == "DEPARTMENT" and g.subject_id}
    role_ids = {g.subject_id for g in grants if g.subject_type == "ROLE" and g.subject_id}
    user_ids = {g.subject_id for g in grants if g.subject_type == "USER" and g.subject_id}

    if dept_ids:
        valid_depts = set(
            (
                await session.execute(
                    select(Department.id).where(
                        Department.organization_id == organization_id,
                        Department.id.in_(dept_ids),
                        Department.status == "ACTIVE",
                    )
                )
            )
            .scalars()
            .all()
        )
        if valid_depts != dept_ids:
            raise ValidationError(
                code="INVALID_ACL_DEPARTMENT",
                message="部分部门 ID 不属于当前组织或已归档",
            )

    if role_ids:
        valid_roles = set(
            (
                await session.execute(
                    select(OrganizationRole.id).where(
                        OrganizationRole.organization_id == organization_id,
                        OrganizationRole.id.in_(role_ids),
                        OrganizationRole.status == "ACTIVE",
                    )
                )
            )
            .scalars()
            .all()
        )
        if valid_roles != role_ids:
            raise ValidationError(
                code="INVALID_ACL_ROLE",
                message="部分角色 ID 不属于当前组织的 OrganizationRole（严禁传入 SystemRole）",
            )

    if user_ids:
        valid_users = set(
            (
                await session.execute(
                    select(OrganizationMember.user_id).where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.user_id.in_(user_ids),
                        OrganizationMember.status == "ACTIVE",
                    )
                )
            )
            .scalars()
            .all()
        )
        if valid_users != user_ids:
            raise ValidationError(
                code="INVALID_ACL_USER",
                message="部分用户 ID 不是当前组织的有效成员",
            )


async def create_document_acl_revision(
    *,
    org_ctx: OrgContext,
    document_id: uuid.UUID,
    payload: ACLRevisionCreateRequest,
    idempotency_key: str,
    session: AsyncSession,
) -> DocumentACLResponse:
    """在带行锁与乐观锁的 RLS 事务中提交四维 ACL 变更、快照、Diff 与 Outbox 事件。"""
    now = datetime.now(UTC)
    operation = f"document.acl.revision:{document_id}"

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        # 1. 幂等重放检查（同键异参返回 409）
        fp, req_hash, existing_idem = await lookup_idempotency_record(
            session,
            organization_id=org_ctx.organization_id,
            principal_id=org_ctx.principal.user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_payload=payload.model_dump(mode="json"),
        )
        if existing_idem is not None:
            return DocumentACLResponse.model_validate(existing_idem.response_body_json)

        # 2. 校验文档归属当前组织
        doc = (
            await session.execute(
                select(Document).where(
                    Document.id == document_id,
                    Document.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="目标文档不存在或无权访问")

        # 3. 锁定 DocumentACL 行（不存在则先创建 revision=0 聚合根再锁）
        acl = (
            await session.execute(
                select(DocumentACL)
                .where(
                    DocumentACL.organization_id == org_ctx.organization_id,
                    DocumentACL.document_id == document_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if acl is None:
            acl = DocumentACL(
                organization_id=org_ctx.organization_id,
                document_id=document_id,
                revision=0,
                status="ACTIVE",
            )
            session.add(acl)
            await session.flush()

        # 4. 乐观锁 revision 核验（并发修改冲突返回 409）
        if acl.revision != payload.expected_revision:
            raise ConflictError(
                code="ACL_REVISION_CONFLICT",
                message=(
                    f"ACL 修订号冲突（期望 revision={payload.expected_revision}，"
                    f"当前最新 revision={acl.revision}），请刷新后重试"
                ),
                detail={
                    "expected_revision": payload.expected_revision,
                    "current_revision": acl.revision,
                },
            )

        # 5. 强类型校验所有授权主体归属当前组织
        await _validate_acl_grants_in_org(
            session,
            organization_id=org_ctx.organization_id,
            grants=payload.grants,
        )

        # 获取上一修订快照用于生成结构化 Diff
        prev_rev = (
            await session.execute(
                select(ACLRevision)
                .where(
                    ACLRevision.organization_id == org_ctx.organization_id,
                    ACLRevision.acl_id == acl.id,
                )
                .order_by(ACLRevision.revision.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        prev_snapshot: list[dict[str, Any]] = prev_rev.snapshot_json if prev_rev else []

        # 6. 替换当前授权集合并展开写入 SNAPSHOT 部门快照表
        await session.execute(
            delete(ACLGrant).where(
                ACLGrant.organization_id == org_ctx.organization_id,
                ACLGrant.acl_id == acl.id,
            )
        )
        await session.flush()

        next_snapshot: list[dict[str, Any]] = []
        for g_in in payload.grants:
            valid_from = g_in.valid_from or now
            grant = ACLGrant(
                organization_id=org_ctx.organization_id,
                acl_id=acl.id,
                subject_type=g_in.subject_type,
                subject_id=g_in.subject_id,
                include_descendants=g_in.include_descendants,
                inheritance_mode=g_in.inheritance_mode,
                valid_from=valid_from,
                valid_until=g_in.valid_until,
                created_by=org_ctx.principal.user_id,
            )
            session.add(grant)
            await session.flush()

            snap_dept_ids: list[uuid.UUID] = []
            if (
                g_in.subject_type == "DEPARTMENT"
                and g_in.subject_id is not None
                and g_in.inheritance_mode == "SNAPSHOT"
            ):
                if g_in.include_descendants:
                    snap_dept_ids = list(
                        (
                            await session.execute(
                                select(DepartmentClosure.descendant_id).where(
                                    DepartmentClosure.organization_id == org_ctx.organization_id,
                                    DepartmentClosure.ancestor_id == g_in.subject_id,
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                else:
                    snap_dept_ids = [g_in.subject_id]

                for did in sorted(set(snap_dept_ids)):
                    session.add(
                        ACLGrantDepartmentSnapshot(
                            organization_id=org_ctx.organization_id,
                            acl_grant_id=grant.id,
                            department_id=did,
                        )
                    )

            next_snapshot.append(_serialize_grant_for_snapshot(grant, snap_dept_ids))

        # 7. 递增 revision，写入不可变 ACLRevision + Diff + Outbox + 安全审计
        acl.revision += 1
        diff_json = _compute_acl_diff(prev_snapshot, next_snapshot)
        session.add(
            ACLRevision(
                organization_id=org_ctx.organization_id,
                acl_id=acl.id,
                revision=acl.revision,
                snapshot_json=next_snapshot,
                diff_json=diff_json,
                changed_by=org_ctx.principal.user_id,
                reason=payload.reason.strip(),
            )
        )

        enqueue_outbox_event(
            session,
            organization_id=org_ctx.organization_id,
            aggregate_type="document_acl",
            aggregate_id=doc.id,
            aggregate_revision=acl.revision,
            event_type="acl.revision.created",
            payload={
                "document_id": str(doc.id),
                "space_id": str(doc.space_id),
                "acl_id": str(acl.id),
                "revision": acl.revision,
                "reason": payload.reason.strip(),
            },
        )
        record_security_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            event_type="acl.revision.created",
            severity="HIGH",
            resource_type="document",
            resource_id=doc.id,
            details={
                "acl_id": str(acl.id),
                "revision": acl.revision,
                "reason": payload.reason.strip(),
                "grant_count": len(next_snapshot),
            },
        )

        response_obj = await _load_document_acl_response_in_tx(
            session,
            organization_id=org_ctx.organization_id,
            doc=doc,
            acl=acl,
        )
        save_idempotency_record(
            session,
            organization_id=org_ctx.organization_id,
            principal_id=org_ctx.principal.user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            fingerprint=fp,
            request_hash=req_hash,
            response_status=200,
            response_body=response_obj.model_dump(mode="json"),
        )

    return response_obj
