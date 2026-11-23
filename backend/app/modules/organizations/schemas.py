"""企业组织与成员管理 Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.identity.schemas import (
    DepartmentMembershipBrief,
    EnterpriseEmail,
    OrganizationBrief,
    RoleBrief,
)


class OrganizationCreateRequest(BaseModel):
    """创建企业组织请求体（需平台 system:org:manage 权限）。"""

    name: str = Field(..., min_length=2, max_length=160, description="企业组织名称")
    slug: str = Field(
        ...,
        min_length=2,
        max_length=80,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        description="组织唯一短标识（仅允许小写字母、数字和连字符）",
    )


class OrganizationResponse(BaseModel):
    """企业组织详情响应。"""

    id: uuid.UUID
    name: str
    slug: str
    status: str
    created_at: datetime


class OrganizationSwitchResponse(BaseModel):
    """切换当前活跃组织响应（含重新绑定 org_id 的新 Access JWT）。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    active_organization_id: uuid.UUID
    active_organization: OrganizationBrief
    departments: list[DepartmentMembershipBrief] = Field(default_factory=list)
    organization_roles: list[RoleBrief] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class OrganizationMemberItem(BaseModel):
    """组织成员列表项。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    email: EnterpriseEmail
    display_name: str
    user_status: str
    status: str
    joined_at: datetime
    removed_at: datetime | None = None


class OrganizationMemberCreateRequest(BaseModel):
    """向组织添加成员请求体（若邮箱尚无账号，可提供 display_name + password 同步创建）。"""

    email: EnterpriseEmail = Field(..., description="成员邮箱")
    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="新用户显示名称（当邮箱尚不存在时必填）",
    )
    password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="新用户初始密码（当邮箱尚不存在时必填，后端自动 Argon2id 哈希）",
    )


class OrganizationMemberUpdateRequest(BaseModel):
    """更新组织成员状态请求体。"""

    status: Literal["ACTIVE", "SUSPENDED", "REMOVED"] = Field(
        ...,
        description="目标成员状态",
    )
