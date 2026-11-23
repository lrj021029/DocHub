"""DocHub FastAPI 应用入口与中间件装配。

职责：
1. 初始化结构化脱敏 JSON 日志。
2. 挂载严格 CORS 白名单、Request ID 追踪中间件、全局统一异常处理。
3. 挂载 `/api/v1` 业务主路由、Prometheus `/metrics` 与 OpenTelemetry Tracing。
4. 管理应用生命周期（停机时优雅关闭数据库连接池）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.v1.router import api_v1_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.ids import new_uuid7_str
from app.core.logging import configure_logging, request_id_ctx
from app.core.telemetry import configure_metrics, configure_telemetry
from app.db.session import dispose_runtime_engine

logger = logging.getLogger(__name__)

# 限制入站 X-Request-Id 格式，防止日志注入或超长头攻击
_VALID_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """为每个 HTTP 请求分配或验证 X-Request-Id，并绑定到上下文变量与响应头。"""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        incoming = request.headers.get("X-Request-Id", "").strip()
        req_id = incoming if _VALID_REQUEST_ID_PATTERN.match(incoming) else new_uuid7_str()

        request.state.request_id = req_id
        token = request_id_ctx.set(req_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)

        response.headers["X-Request-Id"] = req_id
        return response


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """管理 FastAPI 服务启动与优雅停机生命周期。"""
    logger.info("DocHub API 服务启动完成")
    try:
        yield
    finally:
        await dispose_runtime_engine()
        logger.info("DocHub API 服务已优雅关闭，数据库连接池已释放")


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建并配置 DocHub FastAPI 应用实例。"""
    cfg = settings or get_settings()
    configure_logging(cfg.log_level)

    application = FastAPI(
        title=cfg.app_name,
        version=cfg.app_version,
        debug=cfg.app_debug,
        lifespan=_lifespan,
        docs_url="/docs" if cfg.app_env != "production" else None,
        redoc_url="/redoc" if cfg.app_env != "production" else None,
    )
    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: cfg

    # 1. 严格 CORS 白名单中间件（配合 Refresh Cookie 的 credentials 策略）
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-CSRF-Token",
            "X-Organization-Id",
            "X-Request-Id",
            "Idempotency-Key",
        ],
        expose_headers=["X-Request-Id"],
    )

    # 2. Request ID 链路追踪中间件（放在最外层以确保异常响应也能拿到 request_id）
    application.add_middleware(RequestIdMiddleware)

    # 3. 注册全局统一异常处理器（杜绝堆栈与资源存在性泄漏）
    register_exception_handlers(application)

    # 4. 挂载 API v1 路由
    application.include_router(api_v1_router)

    # 5. 挂载可观测性（Prometheus /metrics 与 OpenTelemetry）
    configure_metrics(application)
    configure_telemetry(application, cfg)

    return application


# 默认 ASGI 应用对象，供 uvicorn app.main:app 加载
app = create_app()
