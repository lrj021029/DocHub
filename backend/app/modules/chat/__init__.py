"""AI 对话领域模块：会话、流式 SSE 问答编排、Query 改写、回答策略与历史重鉴权。"""

from app.modules.chat.router import get_chat_runtime_deps
from app.modules.chat.router import router as chat_router
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
from app.modules.chat.service import (
    CancellationRegistry,
    ChatRuntimeDeps,
    QueryRewriter,
    cancel_chat_message,
    create_conversation,
    get_conversation_detail,
    list_conversations,
    sanitize_question_text,
    stream_rag_chat_events,
    submit_message_feedback,
)

__all__ = [
    "CancellationRegistry",
    "ChatMessageResponse",
    "ChatRuntimeDeps",
    "ChatStreamRequest",
    "ConversationCreateRequest",
    "ConversationCursorPage",
    "ConversationDetailResponse",
    "ConversationSummaryResponse",
    "MessageCancelResponse",
    "MessageCitationResponse",
    "MessageFeedbackRequest",
    "MessageFeedbackResponse",
    "MessageSegmentResponse",
    "QueryRewriter",
    "cancel_chat_message",
    "chat_router",
    "create_conversation",
    "get_chat_runtime_deps",
    "get_conversation_detail",
    "list_conversations",
    "sanitize_question_text",
    "stream_rag_chat_events",
    "submit_message_feedback",
]
