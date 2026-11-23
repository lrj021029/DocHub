"""分页契约（Offset/Limit 与 Cursor）及排序字段白名单校验器。

遵循 TECHNICAL_SPEC §12.6：
1. 管理台普通筛选列表支持 Offset/Limit 分页。
2. 问答历史、审计事件、长列表优先使用 Cursor 分页。
3. 所有排序字段必须经过显式白名单校验，严禁将客户端输入的任意表达式直传 ORM。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import ValidationError


class SortOrder(StrEnum):
    """排序方向枚举。"""

    ASC = "asc"
    DESC = "desc"


class SortSpec(BaseModel):
    """经白名单校验后的单个排序项。"""

    field: str = Field(..., description="白名单允许的排序字段名")
    order: SortOrder = Field(default=SortOrder.DESC, description="排序方向")


class OffsetPageParams(BaseModel):
    """Offset/Limit 分页请求参数。"""

    offset: int = Field(default=0, ge=0, description="起始偏移量")
    limit: int = Field(default=20, ge=1, le=100, description="单页最大条数（上限 100）")


class OffsetPage[T](BaseModel):
    """Offset/Limit 分页响应结构。"""

    items: list[T] = Field(default_factory=list, description="当前页数据列表")
    total: int = Field(..., ge=0, description="符合条件的总记录数")
    offset: int = Field(..., ge=0, description="本次查询偏移量")
    limit: int = Field(..., ge=1, description="本次查询单页大小")
    has_more: bool = Field(..., description="是否存在下一页")

    @classmethod
    def create(
        cls,
        items: list[T],
        *,
        total: int,
        offset: int,
        limit: int,
    ) -> OffsetPage[T]:
        """构造标准 Offset 分页对象并自动计算 has_more。"""
        return cls(
            items=items,
            total=total,
            offset=offset,
            limit=limit,
            has_more=(offset + len(items)) < total,
        )


class CursorPageParams(BaseModel):
    """Cursor 分页请求参数。"""

    cursor: str | None = Field(default=None, max_length=512, description="不透明游标字符串")
    limit: int = Field(default=20, ge=1, le=100, description="单页最大条数（上限 100）")


class CursorPage[T](BaseModel):
    """Cursor 分页响应结构。"""

    items: list[T] = Field(default_factory=list, description="当前页数据列表")
    next_cursor: str | None = Field(default=None, description="下一页不透明游标")
    has_more: bool = Field(..., description="是否存在下一页")


def encode_cursor(payload: Mapping[str, Any]) -> str:
    """将游标定位字典编码为 URL-safe Base64 不透明字符串。"""
    raw = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    """解码并验证不透明游标字符串，非法格式统一抛出 ValidationError。"""
    if not cursor or len(cursor) > 512:
        raise ValidationError(code="INVALID_CURSOR", message="分页游标格式无效")

    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValidationError(code="INVALID_CURSOR", message="分页游标格式无效") from exc

    if not isinstance(parsed, dict):
        raise ValidationError(code="INVALID_CURSOR", message="分页游标内容无效")
    return parsed


class SortWhitelistValidator:
    """排序字段白名单校验器（防 ORM 任意字段/表达式注入）。

    用法示例：
        validator = SortWhitelistValidator(
            allowed_fields={"created_at", "updated_at", "title"},
            default_field="created_at",
        )
        spec = validator.parse("-created_at")
    """

    def __init__(
        self,
        allowed_fields: Collection[str],
        *,
        default_field: str | None = None,
        default_order: SortOrder = SortOrder.DESC,
    ) -> None:
        normalized = frozenset(field.strip() for field in allowed_fields if field.strip())
        if not normalized:
            raise ValueError("排序白名单 allowed_fields 不能为空")
        if default_field is not None and default_field not in normalized:
            raise ValueError(f"默认排序字段 {default_field!r} 必须包含在白名单内")

        self.allowed_fields = normalized
        self.default_field = default_field
        self.default_order = default_order

    def validate_field(self, field: str) -> str:
        """严格校验单个排序字段是否在白名单中，不允许点号、空格或表达式。"""
        cleaned = field.strip()
        if not cleaned or cleaned not in self.allowed_fields:
            raise ValidationError(
                code="INVALID_SORT_FIELD",
                message="不支持的排序字段",
                detail={"allowed_fields": sorted(self.allowed_fields)},
            )
        return cleaned

    def parse(self, sort_expr: str | None) -> SortSpec:
        """解析单个排序表达式（支持 `field`、`+field`、`-field`、`field:asc`、`field:desc`）。"""
        if not sort_expr or not sort_expr.strip():
            if self.default_field is None:
                raise ValidationError(code="INVALID_SORT_FIELD", message="缺少排序字段")
            return SortSpec(field=self.default_field, order=self.default_order)

        raw = sort_expr.strip()
        order = self.default_order
        field_part = raw

        if raw.startswith("-"):
            order = SortOrder.DESC
            field_part = raw[1:]
        elif raw.startswith("+"):
            order = SortOrder.ASC
            field_part = raw[1:]
        elif ":" in raw:
            parts = raw.split(":", 1)
            field_part = parts[0]
            direction = parts[1].strip().lower()
            if direction == "asc":
                order = SortOrder.ASC
            elif direction == "desc":
                order = SortOrder.DESC
            else:
                raise ValidationError(
                    code="INVALID_SORT_ORDER",
                    message="排序方向仅支持 asc 或 desc",
                )

        validated_field = self.validate_field(field_part)
        return SortSpec(field=validated_field, order=order)

    def resolve_column(
        self,
        sort_expr: str | None,
        column_mapping: Mapping[str, Any],
    ) -> tuple[Any, SortOrder]:
        """将白名单校验后的排序项安全映射为 SQLAlchemy 列对象。"""
        spec = self.parse(sort_expr)
        if spec.field not in column_mapping:
            raise ValidationError(
                code="INVALID_SORT_FIELD",
                message="排序字段未配置安全映射",
            )
        return column_mapping[spec.field], spec.order
