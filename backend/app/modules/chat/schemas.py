"""AI 对话、流式 SSE、反馈与历史动态重鉴权 Pydantic 契约定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.modules.providers.llm import AnswerPolicyMode


class ConversationCreateRequest(BaseModel):
    """创建多轮对话会话请求体。"""

    title: str | None = Field(default=None, max_length=255, description="可选会话标题")
    space_ids: list[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="绑定的知识空间 ID 列表（均须属于当前组织且满足入口权限）",
    )


class ConversationSummaryResponse(BaseModel):
    """会话列表摘要项（对齐前端 `ConversationSummary`）。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    title: str
    space_ids: list[uuid.UUID] = Field(default_factory=list)
    status: str
    created_at: datetime
    updated_at: datetime


class ConversationCursorPage(BaseModel):
    """会话列表 Cursor 分页响应。"""

    items: list[ConversationSummaryResponse] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class MessageSegmentResponse(BaseModel):
    """可独立重鉴权的消息段落响应（对齐前端 `MessageSegmentItem`）。"""

    id: uuid.UUID
    sequence: int
    segment_type: str
    content: str
    redacted: bool = False
    redacted_reason: str | None = None


class MessageCitationResponse(BaseModel):
    """消息附带的已授权引用摘要（对齐前端 `CitationData`）。"""

    citation_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    document_title: str
    page: int | None = None
    page_number: int | None = None
    chunk_id: uuid.UUID
    block_id: uuid.UUID | None = None
    quote: str | None = None


class ChatMessageResponse(BaseModel):
    """会话消息响应（含段落级/整条动态重鉴权状态）。"""

    id: uuid.UUID
    role: Literal["user", "assistant", "system"]
    content: str
    status: str
    redacted: bool = False
    is_splittable: bool = True
    segments: list[MessageSegmentResponse] = Field(default_factory=list)
    citations: list[MessageCitationResponse] = Field(default_factory=list)
    feedback: Literal["up", "down"] | None = None
    created_at: datetime


class ConversationDetailResponse(BaseModel):
    """会话详情响应（包含按当前时刻 ACL 重鉴权后的历史消息）。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    title: str
    space_ids: list[uuid.UUID] = Field(default_factory=list)
    status: str
    messages: list[ChatMessageResponse] = Field(default_factory=list)


class ChatStreamRequest(BaseModel):
    """POST Fetch SSE 问答请求体（同时兼容 `query` 与 `content` 字段）。"""

    query: str | None = Field(default=None, max_length=4000, description="用户提问正文")
    content: str | None = Field(default=None, max_length=4000, description="提问正文别名")
    space_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=20,
        description="可选覆盖本轮检索的 Space ID 列表（缺省沿用会话绑定的 Space）",
    )
    is_splittable: bool = Field(
        default=True,
        description="是否支持按段落拆分重鉴权；False 时任一来源撤权即整条隐藏",
    )
    answer_policy: AnswerPolicyMode | None = Field(
        default=None,
        description="可选覆盖回答策略（缺省遵循知识空间配置）",
    )

    @model_validator(mode="after")
    def _validate_question(self) -> ChatStreamRequest:
        text = (self.query or self.content or "").strip()
        if not text:
            raise ValueError("提问内容 query 不能为空")
        return self

    def resolved_query(self) -> str:
        """返回去空白后的真实用户原始问题。"""
        return (self.query or self.content or "").strip()


class MessageCancelResponse(BaseModel):
    """取消流式生成响应。"""

    message_id: uuid.UUID
    status: str
    cancelled: bool


class MessageFeedbackRequest(BaseModel):
    """用户回答质量反馈请求体。"""

    rating: Literal["up", "down", "HELPFUL", "UNHELPFUL"] = Field(
        ...,
        description="反馈评价（up/HELPFUL 或 down/UNHELPFUL）",
    )
    comment: str | None = Field(default=None, max_length=500, description="可选反馈说明")


class MessageFeedbackResponse(BaseModel):
    """反馈保存回执。"""

    message_id: uuid.UUID
    rating: Literal["HELPFUL", "UNHELPFUL"]
    comment: str | None = None
