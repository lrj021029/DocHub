"""幂等键（Idempotency-Key）作用域规范与校验工具。

遵循 TECHNICAL_SPEC §12.7：
- 关键写操作（上传完成、导入批次创建、ACL 更新、FAQ 发布、审核、任务创建等）支持 `Idempotency-Key`。
- 唯一作用域强制绑定 `principal + organization + operation`，杜绝跨租户或跨用户幂等冲突与信息泄漏。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.core.errors import ValidationError

IDEMPOTENCY_HEADER = "Idempotency-Key"

# 仅允许 ASCII 安全字符（字母、数字、下划线、连字符、点、冒号），长度 8~128
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{1,95}$")


@dataclass(frozen=True, slots=True)
class IdempotencyScope:
    """幂等键强类型作用域：`principal_id + organization_id + operation`。"""

    principal_id: str
    organization_id: str
    operation: str

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValueError("principal_id 不能为空")
        if not self.organization_id.strip():
            raise ValueError("organization_id 不能为空")
        if not _OPERATION_PATTERN.match(self.operation):
            raise ValueError(f"非法的幂等操作标识: {self.operation!r}")

    @classmethod
    def create(
        cls,
        *,
        principal_id: str | UUID,
        organization_id: str | UUID,
        operation: str,
    ) -> IdempotencyScope:
        """构造标准化幂等作用域对象。"""
        return cls(
            principal_id=str(principal_id).strip(),
            organization_id=str(organization_id).strip(),
            operation=operation.strip().lower(),
        )

    @property
    def scope_key(self) -> str:
        """返回可读的规范化作用域键。"""
        return f"org:{self.organization_id}|principal:{self.principal_id}|op:{self.operation}"


def validate_idempotency_key(raw_key: str | None, *, required: bool = True) -> str | None:
    """校验 HTTP 请求头中的 `Idempotency-Key` 格式。"""
    if raw_key is None or not raw_key.strip():
        if required:
            raise ValidationError(
                code="MISSING_IDEMPOTENCY_KEY",
                message=f"缺少必填请求头 {IDEMPOTENCY_HEADER}",
            )
        return None

    cleaned = raw_key.strip()
    if not _IDEMPOTENCY_KEY_PATTERN.match(cleaned):
        raise ValidationError(
            code="INVALID_IDEMPOTENCY_KEY",
            message=f"{IDEMPOTENCY_HEADER} 格式不合法（需为 8~128 位字母、数字或 _ . : -）",
        )
    return cleaned


def build_idempotency_fingerprint(scope: IdempotencyScope, idempotency_key: str) -> str:
    """结合作用域与客户端幂等键生成全局唯一 SHA-256 哈希指纹。

    用途：作为数据库唯一约束或 Redis 幂等锁的稳定键值，
    避免不同组织或用户复用相同客户端 Key 时发生碰撞。
    """
    validated_key = validate_idempotency_key(idempotency_key, required=True)
    raw = f"{scope.scope_key}|key:{validated_key}".encode()
    return hashlib.sha256(raw).hexdigest()


def compute_request_payload_hash(payload: Any) -> str:
    """计算请求体稳定摘要，用于检测同一幂等键被用于不同请求参数的冲突场景。"""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
