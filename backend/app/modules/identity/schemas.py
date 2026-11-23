"""身份认证与当前上下文 Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# 企业内网兼容邮箱类型：同时支持公网域名与内网保留域名（如 *.local / *.corp）
EnterpriseEmail = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=255,
        pattern=r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$",
    ),
]


class UserSummary(BaseModel):
    """对外安全展示的用户基础信息（绝不含密码哈希）。"""

    id: uuid.UUID
    email: EnterpriseEmail
    display_name: str
    status: str


class OrganizationBrief(BaseModel):
    """用户所属组织的简要信息。"""

    id: uuid.UUID
    name: str
    slug: str
    status: str
    member_status: str = "ACTIVE"


class RoleBrief(BaseModel):
    """角色简要信息（区分平台 SystemRole 与组织 OrganizationRole）。"""

    id: uuid.UUID
    code: str
    name: str
    scope: str = Field(..., description="SYSTEM 或 ORGANIZATION")


class DepartmentMembershipBrief(BaseModel):
    """当前用户在活跃组织内的有效部门归属简要信息。"""

    membership_id: uuid.UUID
    department_id: uuid.UUID
    department_name: str
    membership_type: str
    is_primary: bool
    valid_from: datetime
    valid_until: datetime | None = None


class LoginRequest(BaseModel):
    """邮箱密码登录请求体。"""

    email: EnterpriseEmail = Field(..., description="用户登录邮箱")
    password: str = Field(..., min_length=1, max_length=256, description="用户明文密码")
    organization_id: uuid.UUID | None = Field(
        default=None,
        description="可选指定登录后的初始活跃组织 ID",
    )


class AuthTokenResponse(BaseModel):
    """登录与 Refresh 轮换统一响应体（Access Token 仅交前端内存保存）。"""

    access_token: str = Field(..., description="短期 Access JWT（严禁前端写入 localStorage）")
    token_type: str = Field(default="bearer", description="令牌类型")
    expires_in: int = Field(..., description="Access JWT 剩余有效秒数")
    csrf_token: str = Field(..., description="与 dochub_csrf Cookie 配套的 CSRF 令牌")
    user: UserSummary
    organizations: list[OrganizationBrief] = Field(default_factory=list)
    active_organization_id: uuid.UUID | None = None


class AuthMeResponse(BaseModel):
    """`GET /api/v1/auth/me` 当前身份与有效权限全景响应。"""

    user: UserSummary
    active_organization_id: uuid.UUID | None = None
    active_organization: OrganizationBrief | None = None
    organizations: list[OrganizationBrief] = Field(default_factory=list)
    departments: list[DepartmentMembershipBrief] = Field(default_factory=list)
    system_roles: list[RoleBrief] = Field(default_factory=list)
    organization_roles: list[RoleBrief] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class LogoutResponse(BaseModel):
    """登出响应。"""

    status: str = "ok"
