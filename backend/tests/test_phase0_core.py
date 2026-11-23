"""阶段 0 核心基础设施与安全内核单元测试。

覆盖范围：
1. RFC 9562 UUID v7 版本规范、时间戳提取与高频严格单调性。
2. Argon2id 密码哈希、验证与抗篡改。
3. Access JWT 签发、过期拒绝、签名/载荷篡改拒绝、非法类型拒绝，及 Refresh Token 哈希校验。
4. CSRF Token 生成与常量时间校验（含双提交与会话绑定）。
5. 结构化 JSON 日志与敏感字段/密钥自动脱敏。
6. 分页契约与排序字段白名单防 ORM 表达式注入。
7. 幂等键作用域隔离（principal + organization + operation）。
8. 统一异常响应格式（{code, message, detail, request_id}）与堆栈防泄漏。
9. 配置双角色数据库分离、RLS 事务上下文守卫以及 `/api/v1/healthz`、`/api/v1/readyz` 接口测试。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, SecretStr

from app.api.v1.health import DependencyHealth
from app.core.config import Settings
from app.core.errors import (
    AuthError,
    PolicyBlockedError,
    ValidationError,
)
from app.core.idempotency import (
    IdempotencyScope,
    build_idempotency_fingerprint,
    validate_idempotency_key,
)
from app.core.ids import extract_uuid7_timestamp_ms, new_uuid7
from app.core.logging import (
    REDACTED_VALUE,
    JsonLogFormatter,
    SensitiveDataRedactionFilter,
    redact_sensitive_data,
)
from app.core.pagination import (
    OffsetPage,
    SortOrder,
    SortWhitelistValidator,
    decode_cursor,
    encode_cursor,
)
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_csrf_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_csrf_token,
    verify_password,
    verify_refresh_token_hash,
)
from app.db.session import RLS_ORG_SETTING_KEY, org_rls_session
from app.main import create_app


def _make_test_settings() -> Settings:
    """构造隔离的测试配置，避免污染环境变量与外部 OTLP 连接。"""
    return Settings(
        app_env="test",
        app_debug=False,
        otel_enabled=False,
        jwt_secret_key=SecretStr("test-jwt-secret-key-with-more-than-32-chars-123456"),
        csrf_secret_key=SecretStr("test-csrf-secret-key-with-more-than-32-chars-654321"),
        cors_origins="http://localhost:5173,http://127.0.0.1:5173",
    )


def test_uuid7_format_and_strict_monotonicity() -> None:
    """验证 UUID v7 符合 RFC 9562 版本/变体规范且在同毫秒高频生成下严格单调递增。"""
    before_ms = time.time_ns() // 1_000_000
    generated = [new_uuid7() for _ in range(2000)]
    after_ms = time.time_ns() // 1_000_000

    for item in generated:
        assert item.version == 7
        assert item.variant == uuid.RFC_4122

    # 严格递增（整数序与字符串字典序一致）
    for prev, curr in zip(generated, generated[1:], strict=False):
        assert prev.int < curr.int
        assert str(prev) < str(curr)

    first_ts = extract_uuid7_timestamp_ms(generated[0])
    assert before_ms <= first_ts <= after_ms + 5


def test_argon2id_password_hashing_and_verification() -> None:
    """验证 Argon2id 密码哈希格式、随机盐与正确/错误口令校验。"""
    plain = "DocHub#Enterprise2026!"
    hashed_1 = hash_password(plain)
    hashed_2 = hash_password(plain)

    assert hashed_1.startswith("$argon2id$")
    assert hashed_1 != hashed_2  # 每次随机盐不同
    assert verify_password(plain, hashed_1) is True
    assert verify_password("WrongPassword!", hashed_1) is False
    assert verify_password("", hashed_1) is False
    assert verify_password(plain, "not-a-valid-argon2-hash") is False


def test_jwt_sign_verify_expiration_and_tampering_rejection() -> None:
    """验证 Access JWT 签发、解析、过期拒绝、签名/载荷篡改拒绝及非法令牌类型拒绝。"""
    settings = _make_test_settings()
    user_id = new_uuid7()
    org_id = new_uuid7()

    token = create_access_token(user_id, organization_id=org_id, settings=settings)
    claims = decode_access_token(token, settings=settings)
    assert claims["sub"] == str(user_id)
    assert claims["org_id"] == str(org_id)
    assert claims["type"] == "access"
    assert claims["iss"] == settings.jwt_issuer

    # 1. 过期令牌必须被拒绝
    expired_token = create_access_token(
        user_id,
        expires_delta=timedelta(seconds=-5),
        settings=settings,
    )
    with pytest.raises(AuthError) as expired_exc:
        decode_access_token(expired_token, settings=settings)
    assert expired_exc.value.code == "TOKEN_EXPIRED"

    # 2. 签名被篡改必须被拒绝
    header_part, payload_part, signature_part = token.split(".")
    tampered_sig = ("A" if signature_part[0] != "A" else "B") + signature_part[1:]
    tampered_token = f"{header_part}.{payload_part}.{tampered_sig}"
    with pytest.raises(AuthError) as tampered_exc:
        decode_access_token(tampered_token, settings=settings)
    assert tampered_exc.value.code == "INVALID_TOKEN"

    # 3. 使用错误密钥签发的令牌必须被拒绝
    other_settings = Settings(
        app_env="test",
        otel_enabled=False,
        jwt_secret_key=SecretStr("another-completely-different-secret-key-000000000"),
    )
    with pytest.raises(AuthError):
        decode_access_token(token, settings=other_settings)

    # 4. 非 access 类型的 JWT 必须被拒绝
    now = datetime.now(UTC)
    wrong_type_token = jwt.encode(
        {
            "sub": str(user_id),
            "iss": settings.jwt_issuer,
            "jti": str(new_uuid7()),
            "type": "refresh",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AuthError) as wrong_type_exc:
        decode_access_token(wrong_type_token, settings=settings)
    assert wrong_type_exc.value.code == "INVALID_TOKEN_TYPE"


def test_refresh_token_and_csrf_token_security() -> None:
    """验证 Refresh Token 哈希比对与 CSRF Token 生成/常量时间校验。"""
    settings = _make_test_settings()

    # Refresh Token 哈希校验
    raw_refresh = generate_refresh_token()
    assert len(raw_refresh) >= 48
    hashed_refresh = hash_refresh_token(raw_refresh)
    assert hashed_refresh != raw_refresh
    assert len(hashed_refresh) == 64
    assert verify_refresh_token_hash(raw_refresh, hashed_refresh) is True
    assert verify_refresh_token_hash(raw_refresh + "x", hashed_refresh) is False

    # CSRF Token 签名、双提交与会话绑定校验
    session_id = str(new_uuid7())
    csrf_token = generate_csrf_token(session_binding=session_id, settings=settings)
    assert verify_csrf_token(
        csrf_token,
        expected_token=csrf_token,
        session_binding=session_id,
        settings=settings,
    )
    # 会话不匹配拒绝
    assert not verify_csrf_token(
        csrf_token,
        expected_token=csrf_token,
        session_binding="different-session-id",
        settings=settings,
    )
    # 篡改签名拒绝
    tampered = csrf_token[:-1] + ("0" if csrf_token[-1] != "0" else "1")
    assert not verify_csrf_token(tampered, session_binding=session_id, settings=settings)
    # 恶意/空格式拒绝
    assert not verify_csrf_token("", settings=settings)
    assert not verify_csrf_token("no-dot-separator", settings=settings)


def test_structured_logging_sensitive_redaction() -> None:
    """验证结构化 JSON 日志自动脱敏所有敏感键名与内联密钥/JWT/Bearer 文本。"""
    raw_payload = {
        "user": "alice@example.com",
        "api_key": "sk-supersecretapikey1234567890",
        "password": "MyPlainPassword!",
        "nested": {
            "authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature123456",
            "refresh_token": "refresh-secret-value-999",
            "cookie": "refresh_token=abc123; csrf=xyz",
            "client_secret": SecretStr("top-secret-client"),
        },
    }
    redacted = redact_sensitive_data(raw_payload)
    assert redacted["user"] == "alice@example.com"
    assert redacted["api_key"] == REDACTED_VALUE
    assert redacted["password"] == REDACTED_VALUE
    assert redacted["nested"]["authorization"] == REDACTED_VALUE
    assert redacted["nested"]["refresh_token"] == REDACTED_VALUE
    assert redacted["nested"]["cookie"] == REDACTED_VALUE
    assert redacted["nested"]["client_secret"] == REDACTED_VALUE

    # 验证日志 Filter + JsonLogFormatter 实际输出不含任何明文密钥
    log_msg = (
        "调用失败 Authorization: Bearer secret_token_123 "
        "api_key=sk-abcdef123456789012 password=topsecret"
    )
    record = logging.LogRecord(
        name="dochub.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=log_msg,
        args=(),
        exc_info=None,
    )
    record.refresh_token = "raw-refresh-token-value"
    record.safe_field = "visible-value"

    redaction_filter = SensitiveDataRedactionFilter()
    assert redaction_filter.filter(record) is True

    formatted = JsonLogFormatter().format(record)
    parsed = json.loads(formatted)
    assert "secret_token_123" not in formatted
    assert "sk-abcdef123456789012" not in formatted
    assert "topsecret" not in formatted
    assert "raw-refresh-token-value" not in formatted
    assert parsed["extra"]["refresh_token"] == REDACTED_VALUE
    assert parsed["extra"]["safe_field"] == "visible-value"


def test_pagination_and_sort_whitelist_prevents_injection() -> None:
    """验证分页工具与排序白名单严格拦截非法 ORM 表达式与注入企图。"""
    # OffsetPage 计算
    page = OffsetPage.create(["a", "b"], total=5, offset=0, limit=2)
    assert page.has_more is True
    last_page = OffsetPage.create(["e"], total=5, offset=4, limit=2)
    assert last_page.has_more is False

    # Cursor 编解码与非法游标拒绝
    encoded = encode_cursor({"id": "01900000-0000-7000-8000-000000000001"})
    assert decode_cursor(encoded) == {"id": "01900000-0000-7000-8000-000000000001"}
    with pytest.raises(ValidationError):
        decode_cursor("!!!not-valid-base64!!!")

    # 排序字段白名单防注入
    validator = SortWhitelistValidator(
        allowed_fields={"created_at", "updated_at", "title"},
        default_field="created_at",
        default_order=SortOrder.DESC,
    )

    assert validator.parse(None).field == "created_at"
    assert validator.parse("-updated_at").order == SortOrder.DESC
    assert validator.parse("+title").order == SortOrder.ASC
    assert validator.parse("title:asc").order == SortOrder.ASC

    injection_payloads = [
        "password_hash",
        "organization.slug",
        "created_at; DROP TABLE users;--",
        "__class__.__mro__",
        "title:sideways",
        " ",
    ]
    for bad_expr in injection_payloads:
        if bad_expr.strip() == "":
            # 空白回退默认值，确认仍落在白名单内
            assert validator.parse(bad_expr).field == "created_at"
            continue
        with pytest.raises(ValidationError):
            validator.parse(bad_expr)


def test_idempotency_scope_isolation() -> None:
    """验证幂等键校验及其在 principal + organization + operation 维度的强隔离性。"""
    key = validate_idempotency_key("req-upload-01935a7f")
    assert key == "req-upload-01935a7f"

    with pytest.raises(ValidationError):
        validate_idempotency_key(None, required=True)
    with pytest.raises(ValidationError):
        validate_idempotency_key("short")
    with pytest.raises(ValidationError):
        validate_idempotency_key("bad key with spaces; drop table")

    org_a, org_b = new_uuid7(), new_uuid7()
    user_1, user_2 = new_uuid7(), new_uuid7()

    fp_a1 = build_idempotency_fingerprint(
        IdempotencyScope.create(
            principal_id=user_1,
            organization_id=org_a,
            operation="doc.publish",
        ),
        key,
    )
    fp_b1 = build_idempotency_fingerprint(
        IdempotencyScope.create(
            principal_id=user_1,
            organization_id=org_b,
            operation="doc.publish",
        ),
        key,
    )
    fp_a2 = build_idempotency_fingerprint(
        IdempotencyScope.create(
            principal_id=user_2,
            organization_id=org_a,
            operation="doc.publish",
        ),
        key,
    )
    fp_op = build_idempotency_fingerprint(
        IdempotencyScope.create(
            principal_id=user_1,
            organization_id=org_a,
            operation="acl.update",
        ),
        key,
    )

    assert len({fp_a1, fp_b1, fp_a2, fp_op}) == 4


def test_config_dual_db_roles_and_secret_masking() -> None:
    """验证配置中心严格区分迁移与运行期角色，且所有密钥在 repr 中不暴露明文。"""
    settings = _make_test_settings()
    migrator_url = settings.migrator_database_url.get_secret_value()
    runtime_url = settings.runtime_database_url.get_secret_value()

    assert "app_migrator" in migrator_url
    assert "app_runtime" in runtime_url
    assert migrator_url != runtime_url
    assert settings.llm_base_url == "https://100milionseek.top/v1"
    assert settings.embedding_primary_provider == "qwen"
    assert settings.reranker_primary_provider == "qwen"

    rendered_repr = repr(settings)
    assert "test-jwt-secret-key" not in rendered_repr
    assert "test-csrf-secret-key" not in rendered_repr
    assert "dochub_runtime_dev" not in rendered_repr


@pytest.mark.asyncio
async def test_org_rls_session_sets_local_config_and_blocks_cross_tenant_switch() -> None:
    """验证 org_rls_session 强制执行事务级 set_config 且禁止同一事务切换不同租户。"""

    class FakeTransaction:
        def __init__(self, session: FakeSession) -> None:
            self._session = session

        async def __aenter__(self) -> FakeTransaction:
            self._session._in_tx = True
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            self._session._in_tx = False

    class FakeSession:
        def __init__(self) -> None:
            self._in_tx = False
            self.info: dict[str, Any] = {}
            self.executed: list[tuple[str, dict[str, Any]]] = []

        def in_transaction(self) -> bool:
            return self._in_tx

        def begin(self) -> FakeTransaction:
            return FakeTransaction(self)

        async def execute(self, statement: Any, params: dict[str, Any]) -> None:
            self.executed.append((str(statement), params))

    org_a = new_uuid7()
    org_b = new_uuid7()
    fake_session = FakeSession()

    async with org_rls_session(fake_session, org_a):  # type: ignore[arg-type]
        assert len(fake_session.executed) == 1
        sql_text, params = fake_session.executed[0]
        assert "set_config" in sql_text
        assert "true" in sql_text.lower()
        assert params == {"setting_key": RLS_ORG_SETTING_KEY, "value": str(org_a)}

        # 同一事务中尝试切换至其他组织必须被安全阻断
        with pytest.raises(RuntimeError, match="禁止在同一数据库事务内切换"):
            async with org_rls_session(fake_session, org_b):  # type: ignore[arg-type]
                pass

    # 非法 UUID 必须被拒绝
    with pytest.raises(ValueError, match="非法的 organization_id"):
        async with org_rls_session(fake_session, "not-a-uuid"):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_healthz_error_handlers_and_readyz_contract() -> None:
    """验证 /api/v1/healthz、统一错误响应格式（防堆栈泄漏）及 /api/v1/readyz 真实降级语义。"""
    settings = _make_test_settings()
    app = create_app(settings=settings)

    # 挂载仅用于单测的临时探测路由，验证全局异常处理器行为
    probe_router = APIRouter(prefix="/api/v1/_test")

    class SecretPayload(BaseModel):
        password: str
        count: int

    @probe_router.get("/policy-blocked")
    async def trigger_policy_blocked() -> None:
        raise PolicyBlockedError(
            message="L3 机密文档禁止外发外部解析服务",
            detail={"sensitivity": "L3", "api_key": "sk-should-be-redacted-123456"},
        )

    @probe_router.get("/unhandled-crash")
    async def trigger_unhandled_crash() -> None:
        raise RuntimeError("内部敏感堆栈信息: password=super_secret_db_pwd")

    @probe_router.post("/validate")
    async def trigger_validation(body: SecretPayload) -> dict[str, int]:
        return {"count": body.count}

    app.include_router(probe_router)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. /api/v1/healthz 存活探针与 Request ID 透传
        resp_health = await client.get(
            "/api/v1/healthz",
            headers={"X-Request-Id": "req-health-check-001"},
        )
        assert resp_health.status_code == 200
        assert resp_health.headers["X-Request-Id"] == "req-health-check-001"
        health_body = resp_health.json()
        assert health_body["status"] == "ok"
        assert health_body["environment"] == "test"

        # 2. 业务异常统一响应格式及敏感字段脱敏
        resp_blocked = await client.get("/api/v1/_test/policy-blocked")
        assert resp_blocked.status_code == 403
        blocked_body = resp_blocked.json()
        assert set(blocked_body.keys()) == {"code", "message", "detail", "request_id"}
        assert blocked_body["code"] == "POLICY_BLOCKED"
        assert blocked_body["detail"]["api_key"] == REDACTED_VALUE
        assert blocked_body["request_id"] == resp_blocked.headers["X-Request-Id"]

        # 3. 422 参数校验异常不回显敏感输入值
        resp_invalid = await client.post(
            "/api/v1/_test/validate",
            json={"password": "TopSecretPassword999", "count": "not-an-int"},
        )
        assert resp_invalid.status_code == 422
        assert "TopSecretPassword999" not in resp_invalid.text
        invalid_body = resp_invalid.json()
        assert invalid_body["code"] == "VALIDATION_ERROR"
        assert invalid_body["request_id"]

        # 4. 未捕获异常严禁泄漏堆栈或敏感错误信息
        resp_crash = await client.get("/api/v1/_test/unhandled-crash")
        assert resp_crash.status_code == 500
        assert "super_secret_db_pwd" not in resp_crash.text
        assert "Traceback" not in resp_crash.text
        crash_body = resp_crash.json()
        assert crash_body["code"] == "INTERNAL_SERVER_ERROR"
        assert crash_body["detail"] is None

        # 5. /api/v1/readyz 在任一核心依赖不可用时必须返回 503（绝不伪造 200）
        up_health = DependencyHealth(status="up", latency_ms=1.5, detail="ok")
        down_health = DependencyHealth(status="down", latency_ms=2.5, detail="连接被拒绝")
        with (
            patch("app.api.v1.health.check_postgres", new=AsyncMock(return_value=up_health)),
            patch("app.api.v1.health.check_redis", new=AsyncMock(return_value=down_health)),
            patch("app.api.v1.health.check_minio", new=AsyncMock(return_value=up_health)),
            patch("app.api.v1.health.check_qdrant", new=AsyncMock(return_value=up_health)),
        ):
            resp_degraded = await client.get("/api/v1/readyz")
            assert resp_degraded.status_code == 503
            degraded_body = resp_degraded.json()
            assert degraded_body["status"] == "degraded"
            assert degraded_body["checks"]["redis"]["status"] == "down"
            assert degraded_body["checks"]["postgresql"]["status"] == "up"

        # 6. /metrics Prometheus 端点可用
        resp_metrics = await client.get("/metrics")
        assert resp_metrics.status_code == 200
        assert "http_request" in resp_metrics.text or "process_" in resp_metrics.text
