"""角色与功能权限 Pydantic Schema 定义（明确分离 SystemRole 与 OrganizationRole）。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.modules.identity.schemas import EnterpriseEmail


class PermissionItem(BaseModel):
    """功能权限字典项。"""

    id: uuid.UUID
    code: str
    scope: str
    description: str


class SystemRoleItem(BaseModel):
    """平台级系统角色展示项（与组织角色隔离展示）。"""

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    permission_codes: list[str] = Field(default_factory=list)


class PermissionCatalogResponse(BaseModel):
    """`GET /api/v1/permissions` 权限字典与平台系统角色概览。"""

    permissions: list[PermissionItem] = Field(default_factory=list)
    system_roles: list[SystemRoleItem] = Field(default_factory=list)


class RoleAssignmentItem(BaseModel):
    """组织角色成员授予记录。"""

    id: uuid.UUID
    user_id: uuid.UUID
    email: EnterpriseEmail
    display_name: str
    status: str
    valid_from: datetime
    valid_until: datetime | None = None


class OrganizationRoleItem(BaseModel):
    """组织角色详情项（含已绑定的组织级权限码与有效成员数）。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    status: str
    permission_codes: list[str] = Field(default_factory=list)
    member_count: int = Field(default=0, ge=0)
    assignments: list[RoleAssignmentItem] = Field(default_factory=list)
    created_at: datetime


class OrganizationRoleCreateRequest(BaseModel):
    """创建自定义组织角色请求体（仅允许绑定 ORGANIZATION 作用域权限）。"""

    code: str = Field(
        ...,
        min_length=2,
        max_length=64,
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="组织内角色编码（大写字母、数字、下划线）",
    )
    name: str = Field(..., min_length=1, max_length=120, description="角色展示名称")
    description: str | None = Field(default=None, max_length=255)
    permission_codes: list[str] = Field(
        default_factory=list,
        description="绑定的组织级功能权限编码列表",
    )


class RoleAssignRequest(BaseModel):
    """向组织成员授予组织角色请求体（支持生效/到期时间窗口）。"""

    user_id: uuid.UUID = Field(..., description="目标成员用户 ID")
    valid_from: datetime | None = Field(default=None, description="生效时间（UTC，默认当前时间）")
    valid_until: datetime | None = Field(default=None, description="到期时间（UTC，空表示长期）")

    @model_validator(mode="after")
    def _validate_range(self) -> RoleAssignRequest:
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until 必须晚于 valid_from")
        return self
