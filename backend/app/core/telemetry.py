"""OpenTelemetry 链路追踪与 Prometheus 指标挂载基础设施。

设计要点：
1. 生产/联调环境通过 OTLP gRPC 将 Span 推送至 OpenTelemetry Collector -> Tempo。
2. 通过 `prometheus-fastapi-instrumentator` 暴露 `/metrics` 端点供 Prometheus 抓取。
3. 兼容 FastAPI 0.163+ 引入的 `_IncludedRouter` 路由结构，避免监控中间件解析路由模板报错。
4. 在单测或未启动 Collector 的本地环境下安全降级，绝不阻断 API 主进程启动。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_fastapi_instrumentator import routing as prom_routing
from starlette.routing import Match, Mount
from starlette.types import Scope

from app.core.config import Settings

logger = logging.getLogger(__name__)

_TRACER_INITIALIZED = False
_PROM_ROUTING_PATCHED = False


def _install_prometheus_routing_compat() -> None:
    """兼容 FastAPI 0.163+ `_IncludedRouter` 对象的路由模板名解析补丁。

    说明：FastAPI 0.163+ 在 `app.include_router` 时会将子路由封装为 `_IncludedRouter`，
    其不包含 `.path` 属性，而是通过 `effective_route_contexts()` 暴露展开后的路由上下文。
    此处安全扩展 `prometheus_fastapi_instrumentator.routing._get_route_name`，
    使其同时兼容传统 Route/Mount 与 `_IncludedRouter`。
    """
    global _PROM_ROUTING_PATCHED
    if _PROM_ROUTING_PATCHED:
        return

    def _compat_get_route_name(
        scope: Scope,
        routes: list[Any],
        route_name: str | None = None,
    ) -> str | None:
        for route in routes:
            # 适配 FastAPI 0.163+ _IncludedRouter：递归展开其有效路由上下文
            if not hasattr(route, "path") and hasattr(route, "effective_route_contexts"):
                nested_name = _compat_get_route_name(
                    scope,
                    list(route.effective_route_contexts()),
                    route_name,
                )
                if nested_name is not None:
                    return nested_name
                continue

            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                route_name = getattr(route, "path", None)
                merged_scope = {**scope, **child_scope}
                if isinstance(route, Mount) and route.routes:
                    child_route_name = _compat_get_route_name(
                        merged_scope,
                        list(route.routes),
                        route_name,
                    )
                    if child_route_name is None:
                        route_name = None
                    elif route_name is not None:
                        route_name += child_route_name
                    else:
                        route_name = child_route_name
                return route_name
            if match == Match.PARTIAL and route_name is None:
                route_name = getattr(route, "path", None)
        return None

    prom_routing._get_route_name = _compat_get_route_name  # type: ignore[assignment]
    _PROM_ROUTING_PATCHED = True


def configure_telemetry(app: FastAPI, settings: Settings) -> None:
    """按配置初始化 OpenTelemetry Tracing 并自动埋点 FastAPI。"""
    global _TRACER_INITIALIZED

    if not settings.otel_enabled:
        logger.info("OpenTelemetry 已关闭，跳过链路追踪初始化")
        return

    try:
        if not _TRACER_INITIALIZED:
            resource = Resource.create(
                {
                    SERVICE_NAME: settings.otel_service_name,
                    SERVICE_VERSION: settings.app_version,
                    "deployment.environment": settings.app_env,
                }
            )
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(
                endpoint=settings.otel_exporter_otlp_endpoint,
                insecure=settings.otel_exporter_otlp_endpoint.startswith("http://"),
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            _TRACER_INITIALIZED = True

        # 避免对同一 FastAPI 实例重复插桩
        if not getattr(app.state, "otel_instrumented", False):
            FastAPIInstrumentor.instrument_app(
                app,
                excluded_urls="api/v1/healthz,metrics",
            )
            app.state.otel_instrumented = True
    except Exception:
        logger.warning("OpenTelemetry 初始化失败，已安全降级继续运行", exc_info=True)


def configure_metrics(app: FastAPI) -> None:
    """挂载 Prometheus `/metrics` 指标采集器（排除高频探针路径噪音）。"""
    if getattr(app.state, "prometheus_instrumented", False):
        return

    _install_prometheus_routing_compat()
    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/api/v1/healthz"],
    )
    instrumentator.instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
    )
    app.state.prometheus_instrumented = True
