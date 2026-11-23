"""Knowledge Gap 闭环治理 API 请求与响应 Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

GAP_ADMITTED_OUTCOMES: frozenset[str] = frozenset(
    {
        "NO_RETRIEVAL",
        "LOW_RELEVANCE",
        "INSUFFICIENT_EVIDENCE",
        "INSUFFICIENT_AUTHORIZED_EVIDENCE",
        "USER_NEGATIVE_FEEDBACK",
    }
)
GAP_SECURITY_REJECTED_OUTCOMES: frozenset[str] = frozenset(
    {"ACL_DENIED", "UNAUTHORIZED_ONLY_RESULTS"}
)
GAP_OPERATIONAL_REJECTED_OUTCOMES: frozenset[str] = frozenset(
    {"LLM_PROVIDER_FAILURE", "MINERU_FAILURE", "PROVIDER_FAILURE"}
)

GAP_11_STATES: tuple[str, ...] = (
    "DETECTED",
    "TRIAGED",
    "ASSIGNED",
    "IN_PROGRESS",
    "CONTENT_SUBMITTED",
    "REVIEWING",
    "RESOLVED",
    "REJECTED",
    "DUPLICATED",
    "CLOSED",
    "REOPENED",
)

GAP_TASK_ALLOWED_STATES: frozenset[str] = frozenset(
    {
        "ASSIGNED",
        "IN_PROGRESS",
        "CONTENT_SUBMITTED",
        "REVIEWING",
        "RESOLVED",
        "CLOSED",
    }
)


class GapSignalInput(BaseModel):
    """问答或检索链路触发的潜在知识缺口信号入参。"""

    space_id: uuid.UUID | None = Field(default=None, description="目标知识空间 ID")
    user_id: uuid.UUID | None = Field(default=None, description="提问用户 ID（仅用于单向哈希）")
    session_id: str | None = Field(default=None, description="会话 ID（仅用于单向哈希）")
    department_id: uuid.UUID | None = Field(default=None, description="主部门 ID")
    question_text: str = Field(..., min_length=1, max_length=2000, description="原始问题文本")
    outcome: str = Field(..., min_length=2, max_length=64, description="检索/问答归因结果")
    sensitivity_level: Literal["L0", "L1", "L2", "L3"] = Field(
        default="L1",
        description="敏感等级",
    )
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = Field(
        default="MEDIUM",
        description="初始优先级",
    )
    cluster_id: uuid.UUID | None = Field(default=None, description="关联问题聚类簇 ID")
    metadata: dict[str, Any] = Field(default_factory=dict, description="附加脱敏上下文")


class GapSignalIngestionResult(BaseModel):
    """缺口准入分类器处理结果。"""

    admitted: bool = Field(..., description="是否准入进入 knowledge_gaps 表")
    action: Literal[
        "ADMITTED_TO_GAP",
        "ROUTED_TO_SECURITY_AUDIT",
        "SKIPPED_OPERATIONAL_FAILURE",
    ]
    gap_id: uuid.UUID | None = Field(default=None, description="创建或聚合的 KnowledgeGap ID")
    question_event_id: uuid.UUID | None = Field(default=None, description="脱敏事件 ID")
    redacted_text: str = Field(..., description="PII 脱敏后的标准问题文本")


class KnowledgeGapTaskItem(BaseModel):
    """知识缺口整改任务响应条目。"""

    id: uuid.UUID
    gap_id: uuid.UUID
    assignee_id: uuid.UUID | None = None
    assignee_name: str | None = None
    priority: str
    status: str
    due_at: datetime | None = None
    related_document_id: uuid.UUID | None = None
    related_faq_id: uuid.UUID | None = None
    regression_passed: bool | None = None
    resolution_evidence_json: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGapItem(BaseModel):
    """知识缺口详情与任务列表响应条目。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    space_id: uuid.UUID | None = None
    space_name: str | None = None
    topic: str
    event_type: str = "NO_RETRIEVAL"
    occurrence_count: int
    unique_user_count: int
    status: str
    priority: str
    tasks: list[KnowledgeGapTaskItem] = Field(default_factory=list)
    last_seen_at: datetime
    created_at: datetime


class KnowledgeGapListResponse(BaseModel):
    """知识缺口列表响应。"""

    items: list[KnowledgeGapItem]
    total: int


class KnowledgeGapTaskCreateRequest(BaseModel):
    """创建知识缺口整改任务请求体。"""

    assignee_id: uuid.UUID | None = Field(default=None, description="整改负责人 ID")
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = Field(
        default="MEDIUM",
        description="任务优先级",
    )
    due_at: datetime | None = Field(default=None, description="整改截止时间")
    related_document_id: uuid.UUID | None = Field(default=None, description="关联补充文档 ID")
    related_faq_id: uuid.UUID | None = Field(default=None, description="关联补充 FAQ ID")


class KnowledgeGapTaskUpdateRequest(BaseModel):
    """更新知识缺口整改任务状态与触发回归验证请求体。"""

    status: str | None = Field(default=None, description="目标状态（11 态/任务状态校验）")
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None
    assignee_id: uuid.UUID | None = None
    due_at: datetime | None = None
    related_document_id: uuid.UUID | None = None
    related_faq_id: uuid.UUID | None = None
    run_regression: bool = Field(default=False, description="是否触发知识缺口回归验证查询")
    regression_query: str | None = Field(
        default=None,
        description="可选自定义回归测试问句，默认使用所属 KnowledgeGap.title",
    )


class KnowledgeGapStatusUpdateRequest(BaseModel):
    """独立变更 KnowledgeGap 状态请求体（禁止绕过回归验证直接改为 RESOLVED）。"""

    status: str = Field(..., description="目标 11 态状态")
    reason: str | None = Field(default=None, max_length=500, description="状态变更说明")
