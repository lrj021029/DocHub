"""受 ACL 保护的 RAG 混合检索问答编排、Query Rewrite、SSE 推送与历史重鉴权服务。

核心安全红线（PRD §5 / §8 & TECHNICAL_SPEC §6.8 / §9.2 / §9.7）：
1. 每次问答、多轮改写、历史会话打开均以 PostgreSQL `AuthoritativeACLEngine` 作为唯一事实源。
2. Qdrant 仅返回候选 ID；PostgreSQL 批量 ACL 复核后丢弃全部拒绝项，仅加载已授权 Chunk 正文。
3. **进入 LLM 前二次 ACL Revision 复核**：检测检索窗口期内是否发生权限撤销或版本变更，
   若失权立即剔除。
4. 任何 SSE 帧、普通日志、或 `RetrievalCandidate` 表均严禁记录未授权文档标题、正文或路径。
5. 历史会话 `GET /chat/conversations/{id}` 逐段校验来源权限；若 `is_splittable=False`
   且任一来源撤权，整条回答整体脱敏隐藏；数据库原始审计与 `AnswerCitation` 保持不可变留痕。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, NotFoundError, PolicyBlockedError, ValidationError
from app.core.ids import new_uuid7
from app.core.pagination import decode_cursor, encode_cursor
from app.db.session import org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.audit.service import record_security_audit_event
from app.modules.chat.models import (
    Conversation,
    ConversationSpace,
    LLMUsage,
    Message,
    MessageFeedback,
    MessageSegment,
    RetrievalAudit,
    RetrievalCandidate,
    RetrievalDecision,
)
from app.modules.chat.schemas import (
    ChatMessageResponse,
    ChatStreamRequest,
    ConversationCreateRequest,
    ConversationCursorPage,
    ConversationDetailResponse,
    ConversationSummaryResponse,
    MessageCancelResponse,
    MessageCitationResponse,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
    MessageSegmentResponse,
)
from app.modules.citations.models import AnswerCitation
from app.modules.documents.models import Chunk, Document, DocumentVersion, ParsedBlock
from app.modules.embeddings.providers import EmbeddingProfile as RuntimeEmbeddingProfile
from app.modules.embeddings.providers import EmbeddingRouter
from app.modules.identity.deps import OrgContext
from app.modules.indexing.models import (
    ActiveIndexPointer,
    EmbeddingProfile,
    EmbeddingProvider,
    IndexGeneration,
)
from app.modules.indexing.qdrant_store import QdrantIndexStore
from app.modules.knowledge_gaps.models import QuestionEvent
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.providers.egress import SensitivityLevel, highest_sensitivity
from app.modules.providers.llm import (
    AnswerPolicyMode,
    LLMMessage,
    OpenAICompatibleLLMProvider,
)
from app.modules.providers.reranker import RerankerRouter
from app.modules.retrieval.hybrid import (
    AuthorizedChunkBody,
    ChunkAuthorizationResult,
    ContextBuilder,
    FusedEvidenceItem,
    MultiSpaceHybridRetriever,
    SpaceRetrievalTarget,
)

logger = logging.getLogger(__name__)

# 脱敏占位文案（不透露任何受限文档标题或存在性）
REDACTED_SEGMENT_PLACEHOLDER = "[该段落引用的知识来源当前无访问权限或已撤权，内容已脱敏隐藏]"
REDACTED_MESSAGE_PLACEHOLDER = (
    "[该回答包含已撤权或受限知识来源，且无法按段落安全拆分，系统已整体脱敏隐藏]"
)
ACL_DENIED_SAFE_REPLY = "抱歉，当前知识库中暂无您有权访问的相关证据，无法回答该问题。"
NO_RETRIEVAL_SAFE_REPLY = "抱歉，当前所选知识空间中未检索到与该问题相关的已索引证据。"

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")


def sanitize_question_text(raw_text: str) -> str:
    """对 QuestionEvent 问题文本执行 PII 脱敏并归一化（TECHNICAL_SPEC §10.2）。"""
    cleaned = " ".join(raw_text.strip().split())
    cleaned = _PHONE_RE.sub("[PHONE]", cleaned)
    cleaned = _EMAIL_RE.sub("[EMAIL]", cleaned)
    cleaned = _ID_CARD_RE.sub("[ID_CARD]", cleaned)
    return cleaned


def _format_sse_frame(event_name: str, data: dict[str, Any]) -> str:
    """格式化标准 SSE 文本帧（`event: ...\\ndata: ...\\n\\n`）。"""
    return f"event: {event_name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class CancellationRegistry:
    """进程内流式生成取消协调器（配合数据库 Message.status 持久化状态）。"""

    def __init__(self) -> None:
        self._cancelled_ids: set[uuid.UUID] = set()

    def request_cancel(self, message_id: uuid.UUID) -> None:
        """登记流式消息取消请求。"""
        self._cancelled_ids.add(message_id)

    def is_cancelled(self, message_id: uuid.UUID) -> bool:
        """判断指定消息是否已被请求取消。"""
        return message_id in self._cancelled_ids

    def clear(self, message_id: uuid.UUID) -> None:
        """流结束后清理内存取消标记。"""
        self._cancelled_ids.discard(message_id)


class QueryRewriter:
    """多轮对话独立检索 Query 改写器（保留原始提问，不覆写用户原文）。"""

    @staticmethod
    def rewrite(
        original_query: str,
        authorized_history: Sequence[LLMMessage],
    ) -> str:
        """结合最近已鉴权的非脱敏对话轮次构造独立检索问句。"""
        cleaned = original_query.strip()
        if not authorized_history:
            return cleaned

        # 仅提取最近 2 轮已确认安全的 USER 历史主题词进行语义消歧拼接
        recent_user_turns = [
            msg.content.strip()
            for msg in authorized_history
            if msg.role == "user" and msg.content.strip()
        ][-2:]
        if not recent_user_turns:
            return cleaned

        # 若当前提问极短或含指代词（“它/这个/上述/刚才”），补充前置上下文关键词
        pronoun_hints = ("它", "这个", "那个", "该", "上述", "上面", "前面", "刚才", "其中")
        if len(cleaned) <= 12 or any(h in cleaned for h in pronoun_hints):
            prefix = " ".join(turn[:60] for turn in recent_user_turns)
            return f"{prefix} {cleaned}".strip()
        return cleaned


@dataclass(slots=True)
class ChatRuntimeDeps:
    """Chat Service 运行期可注入依赖包（便于生产装配与测试覆盖）。"""

    qdrant_store: QdrantIndexStore
    embedding_router: EmbeddingRouter
    reranker_router: RerankerRouter
    llm_provider: OpenAICompatibleLLMProvider
    acl_engine: AuthoritativeACLEngine
    cancellation_registry: CancellationRegistry
    context_max_tokens: int = 2400
    allow_rerank_rrf_fallback: bool = True


async def create_conversation(
    *,
    org_ctx: OrgContext,
    payload: ConversationCreateRequest,
    session: AsyncSession,
    acl_engine: AuthoritativeACLEngine | None = None,
) -> ConversationSummaryResponse:
    """在当前组织上下文中创建多轮对话并校验全部选中 Space 入口权限。"""
    engine = acl_engine or AuthoritativeACLEngine()
    unique_space_ids = list(dict.fromkeys(payload.space_ids))

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        for space_id in unique_space_ids:
            decision = await engine.check_space_access(
                session,
                org_ctx,
                space_id,
                organization_id=org_ctx.organization_id,
            )
            if not decision.allowed:
                raise ForbiddenError(
                    code="SPACE_ACCESS_DENIED",
                    message="无权访问所选知识空间，无法创建会话",
                )

        conv = Conversation(
            id=new_uuid7(),
            organization_id=org_ctx.organization_id,
            user_id=org_ctx.principal.user_id,
            title=(payload.title or "新知识检索会话").strip()[:255] or "新知识检索会话",
            status="ACTIVE",
        )
        session.add(conv)
        await session.flush()

        for sid in unique_space_ids:
            session.add(
                ConversationSpace(
                    organization_id=org_ctx.organization_id,
                    conversation_id=conv.id,
                    space_id=sid,
                )
            )
        await session.flush()
        await session.refresh(conv)

    return ConversationSummaryResponse(
        id=conv.id,
        organization_id=conv.organization_id,
        title=conv.title,
        space_ids=unique_space_ids,
        status=conv.status,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
    )


async def list_conversations(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
    cursor: str | None = None,
    limit: int = 20,
) -> ConversationCursorPage:
    """使用 Cursor 分页返回当前用户在当前组织内的历史会话列表。"""
    cursor_cond = None
    if cursor:
        payload = decode_cursor(cursor)
        raw_ts = payload.get("created_at")
        raw_id = payload.get("id")
        if not isinstance(raw_ts, str) or not isinstance(raw_id, str):
            raise ValidationError(code="INVALID_CURSOR", message="会话分页游标格式无效")
        cursor_ts = datetime.fromisoformat(raw_ts)
        cursor_id = uuid.UUID(raw_id)
        cursor_cond = or_(
            Conversation.created_at < cursor_ts,
            and_(Conversation.created_at == cursor_ts, Conversation.id < cursor_id),
        )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        stmt = select(Conversation).where(
            Conversation.organization_id == org_ctx.organization_id,
            Conversation.user_id == org_ctx.principal.user_id,
        )
        if cursor_cond is not None:
            stmt = stmt.where(cursor_cond)

        rows = (
            (
                await session.execute(
                    stmt.order_by(Conversation.created_at.desc(), Conversation.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            .scalars()
            .all()
        )

        has_more = len(rows) > limit
        page_rows = rows[:limit]
        conv_ids = [c.id for c in page_rows]
        space_map: dict[uuid.UUID, list[uuid.UUID]] = {cid: [] for cid in conv_ids}
        if conv_ids:
            cs_rows = (
                await session.execute(
                    select(ConversationSpace.conversation_id, ConversationSpace.space_id).where(
                        ConversationSpace.organization_id == org_ctx.organization_id,
                        ConversationSpace.conversation_id.in_(conv_ids),
                    )
                )
            ).all()
            for cid, sid in cs_rows:
                space_map.setdefault(cid, []).append(sid)

    items = [
        ConversationSummaryResponse(
            id=c.id,
            organization_id=c.organization_id,
            title=c.title,
            space_ids=space_map.get(c.id, []),
            status=c.status,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in page_rows
    ]
    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})

    return ConversationCursorPage(items=items, next_cursor=next_cursor, has_more=has_more)


async def get_conversation_detail(
    *,
    org_ctx: OrgContext,
    conversation_id: uuid.UUID,
    session: AsyncSession,
    acl_engine: AuthoritativeACLEngine | None = None,
) -> ConversationDetailResponse:
    """获取会话详情并按当前时刻四维 ACL 执行段落级/整条动态重鉴权。

    注意：数据库底层审计、`MessageSegment` 与 `AnswerCitation` 保持不可变，
    仅对返回给调用方的展示视图执行实时脱敏。
    """
    engine = acl_engine or AuthoritativeACLEngine()

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.organization_id == org_ctx.organization_id,
                    Conversation.user_id == org_ctx.principal.user_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise NotFoundError(code="CONVERSATION_NOT_FOUND", message="目标会话不存在或无权访问")

        bound_spaces = list(
            (
                await session.execute(
                    select(ConversationSpace.space_id).where(
                        ConversationSpace.organization_id == org_ctx.organization_id,
                        ConversationSpace.conversation_id == conv.id,
                    )
                )
            )
            .scalars()
            .all()
        )

        msg_rows = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.organization_id == org_ctx.organization_id,
                        Message.conversation_id == conv.id,
                    )
                    .order_by(Message.created_at.asc(), Message.id.asc())
                )
            )
            .scalars()
            .all()
        )
        msg_ids = [m.id for m in msg_rows]

        segments_by_msg: dict[uuid.UUID, list[MessageSegment]] = {mid: [] for mid in msg_ids}
        citations_by_msg: dict[uuid.UUID, list[tuple[AnswerCitation, str, str]]] = {
            mid: [] for mid in msg_ids
        }
        feedback_by_msg: dict[uuid.UUID, str] = {}
        referenced_doc_ids: set[uuid.UUID] = set()

        if msg_ids:
            seg_rows = (
                (
                    await session.execute(
                        select(MessageSegment)
                        .where(
                            MessageSegment.organization_id == org_ctx.organization_id,
                            MessageSegment.message_id.in_(msg_ids),
                        )
                        .order_by(MessageSegment.sequence.asc())
                    )
                )
                .scalars()
                .all()
            )
            for seg in seg_rows:
                segments_by_msg.setdefault(seg.message_id, []).append(seg)
                for raw_doc_id in seg.source_document_ids or []:
                    try:
                        referenced_doc_ids.add(uuid.UUID(str(raw_doc_id)))
                    except ValueError:
                        continue

            cit_rows = (
                await session.execute(
                    select(AnswerCitation, Document.title, Chunk.content_text)
                    .join(Document, Document.id == AnswerCitation.document_id)
                    .join(Chunk, Chunk.id == AnswerCitation.chunk_id)
                    .where(
                        AnswerCitation.organization_id == org_ctx.organization_id,
                        AnswerCitation.message_id.in_(msg_ids),
                    )
                    .order_by(AnswerCitation.created_at.asc(), AnswerCitation.id.asc())
                )
            ).all()
            for cit, doc_title, chunk_text in cit_rows:
                citations_by_msg.setdefault(cit.message_id, []).append((cit, doc_title, chunk_text))
                referenced_doc_ids.add(cit.document_id)

            fb_rows = (
                await session.execute(
                    select(MessageFeedback.message_id, MessageFeedback.rating).where(
                        MessageFeedback.organization_id == org_ctx.organization_id,
                        MessageFeedback.user_id == org_ctx.principal.user_id,
                        MessageFeedback.message_id.in_(msg_ids),
                    )
                )
            ).all()
            for mid, rating in fb_rows:
                feedback_by_msg[mid] = "up" if rating == "HELPFUL" else "down"

        # 实时逐文档重鉴权（按当前时刻的 Org/Space/Document/4D ACL 事实）
        doc_allowed_map: dict[uuid.UUID, bool] = {}
        for doc_id in referenced_doc_ids:
            decision = await engine.check_document_access(
                session,
                org_ctx,
                doc_id,
                organization_id=org_ctx.organization_id,
            )
            doc_allowed_map[doc_id] = decision.allowed

    def _all_sources_allowed(raw_ids: Sequence[str] | None) -> bool:
        if not raw_ids:
            return True
        for raw_id in raw_ids:
            try:
                uid = uuid.UUID(str(raw_id))
            except ValueError:
                return False
            if not doc_allowed_map.get(uid, False):
                return False
        return True

    rendered_messages: list[ChatMessageResponse] = []
    for msg in msg_rows:
        role_literal = (
            "user"
            if msg.role.upper() == "USER"
            else ("system" if msg.role.upper() == "SYSTEM" else "assistant")
        )
        msg_segs = segments_by_msg.get(msg.id, [])
        msg_cits = citations_by_msg.get(msg.id, [])

        if role_literal != "assistant":
            rendered_messages.append(
                ChatMessageResponse(
                    id=msg.id,
                    role=role_literal,
                    content=msg.content,
                    status=msg.status,
                    redacted=False,
                    is_splittable=msg.is_splittable,
                    segments=[],
                    citations=[],
                    feedback=None,
                    created_at=msg.created_at,
                )
            )
            continue

        # 校验不可拆分敏感消息是否触发整条隐藏
        all_msg_doc_ids: list[str] = []
        for seg in msg_segs:
            all_msg_doc_ids.extend(str(x) for x in (seg.source_document_ids or []))
        for cit, _, _ in msg_cits:
            all_msg_doc_ids.append(str(cit.document_id))

        if not msg.is_splittable and not _all_sources_allowed(all_msg_doc_ids):
            redacted_segs = [
                MessageSegmentResponse(
                    id=seg.id,
                    sequence=seg.sequence,
                    segment_type=seg.segment_type,
                    content=REDACTED_MESSAGE_PLACEHOLDER,
                    redacted=True,
                    redacted_reason="NON_SPLITTABLE_SOURCE_REVOKED",
                )
                for seg in msg_segs
            ]
            if not redacted_segs:
                redacted_segs = [
                    MessageSegmentResponse(
                        id=msg.id,
                        sequence=0,
                        segment_type="EVIDENCE",
                        content=REDACTED_MESSAGE_PLACEHOLDER,
                        redacted=True,
                        redacted_reason="NON_SPLITTABLE_SOURCE_REVOKED",
                    )
                ]
            rendered_messages.append(
                ChatMessageResponse(
                    id=msg.id,
                    role="assistant",
                    content=REDACTED_MESSAGE_PLACEHOLDER,
                    status=msg.status,
                    redacted=True,
                    is_splittable=False,
                    segments=redacted_segs,
                    citations=[],
                    feedback=feedback_by_msg.get(msg.id),
                    created_at=msg.created_at,
                )
            )
            continue

        # 可拆分消息：按段落独立重鉴权
        rendered_segs: list[MessageSegmentResponse] = []
        any_redacted = False
        all_redacted = bool(msg_segs)

        for seg in msg_segs:
            # 同时检查属于该段落的引用文档是否仍授权
            seg_cit_docs = [
                str(cit.document_id) for cit, _, _ in msg_cits if cit.message_segment_id == seg.id
            ]
            combined_sources = [*(str(x) for x in (seg.source_document_ids or [])), *seg_cit_docs]
            if _all_sources_allowed(combined_sources):
                all_redacted = False
                rendered_segs.append(
                    MessageSegmentResponse(
                        id=seg.id,
                        sequence=seg.sequence,
                        segment_type=seg.segment_type,
                        content=seg.content,
                        redacted=False,
                        redacted_reason=None,
                    )
                )
            else:
                any_redacted = True
                rendered_segs.append(
                    MessageSegmentResponse(
                        id=seg.id,
                        sequence=seg.sequence,
                        segment_type=seg.segment_type,
                        content=REDACTED_SEGMENT_PLACEHOLDER,
                        redacted=True,
                        redacted_reason="SOURCE_ACCESS_REVOKED",
                    )
                )

        # 仅保留当前仍授权的引用卡片（失权引用绝不暴露标题/摘录）
        authorized_cits = [
            MessageCitationResponse(
                citation_id=cit.id,
                document_id=cit.document_id,
                document_version_id=cit.document_version_id,
                document_title=doc_title,
                page=cit.page_number,
                page_number=cit.page_number,
                chunk_id=cit.chunk_id,
                block_id=cit.block_id,
                quote=chunk_text[:280].strip() if chunk_text else None,
            )
            for cit, doc_title, chunk_text in msg_cits
            if doc_allowed_map.get(cit.document_id, False)
        ]

        if rendered_segs:
            reconstructed_content = "\n\n".join(s.content for s in rendered_segs)
        else:
            reconstructed_content = msg.content

        rendered_messages.append(
            ChatMessageResponse(
                id=msg.id,
                role="assistant",
                content=reconstructed_content,
                status=msg.status,
                redacted=all_redacted
                or (any_redacted and not authorized_cits and not rendered_segs),
                is_splittable=msg.is_splittable,
                segments=rendered_segs,
                citations=authorized_cits,
                feedback=feedback_by_msg.get(msg.id),
                created_at=msg.created_at,
            )
        )

    return ConversationDetailResponse(
        id=conv.id,
        organization_id=conv.organization_id,
        title=conv.title,
        space_ids=bound_spaces,
        status=conv.status,
        messages=rendered_messages,
    )


async def cancel_chat_message(
    *,
    org_ctx: OrgContext,
    message_id: uuid.UUID,
    session: AsyncSession,
    cancellation_registry: CancellationRegistry,
) -> MessageCancelResponse:
    """取消正在流式生成的助手消息。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        row = (
            await session.execute(
                select(Message, Conversation.user_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.id == message_id,
                    Message.organization_id == org_ctx.organization_id,
                )
                .with_for_update()
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(code="MESSAGE_NOT_FOUND", message="目标消息不存在或无权操作")
        msg, owner_id = row
        if owner_id != org_ctx.principal.user_id:
            raise ForbiddenError(code="MESSAGE_CANCEL_FORBIDDEN", message="无权取消其他用户的消息")

        cancellation_registry.request_cancel(msg.id)
        if msg.status == "STREAMING":
            msg.status = "CANCELLED"
            return MessageCancelResponse(message_id=msg.id, status="CANCELLED", cancelled=True)

        return MessageCancelResponse(
            message_id=msg.id,
            status=msg.status,
            cancelled=msg.status == "CANCELLED",
        )


async def submit_message_feedback(
    *,
    org_ctx: OrgContext,
    message_id: uuid.UUID,
    payload: MessageFeedbackRequest,
    session: AsyncSession,
) -> MessageFeedbackResponse:
    """提交或更新用户对助手回答的质量反馈（HELPFUL / UNHELPFUL）。"""
    normalized_rating = "HELPFUL" if payload.rating in {"up", "HELPFUL"} else "UNHELPFUL"

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        row = (
            await session.execute(
                select(Message, Conversation.user_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    Message.id == message_id,
                    Message.organization_id == org_ctx.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(code="MESSAGE_NOT_FOUND", message="目标消息不存在或无权反馈")
        msg, owner_id = row
        if owner_id != org_ctx.principal.user_id:
            raise ForbiddenError(
                code="MESSAGE_FEEDBACK_FORBIDDEN", message="无权评价其他用户的会话"
            )

        existing = (
            await session.execute(
                select(MessageFeedback)
                .where(
                    MessageFeedback.organization_id == org_ctx.organization_id,
                    MessageFeedback.message_id == msg.id,
                    MessageFeedback.user_id == org_ctx.principal.user_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        cleaned_comment = payload.comment.strip() if payload.comment else None
        if existing is None:
            session.add(
                MessageFeedback(
                    organization_id=org_ctx.organization_id,
                    message_id=msg.id,
                    user_id=org_ctx.principal.user_id,
                    rating=normalized_rating,
                    comment=cleaned_comment,
                )
            )
        else:
            existing.rating = normalized_rating
            existing.comment = cleaned_comment

    return MessageFeedbackResponse(
        message_id=message_id,
        rating=normalized_rating,
        comment=cleaned_comment,
    )


async def stream_rag_chat_events(
    *,
    org_ctx: OrgContext,
    conversation_id: uuid.UUID,
    payload: ChatStreamRequest,
    session: AsyncSession,
    deps: ChatRuntimeDeps,
) -> AsyncIterator[str]:
    """执行受 ACL 保护的混合检索问答与 SSE 流式生成全链路。"""
    raw_query = payload.resolved_query()
    query_hash = hashlib.sha256(raw_query.encode("utf-8")).hexdigest()
    user_hash = hashlib.sha256(str(org_ctx.principal.user_id).encode("utf-8")).hexdigest()
    session_hash = hashlib.sha256(str(conversation_id).encode("utf-8")).hexdigest()

    # --- 1. 校验会话、Space 入口权限、创建消息占位并读取已授权历史 ---
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.organization_id == org_ctx.organization_id,
                    Conversation.user_id == org_ctx.principal.user_id,
                )
            )
        ).scalar_one_or_none()
        if conv is None:
            raise NotFoundError(code="CONVERSATION_NOT_FOUND", message="目标会话不存在或无权访问")

        bound_space_ids = list(
            (
                await session.execute(
                    select(ConversationSpace.space_id).where(
                        ConversationSpace.organization_id == org_ctx.organization_id,
                        ConversationSpace.conversation_id == conv.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        target_space_ids = (
            list(dict.fromkeys(payload.space_ids)) if payload.space_ids else bound_space_ids
        )
        if not target_space_ids:
            raise ValidationError(code="NO_SPACES_SELECTED", message="请至少选择一个检索知识空间")

        space_policies: list[str] = []
        space_targets: list[SpaceRetrievalTarget] = []
        org_allow_external_l1 = False

        for sid in target_space_ids:
            sp_decision = await deps.acl_engine.check_space_access(
                session,
                org_ctx,
                sid,
                organization_id=org_ctx.organization_id,
            )
            if not sp_decision.allowed:
                raise ForbiddenError(
                    code="SPACE_ACCESS_DENIED",
                    message="无权检索所选知识空间",
                )

            space_row = (
                await session.execute(
                    select(KnowledgeSpace).where(
                        KnowledgeSpace.organization_id == org_ctx.organization_id,
                        KnowledgeSpace.id == sid,
                        KnowledgeSpace.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if space_row is None:
                continue

            space_policies.append(space_row.answer_policy)
            if isinstance(space_row.config_json, dict) and space_row.config_json.get(
                "org_allow_external_l1"
            ):
                org_allow_external_l1 = True

            # 解析当前 Space 激活的 IndexGeneration 与 EmbeddingProfile
            ptr_row = (
                await session.execute(
                    select(ActiveIndexPointer, IndexGeneration, EmbeddingProfile, EmbeddingProvider)
                    .join(
                        IndexGeneration,
                        IndexGeneration.id == ActiveIndexPointer.index_generation_id,
                    )
                    .join(
                        EmbeddingProfile,
                        EmbeddingProfile.id == ActiveIndexPointer.embedding_profile_id,
                    )
                    .join(
                        EmbeddingProvider,
                        EmbeddingProvider.id == EmbeddingProfile.provider_id,
                    )
                    .where(
                        ActiveIndexPointer.organization_id == org_ctx.organization_id,
                        ActiveIndexPointer.scope_type == "KNOWLEDGE_SPACE",
                        ActiveIndexPointer.scope_id == sid,
                    )
                )
            ).one_or_none()
            if ptr_row is not None:
                _ptr, gen_row, prof_row, prov_row = ptr_row
                pref_provider = (
                    "local_bge"
                    if prov_row.provider_type == "local_bge"
                    else ("qwen" if prov_row.provider_type == "qwen" else None)
                )
                space_targets.append(
                    SpaceRetrievalTarget(
                        space_id=str(sid),
                        collection_name=gen_row.collection_name,
                        index_generation_id=str(gen_row.id),
                        embedding_profile_id=str(prof_row.id),
                        embedding_profile=RuntimeEmbeddingProfile(
                            provider=pref_provider,
                            model_id=prof_row.model_id,
                            model_version=prof_row.model_version,
                            dimensions=prof_row.dimensions,
                        ),
                        dense_score_threshold=0.15,
                        acl_prefilter=None,
                    )
                )

        # 若多个 Space 策略混用，从严取 STRICT_EVIDENCE
        if payload.answer_policy is not None:
            effective_policy = payload.answer_policy
        elif any(p == AnswerPolicyMode.STRICT_EVIDENCE.value for p in space_policies):
            effective_policy = AnswerPolicyMode.STRICT_EVIDENCE
        elif space_policies:
            effective_policy = AnswerPolicyMode(space_policies[0])
        else:
            effective_policy = AnswerPolicyMode.STRICT_EVIDENCE

        # 加载历史对话轮次供 Query Rewrite 与上下文使用（仅保留已授权未脱敏内容）
        history_msgs_raw = (
            (
                await session.execute(
                    select(Message)
                    .where(
                        Message.organization_id == org_ctx.organization_id,
                        Message.conversation_id == conv.id,
                        Message.status == "COMPLETED",
                    )
                    .order_by(Message.created_at.asc(), Message.id.asc())
                )
            )
            .scalars()
            .all()
        )
        safe_history: list[LLMMessage] = []
        for h_msg in history_msgs_raw[-6:]:
            role_name = "user" if h_msg.role.upper() == "USER" else "assistant"
            if role_name == "assistant":
                # 仅当助手消息至少有一条未撤权来源时纳入历史上下文
                segs = (
                    (
                        await session.execute(
                            select(MessageSegment).where(
                                MessageSegment.organization_id == org_ctx.organization_id,
                                MessageSegment.message_id == h_msg.id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                allowed_parts: list[str] = []
                for s in segs:
                    all_ok = True
                    for d_str in s.source_document_ids or []:
                        dec = await deps.acl_engine.check_document_access(
                            session,
                            org_ctx,
                            uuid.UUID(str(d_str)),
                            organization_id=org_ctx.organization_id,
                        )
                        if not dec.allowed:
                            all_ok = False
                            break
                    if all_ok:
                        allowed_parts.append(s.content)
                if allowed_parts:
                    safe_history.append(
                        LLMMessage(role="assistant", content="\n".join(allowed_parts))
                    )
            else:
                safe_history.append(LLMMessage(role="user", content=h_msg.content))

        user_msg = Message(
            id=new_uuid7(),
            organization_id=org_ctx.organization_id,
            conversation_id=conv.id,
            role="USER",
            content=raw_query,
            status="COMPLETED",
        )
        assistant_msg = Message(
            id=new_uuid7(),
            organization_id=org_ctx.organization_id,
            conversation_id=conv.id,
            role="ASSISTANT",
            content="",
            status="STREAMING",
            answer_policy_used=effective_policy.value,
            is_splittable=payload.is_splittable,
        )
        session.add(user_msg)
        session.add(assistant_msg)
        await session.flush()
        assistant_msg_id = assistant_msg.id

    # 1) 发送 message_start 事件
    yield _format_sse_frame(
        "message_start",
        {
            "message_id": str(assistant_msg_id),
            "conversation_id": str(conversation_id),
        },
    )

    # 2) 多轮 Query Rewrite（不覆写原问题，且后续始终重新鉴权）
    retrieval_query = QueryRewriter.rewrite(raw_query, safe_history)

    yield _format_sse_frame(
        "retrieval_started",
        {"space_ids": [str(sid) for sid in target_space_ids]},
    )

    # --- 3. 调用 MultiSpaceHybridRetriever 执行 Qdrant + PG 权威 ACL + Reranker + RRF ---
    captured_decisions: list[tuple[uuid.UUID, uuid.UUID, str, str, int | None]] = []
    captured_candidates: list[tuple[uuid.UUID, uuid.UUID, int, str, dict[str, Any]]] = []
    retrieval_start = time.perf_counter()

    async def _pg_authorize_chunks(candidate_ids: list[str]) -> ChunkAuthorizationResult:
        chunk_uuids = [uuid.UUID(cid) for cid in candidate_ids]
        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            batch_res = await deps.acl_engine.batch_authorize_chunks(
                session,
                org_ctx,
                chunk_uuids,
                organization_id=org_ctx.organization_id,
            )
            # 查出通过与拒绝的文档映射供检索审计明细留痕（绝不记录正文/标题）
            allowed_set = {str(cid) for cid in batch_res.allowed_chunk_ids}
            denied_set = {str(rec.chunk_id) for rec in batch_res.denied_records}
            doc_revs = {str(did): rev for did, rev in batch_res.doc_acl_revisions.items()}
            reason_codes: dict[str, str] = {}

            if batch_res.allowed_chunk_ids:
                allowed_doc_rows = (
                    await session.execute(
                        select(Chunk.id, DocumentVersion.document_id)
                        .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
                        .where(
                            Chunk.organization_id == org_ctx.organization_id,
                            Chunk.id.in_(batch_res.allowed_chunk_ids),
                        )
                    )
                ).all()
                for ch_id, doc_id in allowed_doc_rows:
                    rev = batch_res.doc_acl_revisions.get(doc_id)
                    captured_decisions.append((ch_id, doc_id, "ALLOW", "ACL_ALLOW", rev))
                    reason_codes[str(ch_id)] = "ACL_ALLOW"

            for rec in batch_res.denied_records:
                captured_decisions.append(
                    (rec.chunk_id, rec.document_id, "DENY", rec.reason_code, rec.acl_revision)
                )
                reason_codes[str(rec.chunk_id)] = rec.reason_code

            return ChunkAuthorizationResult(
                allowed_chunk_ids=allowed_set,
                denied_chunk_ids=denied_set,
                doc_acl_revisions=doc_revs,
                reason_codes=reason_codes,
            )

    async def _pg_load_authorized_chunks(allowed_ids: list[str]) -> list[AuthorizedChunkBody]:
        if not allowed_ids:
            return []
        allowed_uuids = [uuid.UUID(cid) for cid in allowed_ids]
        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            rows = (
                await session.execute(
                    select(Chunk, DocumentVersion, Document)
                    .join(DocumentVersion, DocumentVersion.id == Chunk.document_version_id)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(
                        Chunk.organization_id == org_ctx.organization_id,
                        Chunk.id.in_(allowed_uuids),
                    )
                )
            ).all()

            # 加载来源首块页码与坐标框信息
            first_block_ids: list[uuid.UUID] = []
            for chunk_row, _, _ in rows:
                if chunk_row.source_block_ids:
                    try:
                        first_block_ids.append(uuid.UUID(str(chunk_row.source_block_ids[0])))
                    except ValueError:
                        continue

            block_map: dict[uuid.UUID, ParsedBlock] = {}
            if first_block_ids:
                for blk in (
                    (
                        await session.execute(
                            select(ParsedBlock).where(
                                ParsedBlock.organization_id == org_ctx.organization_id,
                                ParsedBlock.id.in_(first_block_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                ):
                    block_map[blk.id] = blk

            bodies: list[AuthorizedChunkBody] = []
            for chunk_row, ver_row, doc_row in rows:
                blk_id: uuid.UUID | None = None
                bbox: list[float] | dict[str, Any] | None = None
                page_num: int | None = None
                if chunk_row.page_range and isinstance(chunk_row.page_range, dict):
                    sp = chunk_row.page_range.get("start_page")
                    if isinstance(sp, int):
                        page_num = sp

                if chunk_row.source_block_ids:
                    try:
                        cand_blk_id = uuid.UUID(str(chunk_row.source_block_ids[0]))
                        blk = block_map.get(cand_blk_id)
                        if blk is not None:
                            blk_id = blk.id
                            if page_num is None:
                                page_num = blk.page_number
                            bbox = blk.bounding_box
                    except ValueError:
                        pass

                meta_json = chunk_row.metadata_json or {}
                heading_path = (
                    [str(x) for x in meta_json.get("heading_path", [])]
                    if isinstance(meta_json.get("heading_path"), list)
                    else []
                )
                bodies.append(
                    AuthorizedChunkBody(
                        chunk_id=str(chunk_row.id),
                        document_id=str(doc_row.id),
                        document_version_id=str(ver_row.id),
                        space_id=str(doc_row.space_id),
                        document_title=doc_row.title,
                        content=chunk_row.content_text,
                        parent_chunk_id=(
                            str(chunk_row.parent_chunk_id) if chunk_row.parent_chunk_id else None
                        ),
                        page_number=page_num,
                        heading_path=heading_path,
                        block_id=str(blk_id) if blk_id else None,
                        bounding_box=bbox,
                        sensitivity_level=SensitivityLevel(doc_row.sensitivity_level),
                    )
                )
            return bodies

    retriever = MultiSpaceHybridRetriever(
        qdrant_store=deps.qdrant_store,
        embedding_router=deps.embedding_router,
        reranker_router=deps.reranker_router,
        context_builder=ContextBuilder(max_context_tokens=deps.context_max_tokens),
    )

    try:
        if space_targets:
            retrieval_res = await retriever.retrieve(
                query=retrieval_query,
                organization_id=str(org_ctx.organization_id),
                spaces=space_targets,
                authorize_chunks_fn=_pg_authorize_chunks,
                load_authorized_chunks_fn=_pg_load_authorized_chunks,
                org_allow_external_l1=org_allow_external_l1,
                allow_rerank_rrf_fallback=deps.allow_rerank_rrf_fallback,
                answer_policy=effective_policy,
                conversation_history=safe_history,
            )
            candidate_ids_all = retrieval_res.audit_trace.candidate_chunk_ids
            denied_count_total = retrieval_res.audit_trace.denied_count
            selected_items = list(retrieval_res.context.selected_items)
        else:
            candidate_ids_all = []
            denied_count_total = 0
            selected_items = []
    except Exception as exc:
        logger.warning("混合检索执行异常: conv=%s err=%s", conversation_id, type(exc).__name__)
        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            await session.execute(
                update(Message)
                .where(
                    Message.organization_id == org_ctx.organization_id,
                    Message.id == assistant_msg_id,
                )
                .values(status="FAILED", content="检索服务异常，问答已安全中止")
            )
            session.add(
                QuestionEvent(
                    organization_id=org_ctx.organization_id,
                    space_id=target_space_ids[0],
                    user_hash=user_hash,
                    session_hash=session_hash,
                    normalized_text=sanitize_question_text(raw_query),
                    sensitivity_level="L1",
                    outcome="PROVIDER_FAILURE",
                )
            )
        yield _format_sse_frame(
            "message_error",
            {
                "code": "RETRIEVAL_FAILED",
                "message": "知识检索执行失败，请稍后重试",
                "retryable": True,
            },
        )
        return

    # --- 4. 进入 LLM 前二次 ACL Revision 复核（防止检索到生成窗口期间撤权竞态） ---
    final_items: list[FusedEvidenceItem] = []
    if selected_items:
        selected_uuids = [uuid.UUID(item.chunk.chunk_id) for item in selected_items]
        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            recheck_res = await deps.acl_engine.batch_authorize_chunks(
                session,
                org_ctx,
                selected_uuids,
                organization_id=org_ctx.organization_id,
            )
            still_allowed = set(recheck_res.allowed_chunk_ids)
            for item in selected_items:
                c_uuid = uuid.UUID(item.chunk.chunk_id)
                d_uuid = uuid.UUID(item.chunk.document_id)
                if c_uuid not in still_allowed:
                    denied_count_total += 1
                    continue
                latest_rev = recheck_res.doc_acl_revisions.get(d_uuid)
                if latest_rev is not None and latest_rev != item.chunk.acl_revision:
                    item = item.model_copy(
                        update={"chunk": item.chunk.model_copy(update={"acl_revision": latest_rev})}
                    )
                final_items.append(item)

    retrieval_latency_ms = round((time.perf_counter() - retrieval_start) * 1000.0, 2)
    yield _format_sse_frame(
        "retrieval_completed",
        {
            "authorized_count": len(final_items),
            "latency_ms": retrieval_latency_ms,
        },
    )

    # 记录候选元数据（仅 ID 与排名，绝不记录未授权正文）
    for idx, cid_str in enumerate(candidate_ids_all, start=1):
        captured_candidates.append(
            (
                uuid.UUID(cid_str),
                target_space_ids[0],
                idx,
                "hybrid_rrf",
                {"rank": idx},
            )
        )

    # --- 5. 无授权结果分支（ACL_DENIED vs NO_RETRIEVAL） ---
    if not final_items:
        is_acl_denied = denied_count_total > 0
        outcome = "ACL_DENIED" if is_acl_denied else "NO_RETRIEVAL"
        reply_text = ACL_DENIED_SAFE_REPLY if is_acl_denied else NO_RETRIEVAL_SAFE_REPLY

        # 逐字/分段流式输出受控安全应答，绝不泄露受限文档标题或存在性
        yield _format_sse_frame("token", {"text": reply_text})

        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            await session.execute(
                update(Message)
                .where(
                    Message.organization_id == org_ctx.organization_id,
                    Message.id == assistant_msg_id,
                )
                .values(status="COMPLETED", content=reply_text)
            )
            session.add(
                MessageSegment(
                    organization_id=org_ctx.organization_id,
                    message_id=assistant_msg_id,
                    sequence=0,
                    content=reply_text,
                    segment_type="ABSTENTION",
                    source_document_ids=[],
                    sensitivity_level="L0",
                )
            )
            await _persist_retrieval_audit_and_question_event(
                session,
                organization_id=org_ctx.organization_id,
                message_id=assistant_msg_id,
                space_id=target_space_ids[0],
                query_hash=query_hash,
                user_hash=user_hash,
                session_hash=session_hash,
                raw_query=raw_query,
                authorized_count=0,
                denied_count=denied_count_total,
                latency_ms=int(retrieval_latency_ms),
                candidates=captured_candidates,
                decisions=captured_decisions,
                max_sensitivity="L0",
                outcome=outcome,
            )

        yield _format_sse_frame(
            "message_complete",
            {
                "message_id": str(assistant_msg_id),
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "finish_reason": "stop",
            },
        )
        return

    # --- 6. 有授权证据：生成 Citation 事件并流式调用 LLM ---
    rebuilt_context = ContextBuilder(max_context_tokens=deps.context_max_tokens).build(
        user_query=raw_query,
        items=final_items,
        answer_policy=effective_policy,
        conversation_history=safe_history,
    )
    selected_final = rebuilt_context.selected_items
    max_sens = highest_sensitivity(
        [item.chunk.sensitivity_level for item in selected_final],
        default=SensitivityLevel.L0,
    )

    prepared_citations: list[tuple[uuid.UUID, FusedEvidenceItem, str]] = []
    for item in selected_final:
        cit_uuid = new_uuid7()
        quote_text = item.chunk.content[:280].strip()
        prepared_citations.append((cit_uuid, item, quote_text))
        yield _format_sse_frame(
            "citation",
            {
                "citation_id": str(cit_uuid),
                "document_id": item.chunk.document_id,
                "document_version_id": item.chunk.document_version_id,
                "document_title": item.chunk.document_title,
                "page": item.chunk.page_number,
                "chunk_id": item.chunk.chunk_id,
                "quote": quote_text,
            },
        )

    streamed_tokens: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    total_latency_ms = 0
    finish_reason = "stop"
    was_cancelled = False

    try:
        async for chunk in deps.llm_provider.chat_stream(
            rebuilt_context.messages,
            sensitivity_level=max_sens,
            org_allow_external_l1=org_allow_external_l1,
            evidences=rebuilt_context.evidences,
            organization_id=str(org_ctx.organization_id),
        ):
            if deps.cancellation_registry.is_cancelled(assistant_msg_id):
                was_cancelled = True
                finish_reason = "cancelled"
                break
            if chunk.delta:
                streamed_tokens.append(chunk.delta)
                yield _format_sse_frame("token", {"text": chunk.delta})
            if chunk.is_final:
                total_latency_ms = int(chunk.latency_ms)
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
                if chunk.usage is not None:
                    prompt_tokens = chunk.usage.prompt_tokens
                    completion_tokens = chunk.usage.completion_tokens
    except PolicyBlockedError as exc:
        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            await session.execute(
                update(Message)
                .where(
                    Message.organization_id == org_ctx.organization_id,
                    Message.id == assistant_msg_id,
                )
                .values(status="FAILED", content="模型外发策略拦截")
            )
            record_security_audit_event(
                session,
                organization_id=org_ctx.organization_id,
                actor_user_id=org_ctx.principal.user_id,
                event_type="llm.egress_blocked",
                severity="HIGH",
                resource_type="message",
                resource_id=assistant_msg_id,
                details={"reason_code": exc.code, "sensitivity_level": max_sens.value},
            )
            await _persist_retrieval_audit_and_question_event(
                session,
                organization_id=org_ctx.organization_id,
                message_id=assistant_msg_id,
                space_id=target_space_ids[0],
                query_hash=query_hash,
                user_hash=user_hash,
                session_hash=session_hash,
                raw_query=raw_query,
                authorized_count=len(selected_final),
                denied_count=denied_count_total,
                latency_ms=int(retrieval_latency_ms),
                candidates=captured_candidates,
                decisions=captured_decisions,
                max_sensitivity=max_sens.value,
                outcome="ACL_DENIED",
            )
        yield _format_sse_frame(
            "message_error",
            {
                "code": exc.code,
                "message": exc.message,
                "retryable": False,
            },
        )
        return
    except Exception as exc:
        logger.warning("LLM 流式生成失败: msg=%s err=%s", assistant_msg_id, type(exc).__name__)
        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            await session.execute(
                update(Message)
                .where(
                    Message.organization_id == org_ctx.organization_id,
                    Message.id == assistant_msg_id,
                )
                .values(status="FAILED", content="模型生成异常")
            )
            await _persist_retrieval_audit_and_question_event(
                session,
                organization_id=org_ctx.organization_id,
                message_id=assistant_msg_id,
                space_id=target_space_ids[0],
                query_hash=query_hash,
                user_hash=user_hash,
                session_hash=session_hash,
                raw_query=raw_query,
                authorized_count=len(selected_final),
                denied_count=denied_count_total,
                latency_ms=int(retrieval_latency_ms),
                candidates=captured_candidates,
                decisions=captured_decisions,
                max_sensitivity=max_sens.value,
                outcome="PROVIDER_FAILURE",
            )
        yield _format_sse_frame(
            "message_error",
            {
                "code": "LLM_STREAM_ERROR",
                "message": "模型生成中断，请稍后重试",
                "retryable": True,
            },
        )
        return
    finally:
        deps.cancellation_registry.clear(assistant_msg_id)

    full_answer = "".join(streamed_tokens)
    final_status = "CANCELLED" if was_cancelled else "COMPLETED"
    unique_source_doc_ids = list(dict.fromkeys(item.chunk.document_id for item in selected_final))

    # --- 7. 持久化 Message / MessageSegment / AnswerCitation / LLMUsage / Audit / QuestionEvent ---
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        await session.execute(
            update(Message)
            .where(
                Message.organization_id == org_ctx.organization_id,
                Message.id == assistant_msg_id,
            )
            .values(status=final_status, content=full_answer)
        )
        seg = MessageSegment(
            id=new_uuid7(),
            organization_id=org_ctx.organization_id,
            message_id=assistant_msg_id,
            sequence=0,
            content=full_answer,
            segment_type="EVIDENCE",
            source_document_ids=unique_source_doc_ids,
            sensitivity_level=max_sens.value,
        )
        session.add(seg)
        await session.flush()

        for cit_uuid, item, _quote_text in prepared_citations:
            quote_sha = hashlib.sha256(item.chunk.content.encode("utf-8")).hexdigest()
            session.add(
                AnswerCitation(
                    id=cit_uuid,
                    organization_id=org_ctx.organization_id,
                    message_segment_id=seg.id,
                    message_id=assistant_msg_id,
                    document_id=uuid.UUID(item.chunk.document_id),
                    document_version_id=uuid.UUID(item.chunk.document_version_id),
                    chunk_id=uuid.UUID(item.chunk.chunk_id),
                    block_id=uuid.UUID(item.chunk.block_id) if item.chunk.block_id else None,
                    page_number=item.chunk.page_number,
                    bbox_json=item.chunk.bounding_box,
                    quote_hash=quote_sha,
                    acl_revision=item.chunk.acl_revision,
                )
            )

        session.add(
            LLMUsage(
                organization_id=org_ctx.organization_id,
                message_id=assistant_msg_id,
                provider=deps.llm_provider._settings.llm_provider,  # noqa: SLF001
                model=deps.llm_provider.default_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=total_latency_ms,
                cost_metadata={"sensitivity_level": max_sens.value},
            )
        )
        await _persist_retrieval_audit_and_question_event(
            session,
            organization_id=org_ctx.organization_id,
            message_id=assistant_msg_id,
            space_id=target_space_ids[0],
            query_hash=query_hash,
            user_hash=user_hash,
            session_hash=session_hash,
            raw_query=raw_query,
            authorized_count=len(selected_final),
            denied_count=denied_count_total,
            latency_ms=int(retrieval_latency_ms),
            candidates=captured_candidates,
            decisions=captured_decisions,
            max_sensitivity=max_sens.value,
            outcome="ANSWERED",
        )

    yield _format_sse_frame(
        "message_complete",
        {
            "message_id": str(assistant_msg_id),
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            "finish_reason": finish_reason,
        },
    )


async def _persist_retrieval_audit_and_question_event(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    message_id: uuid.UUID,
    space_id: uuid.UUID,
    query_hash: str,
    user_hash: str,
    session_hash: str,
    raw_query: str,
    authorized_count: int,
    denied_count: int,
    latency_ms: int,
    candidates: Sequence[tuple[uuid.UUID, uuid.UUID, int, str, dict[str, Any]]],
    decisions: Sequence[tuple[uuid.UUID, uuid.UUID, str, str, int | None]],
    max_sensitivity: str,
    outcome: str,
) -> None:
    """在同一 org_rls_session 内持久化检索审计、ACL 判定明细与脱敏 QuestionEvent。"""
    audit = RetrievalAudit(
        id=new_uuid7(),
        organization_id=organization_id,
        message_id=message_id,
        query_hash=query_hash,
        retrieval_config_version="hybrid-rrf-v1",
        policy_revision=1,
        latency_ms=latency_ms,
        authorized_count=authorized_count,
        denied_count=denied_count,
    )
    session.add(audit)
    await session.flush()

    for ch_id, sp_id, rank, src, meta in candidates:
        session.add(
            RetrievalCandidate(
                organization_id=organization_id,
                audit_id=audit.id,
                chunk_id=ch_id,
                space_id=sp_id,
                per_space_rank=rank,
                retrieval_source=src,
                score_metadata=meta,
            )
        )

    for ch_id, doc_id, decision_str, reason_code, acl_rev in decisions:
        session.add(
            RetrievalDecision(
                organization_id=organization_id,
                audit_id=audit.id,
                chunk_id=ch_id,
                document_id=doc_id,
                decision=decision_str,
                reason_code=reason_code,
                acl_revision=acl_rev,
            )
        )

    session.add(
        QuestionEvent(
            organization_id=organization_id,
            space_id=space_id,
            user_hash=user_hash,
            session_hash=session_hash,
            normalized_text=sanitize_question_text(raw_query),
            sensitivity_level=max_sensitivity,
            outcome=outcome,
        )
    )
