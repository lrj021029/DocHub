"""Document ACL HTTP 路由（`/api/v1/documents/{document_id}/acl*`）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import validate_idempotency_key
from app.db.session import get_db_session
from app.modules.acl.schemas import (
    ACLRevisionCreateRequest,
    ACLRevisionItem,
    DocumentACLResponse,
)
from app.modules.acl.service import (
    create_document_acl_revision,
    get_document_acl,
    list_document_acl_revisions,
)
from app.modules.identity.deps import OrgContext, require_permission

router = APIRouter(prefix="/documents/{document_id}/acl", tags=["Document ACL"])


@router.get(
    "",
    response_model=DocumentACLResponse,
    summary="查询文档四维 ACL 配置",
)
async def get_doc_acl(
    document_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("document:acl:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentACLResponse:
    """获取文档当前四维 Allow OR 授权详情及修订号。"""
    return await get_document_acl(
        org_ctx=org_ctx,
        document_id=document_id,
        session=session,
    )


@router.get(
    "/revisions",
    response_model=list[ACLRevisionItem],
    summary="查询文档 ACL 不可变修订历史",
)
async def get_doc_acl_revisions(
    document_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("document:acl:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> list[ACLRevisionItem]:
    """查询文档历史 ACL 变更时间线与 Diff。"""
    return await list_document_acl_revisions(
        org_ctx=org_ctx,
        document_id=document_id,
        session=session,
    )


@router.post(
    "/revisions",
    response_model=DocumentACLResponse,
    summary="提交文档 ACL 新修订（唯一写入口，带乐观锁与幂等键）",
)
async def post_doc_acl_revision(
    document_id: uuid.UUID,
    payload: ACLRevisionCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    org_ctx: OrgContext = Depends(require_permission("document:acl:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentACLResponse:
    """使用乐观锁 `expected_revision` + `Idempotency-Key` 提交 ACL 变更并写 Outbox 事件。"""
    validated_key = validate_idempotency_key(idempotency_key, required=True)
    assert validated_key is not None
    return await create_document_acl_revision(
        org_ctx=org_ctx,
        document_id=document_id,
        payload=payload,
        idempotency_key=validated_key,
        session=session,
    )
