"""四维 Document ACL 与修订历史 Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ACLGrantInput(BaseModel):
    """单条四维 Allow OR 授权项输入契约。"""

    subject_type: Literal["GLOBAL", "DEPARTMENT", "ROLE", "USER"] = Field(
        ...,
        description="授权主体维度（GLOBAL / DEPARTMENT / ROLE / USER）",
    )
    subject_id: uuid.UUID | None = Field(
        default=None,
        description="主体 UUID（GLOBAL 必须为 null，其余维度必填）",
    )
    include_descendants: bool = Field(
        default=False,
        description="部门授权是否包含后代子部门（仅 DEPARTMENT 有效）",
    )
    inheritance_mode: Literal["DYNAMIC", "SNAPSHOT"] = Field(
        default="DYNAMIC",
        description="部门继承模式（DYNAMIC 实时闭包表 / SNAPSHOT 授权时刻部门快照）",
    )
    valid_from: datetime | None = Field(default=None, description="生效时间（UTC，默认当前时间）")
    valid_until: datetime | None = Field(default=None, description="到期时间（UTC，空表示长期）")

    @model_validator(mode="after")
    def _validate_grant_shape(self) -> ACLGrantInput:
        if self.subject_type == "GLOBAL":
            if self.subject_id is not None:
                raise ValueError("GLOBAL 授权的 subject_id 必须为 null")
            if self.include_descendants:
                raise ValueError("GLOBAL 授权不允许设置 include_descendants")
        elif self.subject_id is None:
            raise ValueError(f"{self.subject_type} 授权必须提供有效的 subject_id")

        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until 必须晚于 valid_from")
        return self


class ACLGrantItem(BaseModel):
    """已落库的四维 ACL 授权项视图。"""

    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID | None = None
    subject_label: str | None = None
    include_descendants: bool
    inheritance_mode: str
    snapshot_department_ids: list[uuid.UUID] = Field(default_factory=list)
    valid_from: datetime
    valid_until: datetime | None = None


class DocumentACLResponse(BaseModel):
    """`GET /api/v1/documents/{document_id}/acl` 响应结构。"""

    acl_id: uuid.UUID
    organization_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    document_status: str
    revision: int
    status: str
    grants: list[ACLGrantItem] = Field(default_factory=list)


class ACLRevisionCreateRequest(BaseModel):
    """`POST /api/v1/documents/{document_id}/acl/revisions` 乐观锁更新请求体。"""

    expected_revision: int = Field(..., ge=0, description="乐观锁期望修订号（防止并发覆盖）")
    reason: str = Field(..., min_length=2, max_length=500, description="必填 ACL 变更理由")
    grants: list[ACLGrantInput] = Field(
        default_factory=list,
        description="全量目标四维 Allow OR 授权项（空列表表示全部拒绝）",
    )


class ACLRevisionItem(BaseModel):
    """不可变 ACL 修订历史记录条目。"""

    id: uuid.UUID
    acl_id: uuid.UUID
    revision: int
    reason: str
    changed_by: uuid.UUID | None = None
    changed_by_email: str | None = None
    snapshot_json: list[dict[str, Any]] = Field(default_factory=list)
    diff_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
