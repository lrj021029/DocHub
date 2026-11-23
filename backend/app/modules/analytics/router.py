"""运营分析与 Benchmark 评测 HTTP 路由（`/api/v1/analytics/*` 与 `/api/v1/benchmarks`）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency import validate_idempotency_key
from app.db.session import get_db_session
from app.modules.analytics.schemas import (
    AnalyticsOverviewResponse,
    BenchmarkRunResponse,
    BenchmarkTriggerRequest,
    FaqAnalyticsResponse,
    GapAnalyticsResponse,
)
from app.modules.analytics.service import (
    get_analytics_overview,
    get_faq_analytics,
    get_gap_analytics,
    list_benchmark_runs,
    trigger_benchmark_run,
)
from app.modules.identity.deps import OrgContext, require_permission

analytics_router = APIRouter(prefix="/analytics", tags=["Analytics"])
benchmarks_router = APIRouter(prefix="/benchmarks", tags=["Benchmarks"])


@analytics_router.get(
    "/overview",
    response_model=AnalyticsOverviewResponse,
    summary="查询当前组织运营总览指标",
)
async def analytics_overview(
    org_ctx: OrgContext = Depends(require_permission("analytics:read")),
    session: AsyncSession = Depends(get_db_session),
) -> AnalyticsOverviewResponse:
    """聚合当前组织真实知识资产、FAQ 命中与缺口指标。"""
    return await get_analytics_overview(org_ctx=org_ctx, session=session)


@analytics_router.get(
    "/faq",
    response_model=FaqAnalyticsResponse,
    summary="查询 FAQ 运营细分指标",
)
async def analytics_faq(
    org_ctx: OrgContext = Depends(require_permission("analytics:read")),
    session: AsyncSession = Depends(get_db_session),
) -> FaqAnalyticsResponse:
    """聚合当前组织 FAQ 候选、已发布与来源健康度指标。"""
    return await get_faq_analytics(org_ctx=org_ctx, session=session)


@analytics_router.get(
    "/knowledge-gaps",
    response_model=GapAnalyticsResponse,
    summary="查询 Knowledge Gap 运营细分指标",
)
async def analytics_gaps(
    org_ctx: OrgContext = Depends(require_permission("analytics:read")),
    session: AsyncSession = Depends(get_db_session),
) -> GapAnalyticsResponse:
    """聚合当前组织知识缺口与闭环解决率指标。"""
    return await get_gap_analytics(org_ctx=org_ctx, session=session)


@benchmarks_router.get(
    "",
    response_model=list[BenchmarkRunResponse],
    summary="查询历史 Benchmark 运行记录",
)
async def get_benchmarks(
    org_ctx: OrgContext = Depends(require_permission("analytics:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[BenchmarkRunResponse]:
    """查询当前组织的 ACL/RAG/Parser Benchmark 评测记录。"""
    return await list_benchmark_runs(org_ctx=org_ctx, session=session)


@benchmarks_router.post(
    "",
    response_model=BenchmarkRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="触发组织级 ACL/RAG 回归 Benchmark 检查",
)
async def post_benchmark(
    payload: BenchmarkTriggerRequest = BenchmarkTriggerRequest(),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    org_ctx: OrgContext = Depends(require_permission("analytics:read")),
    session: AsyncSession = Depends(get_db_session),
) -> BenchmarkRunResponse:
    """执行真实安全门禁与检索质量基准校验并持久化结果。"""
    validated_key = validate_idempotency_key(idempotency_key, required=False)
    return await trigger_benchmark_run(
        org_ctx=org_ctx,
        payload=payload,
        idempotency_key=validated_key,
        session=session,
    )
