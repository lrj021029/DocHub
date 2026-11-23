"""FAQ 挖掘、三轴生命周期、人工审核、发布与 Fast Path 匹配 Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FaqLifecycleState = Literal["CANDIDATE", "PENDING_REVIEW", "REJECTED", "ARCHIVED"]
FaqPublicationState = Literal["UNPUBLISHED", "PUBLISHED", "DISABLED"]
FaqSourceHealthState = Literal["HEALTHY", "STALE", "EXPIRED"]


class FaqMiningRunRequest(BaseModel):
    """手动触发 FAQ 挖掘请求体。"""

    space_id: uuid.UUID | None = Field(default=None, description="可选限定单个知识空间")
    window_days: int = Field(default=30, ge=1, le=365, description="回溯统计时间窗口（天）")
    min_cluster_size: int = Field(default=3, ge=2, le=50, description="HDBSCAN 最小成簇问题数")


class FaqMiningRunResponse(BaseModel):
    """FAQ 挖掘任务执行结果。"""

    run_id: uuid.UUID
    status: str
    processed_events: int
    clusters_created: int
    candidates_created: int


class FaqCandidateItem(BaseModel):
    """FAQ 挖掘候选队列条目（字段兼容前端 FaqCandidateItem）。"""

    id: uuid.UUID
    faq_id: uuid.UUID | None = None
    cluster_id: uuid.UUID
    space_id: uuid.UUID | None = None
    question: str
    answer_draft: str
    frequency_count: int
    unique_user_count: int
    confidence_score: float
    lifecycle_state: FaqLifecycleState
    candidate_status: str
    source_citation_count: int
    created_by: uuid.UUID | None = None
    created_at: datetime


class FaqReviewRequest(BaseModel):
    """FAQ 人工审核决策请求体。"""

    decision: Literal["APPROVE", "APPROVED", "REJECT", "REJECTED"] = Field(
        ...,
        description="审核结论",
    )
    comments: str = Field(default="", max_length=1000, description="审核意见")


class FaqSourceRefInput(BaseModel):
    """创建或关联 FAQ 答案来源引用（用于多来源最严交集 ACL 与健康度追踪）。"""

    document_id: uuid.UUID
    document_version_id: uuid.UUID
    chunk_id: uuid.UUID | None = None
    citation_ref: str | None = Field(default=None, max_length=255)


class ManualFaqCreateRequest(BaseModel):
    """手工创建 MANUAL FAQ 请求体。"""

    question: str = Field(..., min_length=2, max_length=1000, description="标准问题")
    answer: str = Field(..., min_length=2, max_length=20000, description="结构化答案正文")
    evidence: str | None = Field(
        default=None,
        min_length=2,
        max_length=4000,
        description="必填事实依据说明（兼容 fact_basis）",
    )
    fact_basis: str | None = Field(default=None, min_length=2, max_length=4000)
    maintainer: str = Field(..., min_length=1, max_length=255, description="长期维护责任人")
    expires_at: datetime = Field(..., description="有效期截止时间（UTC）")
    space_id: uuid.UUID | None = Field(default=None, description="所属知识空间 ID")
    source_refs: list[FaqSourceRefInput] = Field(
        default_factory=list,
        description="关联来源文档与版本引用（用于严格来源权限交集校验）",
    )


class FaqItemResponse(BaseModel):
    """FAQ 三轴状态台账响应条目（严格解耦 Lifecycle / Publication / Source Health）。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    space_id: uuid.UUID
    question: str
    answer_summary: str
    version_no: int
    lifecycle_state: FaqLifecycleState
    publication_state: FaqPublicationState
    source_health_state: FaqSourceHealthState
    is_manual: bool
    maintainer: str | None = None
    expires_at: datetime | None = None
    cache_status: str = "SYNCED"
    cache_hit_count: int = 0
    acl_revision: int = 1
    created_by: uuid.UUID | None = None
    created_at: datetime


class FaqFastPathMatchRequest(BaseModel):
    """FAQ Fast Path 命中测试请求体。"""

    question: str = Field(..., min_length=1, max_length=2000)
    space_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=20)
    min_score: float = Field(default=0.55, ge=0.0, le=1.0)


class FaqFastPathMatchResponse(BaseModel):
    """FAQ Fast Path 匹配结果（未命中或 STALE/无权时 hit=false 回落完整 RAG）。"""

    hit: bool
    reason: str
    faq_id: uuid.UUID | None = None
    faq_version_id: uuid.UUID | None = None
    question: str | None = None
    answer: str | None = None
    score: float = 0.0
    from_cache: bool = False
    acl_revision: int | None = None
    source_citations: list[dict[str, Any]] = Field(default_factory=list)
