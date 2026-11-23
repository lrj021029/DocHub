"""核心认证与密码学安全原语。

包含：
1. Argon2id 密码哈希、校验与参数升级判定。
2. Access JWT 签发与严格校验（校验签名、过期时间、签发者与 token 类型）。
3. Refresh Token 高熵随机生成与 SHA-256 哈希存储（数据库绝不保存明文 Refresh Token）。
4. CSRF Token 生成与常量时间比对（HMAC 签名 + secrets.compare_digest 防时序攻击）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jwt import ExpiredSignatureError, InvalidTokenError

from app.core.config import Settings, get_settings
from app.core.errors import AuthError
from app.core.ids import new_uuid7_str

# Argon2id 参数遵循 OWASP 推荐基线，兼具抗 GPU 暴力破解能力与可控登录延时
_PASSWORD_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

# 允许使用的对称/非对称 JWT 算法白名单，杜绝 alg=none 攻击
_ALLOWED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})


def hash_password(plain_password: str) -> str:
    """使用 Argon2id 对明文密码进行不可逆哈希。"""
    if not plain_password:
        raise ValueError("密码不能为空")
    return _PASSWORD_HASHER.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码与 Argon2id 哈希是否匹配。"""
    if not plain_password or not password_hash:
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """检测已有密码哈希是否需要按最新 Argon2id 参数重新计算。"""
    return _PASSWORD_HASHER.check_needs_rehash(password_hash)


def create_access_token(
    subject: str | UUID,
    *,
    organization_id: str | UUID | None = None,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
    settings: Settings | None = None,
) -> str:
    """签发短期 Access JWT（前端仅允许保存于内存，严禁写入 localStorage）。"""
    cfg = settings or get_settings()
    if cfg.jwt_algorithm not in _ALLOWED_JWT_ALGORITHMS:
        raise ValueError(f"不支持的 JWT 算法配置: {cfg.jwt_algorithm}")

    now = datetime.now(UTC)
    expire_at = now + (expires_delta or timedelta(minutes=cfg.jwt_access_token_expire_minutes))

    payload: dict[str, Any] = {
        "sub": str(subject),
        "iss": cfg.jwt_issuer,
        "jti": new_uuid7_str(),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int(expire_at.timestamp()),
    }
    if organization_id is not None:
        payload["org_id"] = str(organization_id)
    if extra_claims:
        for reserved in ("sub", "iss", "jti", "type", "iat", "exp"):
            if reserved in extra_claims:
                raise ValueError(f"extra_claims 禁止覆盖保留 JWT 字段: {reserved}")
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        cfg.jwt_secret_key.get_secret_value(),
        algorithm=cfg.jwt_algorithm,
    )


def decode_access_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """校验并解析 Access JWT，任何篡改、过期或类型不符均抛出 AuthError。"""
    cfg = settings or get_settings()
    if cfg.jwt_algorithm not in _ALLOWED_JWT_ALGORITHMS:
        raise AuthError(message="服务端 JWT 算法配置异常")

    if not token or not token.strip():
        raise AuthError(message="缺少访问令牌")

    try:
        payload = jwt.decode(
            token,
            cfg.jwt_secret_key.get_secret_value(),
            algorithms=[cfg.jwt_algorithm],
            issuer=cfg.jwt_issuer,
            options={"require": ["sub", "exp", "iat", "iss", "jti", "type"]},
        )
    except ExpiredSignatureError as exc:
        raise AuthError(code="TOKEN_EXPIRED", message="访问令牌已过期") from exc
    except InvalidTokenError as exc:
        raise AuthError(code="INVALID_TOKEN", message="访问令牌无效") from exc

    if payload.get("type") != "access":
        raise AuthError(code="INVALID_TOKEN_TYPE", message="令牌类型不合法")

    return payload


def generate_refresh_token(num_bytes: int = 48) -> str:
    """生成高熵 URL-safe Refresh Token（用于写入 HttpOnly Cookie）。"""
    if num_bytes < 32:
        raise ValueError("Refresh Token 随机熵不得低于 32 字节")
    return secrets.token_urlsafe(num_bytes)


def hash_refresh_token(raw_token: str) -> str:
    """计算 Refresh Token 的 SHA-256 十六进制摘要，用于数据库持久化与轮换核验。"""
    if not raw_token:
        raise ValueError("Refresh Token 不能为空")
    return hashlib.sha256(raw_token.encode()).hexdigest()


def verify_refresh_token_hash(raw_token: str, expected_hash: str) -> bool:
    """以常量时间比对 Refresh Token 原文与其持久化 SHA-256 摘要。"""
    if not raw_token or not expected_hash:
        return False
    actual_hash = hash_refresh_token(raw_token)
    return hmac.compare_digest(actual_hash, expected_hash)


def generate_csrf_token(
    session_binding: str | None = None,
    *,
    settings: Settings | None = None,
) -> str:
    """生成带 HMAC-SHA256 完整性的 CSRF Token。

    格式为 `<nonce>.<signature>`；当提供 `session_binding` 时，签名同时绑定会话标识。
    """
    cfg = settings or get_settings()
    nonce = secrets.token_urlsafe(32)
    binding = session_binding or ""
    message = f"{nonce}:{binding}".encode()
    signature = hmac.new(
        cfg.csrf_secret_key.get_secret_value().encode(),
        message,
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{signature}"


def verify_csrf_token(
    presented_token: str,
    expected_token: str | None = None,
    *,
    session_binding: str | None = None,
    settings: Settings | None = None,
) -> bool:
    """常量时间校验 CSRF Token。

    校验逻辑：
    1. 校验 Token 自身格式与 HMAC-SHA256 签名（可选绑定 session_binding）。
    2. 如传入双提交对照值 `expected_token`（例如来自 Cookie），同时使用常量时间比对二者完全一致。
    """
    if not presented_token or "." not in presented_token:
        return False

    parts = presented_token.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    nonce, presented_sig = parts

    cfg = settings or get_settings()
    binding = session_binding or ""
    message = f"{nonce}:{binding}".encode()
    expected_sig = hmac.new(
        cfg.csrf_secret_key.get_secret_value().encode(),
        message,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(presented_sig, expected_sig):
        return False

    if expected_token is not None:
        return hmac.compare_digest(presented_token, expected_token)

    return True
