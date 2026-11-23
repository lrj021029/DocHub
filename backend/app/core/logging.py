"""结构化 JSON 日志与敏感字段自动脱敏过滤器。

安全红线（ADR-001 & TECHNICAL_SPEC §15.6）：
- 严禁在日志中输出 API Key、JWT、Cookie、密码、Refresh Token 或任意密钥字段。
- 所有结构化日志字段及日志消息文本均经过统一脱敏管道处理。
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

# 请求链路追踪 ID 上下文变量（由请求中间件注入）
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

REDACTED_VALUE = "[REDACTED]"

# 需要整值脱敏的敏感键名关键字（大小写不敏感，子串匹配）
SENSITIVE_KEY_TOKENS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "authorization",
    "cookie",
    "token",
    "refresh_token",
    "access_token",
    "csrf",
    "private_key",
    "credential",
    "jwt",
)

# 日志自由文本中常见密钥/令牌格式的正则脱敏规则
_SENSITIVE_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer / Basic 认证头
    (
        re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9\-._~+/]+=*"),
        r"\1 " + REDACTED_VALUE,
    ),
    # 常见 JSON 或键值对形式的敏感字段（如 api_key="xxx"、password: xxx、token=xxx）
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|password|passwd|secret(?:_key)?|authorization|"
            r"cookie|set-cookie|refresh[_-]?token|access[_-]?token|csrf[_-]?token|token)"
            r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}\]]+)([\"']?)"
        ),
        r"\1" + REDACTED_VALUE + r"\3",
    ),
    # 标准三段式 JWT（header.payload.signature）
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
        REDACTED_VALUE,
    ),
    # 常见云厂商/模型 API Key 前缀（如 sk-xxxx）
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        REDACTED_VALUE,
    ),
)

# Python 标准 LogRecord 保留属性集合，用于提取用户通过 extra={} 传入的扩展字段
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def is_sensitive_key(key: str) -> bool:
    """判断字典键名是否属于敏感字段。"""
    normalized = key.strip().lower().replace("-", "_")
    return any(token in normalized for token in SENSITIVE_KEY_TOKENS)


def redact_text(text: str) -> str:
    """对日志文本中的 Bearer Token、JWT 与敏感键值对进行正则脱敏。"""
    sanitized = text
    for pattern, replacement in _SENSITIVE_TEXT_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def redact_sensitive_data(value: Any, *, _depth: int = 0) -> Any:
    """递归脱敏字典、列表、元组及字符串中的敏感数据。"""
    if _depth > 10:
        return "[TRUNCATED]"

    if isinstance(value, SecretStr):
        return REDACTED_VALUE
    if isinstance(value, dict):
        sanitized_dict: dict[Any, Any] = {}
        for k, v in value.items():
            key_str = str(k)
            if is_sensitive_key(key_str):
                sanitized_dict[key_str] = REDACTED_VALUE
            else:
                sanitized_dict[key_str] = redact_sensitive_data(v, _depth=_depth + 1)
        return sanitized_dict
    if isinstance(value, list):
        return [redact_sensitive_data(item, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item, _depth=_depth + 1) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


class SensitiveDataRedactionFilter(logging.Filter):
    """标准日志过滤器：拦截并脱敏消息文本、格式化参数与扩展 extra 字段。"""

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. 先格式化原始消息再脱敏，覆盖 %s 参数中携带的密钥
        try:
            rendered_message = record.getMessage()
        except Exception:
            rendered_message = str(record.msg)

        record.msg = redact_text(rendered_message)
        record.args = ()

        # 2. 对 extra 附加字段逐一脱敏
        for attr_name in list(record.__dict__.keys()):
            if attr_name in _STANDARD_LOG_RECORD_ATTRS:
                continue
            attr_val = getattr(record, attr_name)
            if is_sensitive_key(attr_name):
                setattr(record, attr_name, REDACTED_VALUE)
            else:
                setattr(record, attr_name, redact_sensitive_data(attr_val))

        # 3. 注入当前请求 ID
        if not getattr(record, "request_id", None):
            record.request_id = request_id_ctx.get()

        return True


class JsonLogFormatter(logging.Formatter):
    """结构化 JSON 日志格式化器（便于 Loki / Grafana 采集与检索）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "request_id": getattr(record, "request_id", request_id_ctx.get()),
        }

        # 收集非标准扩展字段并再次保证脱敏
        extra_fields: dict[str, Any] = {}
        for attr_name, attr_val in record.__dict__.items():
            if attr_name in _STANDARD_LOG_RECORD_ATTRS or attr_name == "request_id":
                continue
            if is_sensitive_key(attr_name):
                extra_fields[attr_name] = REDACTED_VALUE
            else:
                extra_fields[attr_name] = redact_sensitive_data(attr_val)

        if extra_fields:
            payload["extra"] = extra_fields

        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(log_level: str = "INFO") -> None:
    """初始化全局结构化 JSON 日志与敏感数据脱敏过滤器。"""
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    handler.addFilter(SensitiveDataRedactionFilter())
    handler.setFormatter(JsonLogFormatter())

    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # 统一接管 uvicorn 相关日志器，避免非脱敏明文日志绕过
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        target_logger = logging.getLogger(logger_name)
        target_logger.handlers.clear()
        target_logger.propagate = True
