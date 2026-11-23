"""引用溯源 HTTP 路由（`/api/v1/citations/{citation_id}`）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.modules.citations.schemas import CitationDetailResponse
from app.modules.citations.service import get_authorized_citation_detail
from app.modules.identity.deps import OrgContext, require_permission

router = APIRouter(prefix="/citations", tags=["Citations"])


@router.get(
    "/{citation_id}",
    response_model=CitationDetailResponse,
    summary="获取引用溯源定位详情（实时 ACL 二次重鉴权）",
)
async def get_citation_detail(
    citation_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("chat:use")),
    session: AsyncSession = Depends(get_db_session),
) -> CitationDetailResponse:
    """点击引用卡片时实时核验 PostgreSQL 当前四维 ACL，无权或撤权严格返回 403。"""
    return await get_authorized_citation_detail(
        org_ctx=org_ctx,
        citation_id=citation_id,
        session=session,
    )
