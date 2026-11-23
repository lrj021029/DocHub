"""运营分析与 Benchmark 评测 Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class AnalyticsOverviewResponse(BaseModel):
    """当前组织总览运营看板指标（零受限文档泄露）。"""

    pv: int = 0
    uv: int = 0
    active_conversations: int = 0
    authorized_spaces: int = 0
    active_documents: int = 0
    active_chunks: int = 0
    faq_hit_rate: float = 0.0
    faq_direct_answer_rate: float = 0.0
    stale_faq_ratio: float = 0.0
    open_gap_count: int = 0
    gap_resolution_rate: float = 0.0
    p95_first_token_ms: float | None = None
    p95_total_latency_ms: float | None = None
    total_tokens: int = 0


class FaqAnalyticsResponse(BaseModel):
    """FAQ 运营细分指标。"""

    candidate_count: int = 0
    published_count: int = 0
    stale_count: int = 0
    hit_rate: float = 0.0
    helpful_rate: float = 0.0


class GapAnalyticsResponse(BaseModel):
    """Knowledge Gap 运营细分指标。"""

    total_gaps: int = 0
    resolved_gaps: int = 0
    resolution_rate: float = 0.0
    avg_resolution_hours: float | None = None


class BenchmarkCaseItem(BaseModel):
    """单条 Benchmark 用例结果。"""

    id: uuid.UUID
    case_name: str
    passed: bool
    metrics_json: dict[str, Any] = Field(default_factory=dict)
    detail_json: dict[str, Any] = Field(default_factory=dict)


class BenchmarkRunResponse(BaseModel):
    """Benchmark 评测运行摘要（包含 ACL Violation = 0 硬门槛指标）。"""

    id: uuid.UUID
    suite_type: str
    status: str
    recall_at_k: float | None = None
    mrr: float | None = None
    citation_accuracy: float | None = None
    acl_violations: int = 0
    unauthorized_prompt_chunks: int = 0
    cross_org_leaks: int = 0
    metrics_json: dict[str, Any] = Field(default_factory=dict)
    cases: list[BenchmarkCaseItem] = Field(default_factory=list)
    created_at: datetime


class BenchmarkTriggerRequest(BaseModel):
    """手动触发 Benchmark 评测请求体。"""

    suite_type: Literal["RAG_ACL_REGRESSION", "PARSER_REGRESSION", "ACL"] = Field(
        default="RAG_ACL_REGRESSION",
        description="评测套件类型",
    )
