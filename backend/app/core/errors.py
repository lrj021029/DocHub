"""统一业务异常体系与 FastAPI 全局异常处理器。

安全约束：
1. 所有错误响应统一为 JSON 结构：`{code, message, detail, request_id}`。
2. 严禁向客户端暴露 Python 堆栈、SQL 原文、数据库表名或未授权资源的真实存在性。
3. 校验错误仅返回脱敏后的字段路径与原因，不包含敏感输入值。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import redact_sensitive_data, request_id_ctx

logger = logging.getLogger(__name__)


class ErrorResponse(BaseModel):
    """全局统一错误响应契约。"""

    code: str = Field(..., description="稳定机器可读业务错误码")
    message: str = Field(..., description="面向调用方的安全错误提示")
    detail: Any = Field(default=None, description="可选结构化错误详情（已脱敏，不含敏感信息）")
    request_id: str = Field(..., description="本次请求的追踪标识")


class AppError(Exception):
    """DocHub 业务异常基类。"""

    default_status_code: int = status.HTTP_400_BAD_REQUEST
    default_code: str = "APP_ERROR"
    default_message: str = "请求处理失败"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.code = code or self.default_code
        self.status_code = status_code or self.default_status_code
        self.detail = redact_sensitive_data(detail) if detail is not None else None
        self.headers = headers
        super().__init__(self.message)


class AuthError(AppError):
    """身份认证失败异常（401 Unauthorized）。"""

    default_status_code = status.HTTP_401_UNAUTHORIZED
    default_code = "AUTH_ERROR"
    default_message = "身份认证失败或凭证已失效"


class ForbiddenError(AppError):
    """权限不足异常（403 Forbidden），不透露未授权资源存在性。"""

    default_status_code = status.HTTP_403_FORBIDDEN
    default_code = "FORBIDDEN"
    default_message = "无权执行此操作或访问目标资源"


class NotFoundError(AppError):
    """资源不存在或不可见异常（404 Not Found）。

    说明：对跨租户或无权访问的资源可统一返回 NotFoundError，避免暴露存在性侧信道。
    """

    default_status_code = status.HTTP_404_NOT_FOUND
    default_code = "NOT_FOUND"
    default_message = "目标资源不存在或无权访问"


class ConflictError(AppError):
    """资源状态冲突或乐观锁版本冲突异常（409 Conflict）。"""

    default_status_code = status.HTTP_409_CONFLICT
    default_code = "CONFLICT"
    default_message = "资源状态冲突，请刷新后重试"


class PolicyBlockedError(AppError):
    """安全策略阻断异常（如 L2/L3 敏感文档外发拦截、缺失本地 OCR Fail-Closed）。"""

    default_status_code = status.HTTP_403_FORBIDDEN
    default_code = "POLICY_BLOCKED"
    default_message = "请求已被数据安全与外发策略阻断"


HTTP_422_STATUS = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)


class ValidationError(AppError):
    """业务参数校验失败异常（422 Unprocessable Content）。"""

    default_status_code = HTTP_422_STATUS
    default_code = "VALIDATION_ERROR"
    default_message = "请求参数校验失败"


def _resolve_request_id(request: Request) -> str:
    """优先从请求状态提取 request_id，回退至上下文变量。"""
    state_id = getattr(request.state, "request_id", None)
    if isinstance(state_id, str) and state_id:
        return state_id
    return request_id_ctx.get()


def _build_error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    detail: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造标准化 JSON 错误响应。"""
    payload = ErrorResponse(
        code=code,
        message=message,
        detail=redact_sensitive_data(detail) if detail is not None else None,
        request_id=_resolve_request_id(request),
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """向 FastAPI 应用注册全局统一异常处理器。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "业务异常: code=%s status=%s path=%s",
            exc.code,
            exc.status_code,
            request.url.path,
        )
        return _build_error_response(
            request=request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            detail=exc.detail,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # 仅保留字段位置、错误消息与类型，绝不原样回显可能含密码/密钥的 input 值
        sanitized_errors = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": str(err.get("msg", "参数无效")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        return _build_error_response(
            request=request,
            status_code=HTTP_422_STATUS,
            code="VALIDATION_ERROR",
            message="请求参数校验失败",
            detail={"errors": sanitized_errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        status_code_map = {
            status.HTTP_400_BAD_REQUEST: ("BAD_REQUEST", "无效的请求"),
            status.HTTP_401_UNAUTHORIZED: ("AUTH_ERROR", "身份认证失败或凭证已失效"),
            status.HTTP_403_FORBIDDEN: ("FORBIDDEN", "无权执行此操作或访问目标资源"),
            status.HTTP_404_NOT_FOUND: ("NOT_FOUND", "目标资源不存在或无权访问"),
            status.HTTP_405_METHOD_NOT_ALLOWED: ("METHOD_NOT_ALLOWED", "不支持的 HTTP 方法"),
            status.HTTP_409_CONFLICT: ("CONFLICT", "资源状态冲突"),
            status.HTTP_429_TOO_MANY_REQUESTS: ("RATE_LIMITED", "请求过于频繁，请稍后重试"),
        }
        code, safe_default_msg = status_code_map.get(
            exc.status_code,
            (f"HTTP_{exc.status_code}", "请求处理失败"),
        )
        message = (
            exc.detail
            if isinstance(exc.detail, str) and exc.status_code < 500
            else safe_default_msg
        )
        return _build_error_response(
            request=request,
            status_code=exc.status_code,
            code=code,
            message=message,
            detail=None,
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        # 完整异常信息仅写入脱敏后的服务端结构化日志，客户端绝不接收堆栈细节
        logger.exception(
            "未捕获服务端异常: path=%s method=%s",
            request.url.path,
            request.method,
        )
        return _build_error_response(
            request=request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_SERVER_ERROR",
            message="服务器内部错误，请稍后重试",
            detail=None,
        )
