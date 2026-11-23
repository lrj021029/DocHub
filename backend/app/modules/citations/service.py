"""引用溯源实时二次鉴权服务（`GET /api/v1/citations/{citation_id}`）。

安全红线（PRD §8.2 & TECHNICAL_SPEC §9.6）：
1. 每次点击或打开引用卡片，必须在 `org_rls_session` 下通过 `AuthoritativeACLEngine`
   实时校验当前用户对该来源文档的访问权限，绝不凭历史 `citation_id` 直接放行。
2. 同时核验引用所属会话归属当前用户，防止同组织内横向枚举他人会话引用。
3. 无权或撤权后严格抛出 `ForbiddenError`（HTTP 403），响应与安全审计中绝不泄露
   文档标题、摘录或路径。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError
from app.db.session import org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.audit.service import record_security_audit_event
from app.modules.chat.models import Conversation, Message
from app.modules.citations.models import AnswerCitation
from app.modules.citations.schemas import CitationDetailResponse
from app.modules.documents.models import Chunk, Document, ParsedArtifact, ParsedBlock
from app.modules.identity.deps import OrgContext


def _format_quote_excerpt(text: str, *, max_chars: int = 800) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "..."


async def get_authorized_citation_detail(
    *,
    org_ctx: OrgContext,
    citation_id: uuid.UUID,
    session: AsyncSession,
    acl_engine: AuthoritativeACLEngine | None = None,
) -> CitationDetailResponse:
    """实时重鉴权并返回单条引用定位与证据摘录；失权/无权时严格返回 403。"""
    engine = acl_engine or AuthoritativeACLEngine()
    denied_error: ForbiddenError | None = None
    response: CitationDetailResponse | None = None

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        row = (
            await session.execute(
                select(AnswerCitation, Message.conversation_id, Conversation.user_id)
                .join(Message, Message.id == AnswerCitation.message_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    AnswerCitation.id == citation_id,
                    AnswerCitation.organization_id == org_ctx.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(code="CITATION_NOT_FOUND", message="目标引用不存在或无权访问")

        citation, _conv_id, owner_user_id = row
        if owner_user_id != org_ctx.principal.user_id:
            record_security_audit_event(
                session,
                organization_id=org_ctx.organization_id,
                actor_user_id=org_ctx.principal.user_id,
                event_type="citation.cross_user_denied",
                severity="MEDIUM",
                resource_type="answer_citation",
                resource_id=citation_id,
            )
            # 先正常退出 org_rls_session 提交安全审计事件，再在事务外抛出 403，避免审计记录被回滚丢失
            denied_error = ForbiddenError(
                code="CITATION_ACCESS_DENIED",
                message="无权访问其他用户的会话引用",
            )
        else:
            decision = await engine.check_document_access(
                session,
                org_ctx,
                citation.document_id,
                organization_id=org_ctx.organization_id,
            )
            if not decision.allowed:
                # 安全红线：绝不在报错信息或审计详情中泄露受限文档标题或摘录；待事务提交落盘后再抛 403
                record_security_audit_event(
                    session,
                    organization_id=org_ctx.organization_id,
                    actor_user_id=org_ctx.principal.user_id,
                    event_type="citation.access_denied",
                    severity="LOW",
                    resource_type="answer_citation",
                    resource_id=citation_id,
                    details={"reason_code": decision.reason_code},
                )
                denied_error = ForbiddenError(
                    code="CITATION_ACCESS_DENIED",
                    message="您当前已不再具备该引用来源文档的访问权限",
                )
            else:
                doc = (
                    await session.execute(
                        select(Document).where(
                            Document.id == citation.document_id,
                            Document.organization_id == org_ctx.organization_id,
                        )
                    )
                ).scalar_one_or_none()
                if doc is None:
                    raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="来源文档不存在或已删除")

                chunk = (
                    await session.execute(
                        select(Chunk).where(
                            Chunk.id == citation.chunk_id,
                            Chunk.organization_id == org_ctx.organization_id,
                        )
                    )
                ).scalar_one_or_none()

                heading_str: str | None = None
                quote_raw = chunk.content_text if chunk else ""

                if citation.block_id is not None:
                    block = (
                        await session.execute(
                            select(ParsedBlock).where(
                                ParsedBlock.id == citation.block_id,
                                ParsedBlock.organization_id == org_ctx.organization_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if block is not None:
                        if block.heading_path:
                            heading_str = " > ".join(str(x) for x in block.heading_path)
                        if not quote_raw and block.content_text:
                            quote_raw = block.content_text

                if not heading_str and chunk and isinstance(chunk.metadata_json, dict):
                    hp = chunk.metadata_json.get("heading_path")
                    if isinstance(hp, list) and hp:
                        heading_str = " > ".join(str(x) for x in hp)

                # 若该文档版本存在对应页码的 page_image 产物，返回内部标识路径（绝不伪造）
                page_image_url: str | None = None
                if citation.page_number is not None:
                    page_artifacts = (
                        (
                            await session.execute(
                                select(ParsedArtifact).where(
                                    ParsedArtifact.organization_id == org_ctx.organization_id,
                                    ParsedArtifact.document_version_id
                                    == citation.document_version_id,
                                    ParsedArtifact.artifact_type == "page_image",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for art in page_artifacts:
                        meta: dict[str, Any] = art.metadata_json or {}
                        if meta.get("page_number") == citation.page_number:
                            page_image_url = f"/api/v1/citations/{citation.id}/page-image"
                            break

                excerpt = _format_quote_excerpt(quote_raw)
                current_acl_rev = (
                    decision.acl_revision
                    if decision.acl_revision is not None
                    else citation.acl_revision
                )
                response = CitationDetailResponse(
                    citation_id=citation.id,
                    document_id=citation.document_id,
                    document_version_id=citation.document_version_id,
                    document_title=doc.title,
                    chunk_id=citation.chunk_id,
                    block_id=citation.block_id,
                    page_number=citation.page_number,
                    page=citation.page_number,
                    heading_path=heading_str,
                    quote_text=excerpt,
                    quote=excerpt,
                    quote_excerpt=excerpt,
                    page_image_url=page_image_url,
                    bbox_json=citation.bbox_json,
                    bbox=citation.bbox_json,
                    acl_revision=current_acl_rev,
                    authorized=True,
                    denied_reason=None,
                )

    if denied_error is not None:
        raise denied_error
    assert response is not None
    return response
