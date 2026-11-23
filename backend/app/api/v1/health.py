"""存活探针（/healthz）与真实依赖连通性就绪探针（/readyz）。

严格遵循工程红线（ADR-001）：
- `/api/v1/healthz` 仅检查 API 进程存活状态。
- `/api/v1/readyz` 必须并发执行真实依赖探活（PostgreSQL、Redis、MinIO、Qdrant），
  返回逐项状态与真实耗时；任一核心依赖不可用必须返回 HTTP 503，绝不伪造 200。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from typing import Literal

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.logging import redact_text
from app.db.session import runtime_engine

router = APIRouter(tags=["Health"])


class HealthzResponse(BaseModel):
    """存活探针响应体。"""

    status: Literal["ok"] = Field(default="ok", description="进程存活状态")
    service: str = Field(..., description="服务名称")
    version: str = Field(..., description="服务版本")
    environment: str = Field(..., description="运行环境")
    timestamp: datetime = Field(..., description="UTC 检查时间")


class DependencyHealth(BaseModel):
    """单个核心基础设施依赖的探活结果。"""

    status: Literal["up", "down"] = Field(..., description="依赖可用性状态")
    latency_ms: float = Field(..., ge=0.0, description="本次探活耗时（毫秒）")
    detail: str | None = Field(default=None, description="脱敏后的状态或失败摘要")


class ReadyzResponse(BaseModel):
    """就绪探针响应体（聚合全部核心依赖状态）。"""

    status: Literal["ready", "degraded"] = Field(..., description="整体就绪状态")
    service: str = Field(..., description="服务名称")
    version: str = Field(..., description="服务版本")
    timestamp: datetime = Field(..., description="UTC 检查时间")
    checks: dict[str, DependencyHealth] = Field(..., description="逐项核心依赖探活明细")


def _safe_error_summary(exc: Exception) -> str:
    """提取不含密钥或凭据的安全异常摘要。"""
    if isinstance(exc, TimeoutError):
        return "连接或响应超时"
    raw = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return redact_text(raw)[:200]


async def check_postgres(settings: Settings) -> DependencyHealth:
    """真实探活 PostgreSQL 运行期连接池（SELECT 1）。"""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(settings.readiness_check_timeout_seconds):
            async with runtime_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return DependencyHealth(status="up", latency_ms=latency_ms, detail="ok")
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return DependencyHealth(
            status="down",
            latency_ms=latency_ms,
            detail=_safe_error_summary(exc),
        )


async def check_redis(settings: Settings) -> DependencyHealth:
    """真实探活 Redis 服务（PING）。"""
    started = time.perf_counter()
    timeout = settings.readiness_check_timeout_seconds
    client = aioredis.from_url(
        settings.redis_cache_url.get_secret_value(),
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )
    try:
        async with asyncio.timeout(timeout):
            pong = await client.ping()
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        if pong:
            return DependencyHealth(status="up", latency_ms=latency_ms, detail="ok")
        return DependencyHealth(status="down", latency_ms=latency_ms, detail="PING 未返回预期响应")
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return DependencyHealth(
            status="down",
            latency_ms=latency_ms,
            detail=_safe_error_summary(exc),
        )
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


async def check_minio(settings: Settings) -> DependencyHealth:
    """真实探活 MinIO 对象存储（HTTP /minio/health/live）。"""
    started = time.perf_counter()
    timeout = settings.readiness_check_timeout_seconds
    scheme = "https" if settings.minio_secure else "http"
    url = f"{scheme}://{settings.minio_endpoint}/minio/health/live"
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        if resp.status_code == status.HTTP_200_OK:
            return DependencyHealth(status="up", latency_ms=latency_ms, detail="ok")
        return DependencyHealth(
            status="down",
            latency_ms=latency_ms,
            detail=f"HTTP {resp.status_code}",
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return DependencyHealth(
            status="down",
            latency_ms=latency_ms,
            detail=_safe_error_summary(exc),
        )


async def check_qdrant(settings: Settings) -> DependencyHealth:
    """真实探活 Qdrant 向量数据库（HTTP /readyz）。"""
    started = time.perf_counter()
    timeout = settings.readiness_check_timeout_seconds
    url = f"{settings.qdrant_http_url}/readyz"
    headers: dict[str, str] = {}
    if settings.qdrant_api_key and settings.qdrant_api_key.get_secret_value():
        headers["api-key"] = settings.qdrant_api_key.get_secret_value()

    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        if resp.status_code == status.HTTP_200_OK:
            return DependencyHealth(status="up", latency_ms=latency_ms, detail="ok")
        return DependencyHealth(
            status="down",
            latency_ms=latency_ms,
            detail=f"HTTP {resp.status_code}",
        )
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return DependencyHealth(
            status="down",
            latency_ms=latency_ms,
            detail=_safe_error_summary(exc),
        )


@router.get(
    "/healthz",
    response_model=HealthzResponse,
    summary="服务存活探针",
    description="检查 FastAPI 服务进程是否存活，不触发外部依赖网络调用。",
)
async def healthz(settings: Settings = Depends(get_settings)) -> HealthzResponse:
    """返回服务进程存活状态。"""
    return HealthzResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        timestamp=datetime.now(UTC),
    )


@router.get(
    "/readyz",
    response_model=ReadyzResponse,
    responses={
        status.HTTP_200_OK: {"description": "全部核心依赖就绪"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "存在不可用的核心依赖"},
    },
    summary="核心依赖真实连通性就绪探针",
    description="并发真实探活 PostgreSQL、Redis、MinIO 与 Qdrant；任意核心依赖不可用时返回 503。",
)
async def readyz(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> ReadyzResponse:
    """并发检查全部核心基础设施依赖的真实连通性与延时。"""
    pg_res, redis_res, minio_res, qdrant_res = await asyncio.gather(
        check_postgres(settings),
        check_redis(settings),
        check_minio(settings),
        check_qdrant(settings),
    )

    checks = {
        "postgresql": pg_res,
        "redis": redis_res,
        "minio": minio_res,
        "qdrant": qdrant_res,
    }
    all_ready = all(item.status == "up" for item in checks.values())
    if not all_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadyzResponse(
        status="ready" if all_ready else "degraded",
        service=settings.app_name,
        version=settings.app_version,
        timestamp=datetime.now(UTC),
        checks=checks,
    )
