"""部门树与多部门兼职归属 Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.modules.identity.schemas import EnterpriseEmail

# 区分“未传入 parent_id”与“显式将 parent_id 置为 None（提升为根部门）”
UNSET_PARENT = object()


class DepartmentItem(BaseModel):
    """部门树节点展示结构（带层级深度与成员数）。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    status: str
    depth: int = Field(default=0, ge=0, description="在组织部门树中的层级深度（根部门为 0）")
    path_names: list[str] = Field(default_factory=list, description="从根部门到当前部门的名称路径")
    member_count: int = Field(default=0, ge=0, description="当前直属有效成员数")
    created_at: datetime


class DepartmentCreateRequest(BaseModel):
    """新建部门请求体。"""

    name: str = Field(..., min_length=1, max_length=120, description="部门名称")
    parent_id: uuid.UUID | None = Field(default=None, description="上级部门 ID（根部门为 null）")


class DepartmentUpdateRequest(BaseModel):
    """更新部门名称、状态或移动父节点（移动子树自动防环并重建 Closure Table）。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: uuid.UUID | None = Field(
        default=None,
        description="新上级部门 ID（配合 move_to_root=true 可提升为根部门）",
    )
    move_to_root: bool = Field(
        default=False,
        description="是否显式将该部门提升为根部门（parent_id 置为 null）",
    )
    status: Literal["ACTIVE", "ARCHIVED"] | None = None


class DepartmentMembershipItem(BaseModel):
    """部门成员归属记录。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    department_id: uuid.UUID
    department_name: str
    user_id: uuid.UUID
    email: EnterpriseEmail
    display_name: str
    membership_type: str
    is_primary: bool
    status: str
    valid_from: datetime
    valid_until: datetime | None = None


class DepartmentMembershipCreateRequest(BaseModel):
    """为用户分配部门或兼职部门请求体。"""

    user_id: uuid.UUID = Field(..., description="组织成员用户 ID")
    membership_type: Literal["PRIMARY", "SECONDARY", "TEMPORARY"] = Field(
        default="PRIMARY",
        description="归属类型（PRIMARY 主部门 / SECONDARY 兼职 / TEMPORARY 临时）",
    )
    is_primary: bool = Field(default=False, description="是否设为用户在该组织的主部门")
    valid_from: datetime | None = Field(default=None, description="生效时间（UTC，默认当前时间）")
    valid_until: datetime | None = Field(default=None, description="到期时间（UTC，空表示长期）")

    @model_validator(mode="after")
    def _validate_time_window(self) -> DepartmentMembershipCreateRequest:
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until 必须晚于 valid_from")
        return self
