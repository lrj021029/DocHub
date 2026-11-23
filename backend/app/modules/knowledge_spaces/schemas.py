"""Knowledge Space 与私有空间成员管理 Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.modules.identity.schemas import EnterpriseEmail


class KnowledgeSpaceItem(BaseModel):
    """知识空间详情与列表项。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    visibility: Literal["PUBLIC", "PRIVATE"]
    answer_policy: Literal["STRICT_EVIDENCE", "GROUNDED_WITH_GENERAL_CONTEXT"]
    default_embedding_profile_id: uuid.UUID | None = None
    default_embedding_profile_code: str | None = None
    status: str
    config_json: dict[str, Any] = Field(default_factory=dict)
    config_version: int
    member_count: int = 0
    document_count: int = 0
    created_at: datetime


class KnowledgeSpaceCreateRequest(BaseModel):
    """创建知识空间请求体。"""

    name: str = Field(..., min_length=2, max_length=160, description="知识空间名称")
    slug: str = Field(
        ...,
        min_length=2,
        max_length=96,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        description="组织内唯一短标识",
    )
    description: str | None = Field(default=None, max_length=512)
    visibility: Literal["PUBLIC", "PRIVATE"] = Field(
        default="PRIVATE",
        description="PUBLIC 仅对本组织成员开放；PRIVATE 仅对显式成员开放",
    )
    answer_policy: Literal["STRICT_EVIDENCE", "GROUNDED_WITH_GENERAL_CONTEXT"] = Field(
        default="STRICT_EVIDENCE",
        description="回答模式（默认 STRICT_EVIDENCE 严格依据授权证据）",
    )
    default_embedding_profile_id: uuid.UUID | None = None
    config_json: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSpaceUpdateRequest(BaseModel):
    """更新知识空间配置请求体。"""

    name: str | None = Field(default=None, min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=512)
    visibility: Literal["PUBLIC", "PRIVATE"] | None = None
    answer_policy: Literal["STRICT_EVIDENCE", "GROUNDED_WITH_GENERAL_CONTEXT"] | None = None
    default_embedding_profile_id: uuid.UUID | None = None
    status: Literal["ACTIVE", "ARCHIVED"] | None = None
    config_json: dict[str, Any] | None = None


class SpaceMemberItem(BaseModel):
    """Private Space 显式成员项。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    space_id: uuid.UUID
    user_id: uuid.UUID
    email: EnterpriseEmail
    display_name: str
    role: str
    status: str
    valid_from: datetime
    valid_until: datetime | None = None


class SpaceMemberUpsertEntry(BaseModel):
    """单个私有空间成员新增或更新项。"""

    user_id: uuid.UUID
    role: Literal["MEMBER", "ADMIN"] = "MEMBER"
    status: Literal["ACTIVE", "REVOKED"] = "ACTIVE"
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _check_window(self) -> SpaceMemberUpsertEntry:
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until 必须晚于 valid_from")
        return self


class SpaceMembersPatchRequest(BaseModel):
    """批量维护私有空间成员请求体。"""

    upsert: list[SpaceMemberUpsertEntry] = Field(default_factory=list)
    remove_user_ids: list[uuid.UUID] = Field(default_factory=list)
