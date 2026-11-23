"""AI 问答与会话 HTTP 路由（`/api/v1/chat/*`）。"""

from __future__ import annotations

import uuid
from functools import lru_cache

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.chat.schemas import (
    ChatStreamRequest,
    ConversationCreateRequest,
    ConversationCursorPage,
    ConversationDetailResponse,
    ConversationSummaryResponse,
    MessageCancelResponse,
    MessageFeedbackRequest,
    MessageFeedbackResponse,
)
from app.modules.chat.service import (
    CancellationRegistry,
    ChatRuntimeDeps,
    cancel_chat_message,
    create_conversation,
    get_conversation_detail,
    list_conversations,
    stream_rag_chat_events,
    submit_message_feedback,
)
from app.modules.embeddings.providers import EmbeddingRouter
from app.modules.identity.deps import OrgContext, require_permission
from app.modules.indexing.qdrant_store import QdrantIndexStore
from app.modules.providers.llm import OpenAICompatibleLLMProvider
from app.modules.providers.reranker import RerankerRouter

router = APIRouter(prefix="/chat", tags=["Chat"])

_CANCELLATION_REGISTRY = CancellationRegistry()


@lru_cache(maxsize=1)
def get_chat_runtime_deps() -> ChatRuntimeDeps:
    """获取进程级缓存的 Chat RAG 运行期依赖（可在测试中通过 dependency_overrides 注入）。"""
    settings = get_settings()
    return ChatRuntimeDeps(
        qdrant_store=QdrantIndexStore(settings=settings),
        embedding_router=EmbeddingRouter(settings=settings),
        reranker_router=RerankerRouter(settings=settings),
        llm_provider=OpenAICompatibleLLMProvider(settings=settings),
        acl_engine=AuthoritativeACLEngine(),
        cancellation_registry=_CANCELLATION_REGISTRY,
    )


@router.post(
    "/conversations",
    response_model=ConversationSummaryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建受空间入口权限保护的 AI 问答会话",
)
async def post_create_conversation(
    payload: ConversationCreateRequest,
    org_ctx: OrgContext = Depends(require_permission("chat:use")),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummaryResponse:
    """校验全部选中 KnowledgeSpace 所属组织与入口权限后创建会话。"""
    return await create_conversation(
        org_ctx=org_ctx,
        payload=payload,
        session=session,
    )


@router.get(
    "/conversations",
    response_model=ConversationCursorPage,
    summary="分页查询当前用户的会话列表（Cursor 分页）",
)
async def get_list_conversations(
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
    org_ctx: OrgContext = Depends(require_permission("chat:use")),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationCursorPage:
    """使用 Cursor 分页获取当前用户在当前组织的会话列表。"""
    return await list_conversations(
        org_ctx=org_ctx,
        session=session,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    summary="获取会话详情并触发段落级/整条历史动态重鉴权",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("chat:use")),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationDetailResponse:
    """每次打开历史会话均按当前最新 ACL 实时校验各段落来源，失权即时脱敏。"""
    return await get_conversation_detail(
        org_ctx=org_ctx,
        conversation_id=conversation_id,
        session=session,
    )


@router.post(
    "/conversations/{conversation_id}/messages:stream",
    summary="发起受 ACL 保护的混合检索问答（POST Fetch SSE 流）",
)
async def post_stream_message(
    conversation_id: uuid.UUID,
    payload: ChatStreamRequest,
    org_ctx: OrgContext = Depends(require_permission("chat:use")),
    session: AsyncSession = Depends(get_db_session),
    deps: ChatRuntimeDeps = Depends(get_chat_runtime_deps),
) -> StreamingResponse:
    """以 POST Fetch SSE 依次推送检索状态、已授权引用、Token 流与审计用量。"""
    event_stream = stream_rag_chat_events(
        org_ctx=org_ctx,
        conversation_id=conversation_id,
        payload=payload,
        session=session,
        deps=deps,
    )
    return StreamingResponse(
        event_stream,
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/messages/{message_id}/cancel",
    response_model=MessageCancelResponse,
    summary="取消正在生成的助手回答流",
)
async def post_cancel_message(
    message_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("chat:use")),
    session: AsyncSession = Depends(get_db_session),
    deps: ChatRuntimeDeps = Depends(get_chat_runtime_deps),
) -> MessageCancelResponse:
    """标记指定助手消息取消生成。"""
    return await cancel_chat_message(
        org_ctx=org_ctx,
        message_id=message_id,
        session=session,
        cancellation_registry=deps.cancellation_registry,
    )


@router.post(
    "/messages/{message_id}/feedback",
    response_model=MessageFeedbackResponse,
    summary="提交助手回答质量反馈（HELPFUL / UNHELPFUL）",
)
async def post_message_feedback(
    message_id: uuid.UUID,
    payload: MessageFeedbackRequest,
    org_ctx: OrgContext = Depends(require_permission("chat:use")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageFeedbackResponse:
    """落盘或更新用户对指定回答的评价。"""
    return await submit_message_feedback(
        org_ctx=org_ctx,
        message_id=message_id,
        payload=payload,
        session=session,
    )
