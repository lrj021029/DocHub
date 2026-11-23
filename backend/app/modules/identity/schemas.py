"""身份认证、账号自助注册与平台用户管理 Pydantic Schema 定义。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

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

_LETTER_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"[0-9]")


def _validate_strong_password(value: str) -> str:
    """校验密码强度：至少 8 位且同时包含字母与数字。"""
    if len(value) < 8 or len(value) > 128:
        raise ValueError("密码长度必须在 8 到 128 位之间")
    if not _LETTER_RE.search(value) or not _DIGIT_RE.search(value):
        raise ValueError("密码必须同时包含至少一个英文字母和一位数字")
    return value


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


class RegisterRequest(BaseModel):
    """账号自助注册请求体（安全约束：仅授予目标组织最小基础角色 EMPLOYEE）。"""

    email: EnterpriseEmail = Field(..., description="注册登录邮箱（全平台唯一）")
    display_name: str = Field(..., min_length=2, max_length=120, description="用户显示名称")
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="登录密码（至少 8 位含字母与数字）",
    )
    organization_slug: str | None = Field(
        default="acme-corp",
        max_length=64,
        description="默认加入的企业组织短标识（如 acme-corp）",
    )

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, value: str) -> str:
        """去除姓名首尾空白并校验非空。"""
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("用户显示名称去除空白后不少于 2 个字符")
        return cleaned

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        """校验自助注册密码强度。"""
        return _validate_strong_password(value)


class ChangePasswordRequest(BaseModel):
    """当前登录用户自助修改密码请求体。"""

    current_password: str = Field(..., min_length=1, max_length=256, description="当前登录密码")
    new_password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="新密码（至少 8 位含字母与数字）",
    )

    @field_validator("new_password")
    @classmethod
    def _check_new_password_strength(cls, value: str) -> str:
        """校验新密码强度。"""
        return _validate_strong_password(value)


class ChangePasswordResponse(BaseModel):
    """修改密码响应体。"""

    status: str = "ok"
    revoked_other_sessions: int = Field(
        default=0,
        description="本次改密同步吊销的其他设备活跃会话数量",
    )


class AdminUserOrganizationBrief(BaseModel):
    """平台用户所属组织的简要绑定信息。"""

    id: uuid.UUID
    name: str
    slug: str
    member_status: str = "ACTIVE"


class AdminUserItem(BaseModel):
    """平台用户管理台账条目（绝不含密码哈希）。"""

    id: uuid.UUID
    email: EnterpriseEmail
    display_name: str
    status: str
    system_roles: list[str] = Field(default_factory=list)
    organizations: list[AdminUserOrganizationBrief] = Field(default_factory=list)
    created_at: datetime


class AdminUserListResponse(BaseModel):
    """平台用户分页列表响应。"""

    items: list[AdminUserItem] = Field(default_factory=list)
    total: int
    offset: int
    limit: int


class AdminUserCreateRequest(BaseModel):
    """平台管理员创建用户请求体（需 system:user:manage 权限）。"""

    email: EnterpriseEmail = Field(..., description="登录邮箱（全平台唯一）")
    display_name: str = Field(..., min_length=2, max_length=120, description="用户显示名称")
    password: str = Field(..., min_length=8, max_length=128, description="初始登录密码")
    grant_system_admin: bool = Field(
        default=False,
        description="是否授予平台 SYSTEM_ADMIN 角色（注意：仍不隐式授予文档读权）",
    )
    organization_id: uuid.UUID | None = Field(
        default=None,
        description="可选：创建时同步加入的目标组织 ID",
    )
    organization_role_code: str | None = Field(
        default=None,
        max_length=64,
        description="可选：在目标组织内授予的 OrganizationRole 代码（默认 EMPLOYEE）",
    )
    department_id: uuid.UUID | None = Field(
        default=None,
        description="可选：在目标组织内归属的主部门 ID（留空则默认归属组织根部门）",
    )

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("用户显示名称去除空白后不少于 2 个字符")
        return cleaned

    @field_validator("password")
    @classmethod
    def _check_password_strength(cls, value: str) -> str:
        return _validate_strong_password(value)


class AdminUserUpdateRequest(BaseModel):
    """平台管理员更新用户资料、状态、密码或平台角色请求体。"""

    display_name: str | None = Field(default=None, min_length=2, max_length=120)
    status: Literal["ACTIVE", "DISABLED", "LOCKED"] | None = Field(
        default=None,
        description="目标账号状态；停用或锁定将立即吊销该用户所有活跃会话",
    )
    new_password: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        description="可选：管理员重置后的新密码（同步吊销该用户全部活跃会话）",
    )
    grant_system_admin: bool | None = Field(
        default=None,
        description="可选：true 授予 SYSTEM_ADMIN，false 撤销 SYSTEM_ADMIN",
    )

    @field_validator("display_name")
    @classmethod
    def _strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("用户显示名称去除空白后不少于 2 个字符")
        return cleaned

    @field_validator("new_password")
    @classmethod
    def _check_optional_password(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_strong_password(value)
