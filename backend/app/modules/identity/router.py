"""身份认证与平台账号管理 HTTP 路由（`/api/v1/auth/*` 与 `/api/v1/users/*`）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AuthError, ForbiddenError
from app.db.session import get_db_session
from app.modules.identity.deps import (
    Principal,
    get_current_principal,
    require_system_permission,
)
from app.modules.identity.schemas import (
    AdminUserCreateRequest,
    AdminUserItem,
    AdminUserListResponse,
    AdminUserUpdateRequest,
    AuthMeResponse,
    AuthTokenResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    LoginRequest,
    LogoutResponse,
    RegisterRequest,
)
from app.modules.identity.service import (
    admin_create_user,
    admin_update_user,
    build_auth_me_response,
    change_own_password,
    list_platform_users,
    login_with_password,
    logout_current_session,
    register_user_with_password,
    rotate_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["Auth"])
users_router = APIRouter(prefix="/users", tags=["Platform Users"])
_optional_bearer = HTTPBearer(auto_error=False)


@router.post(
    "/login",
    response_model=AuthTokenResponse,
    summary="邮箱密码登录",
    description="校验 Argon2id 密码，签发内存 Access JWT 并设置 Refresh Cookie 与 CSRF Cookie。",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthTokenResponse:
    """用户登录接口。"""
    return await login_with_password(
        payload=payload,
        request=request,
        response=response,
        session=session,
        settings=settings,
    )


@router.post(
    "/refresh",
    response_model=AuthTokenResponse,
    summary="Refresh Token 轮换",
    description="强制校验 X-CSRF-Token 与 Cookie 一致，执行单次令牌轮换；重放攻击将吊销整族会话。",
)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthTokenResponse:
    """静默刷新 Access Token 并轮换 Refresh Token。"""
    return await rotate_refresh_token(
        request=request,
        response=response,
        session=session,
        settings=settings,
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="安全登出",
    description="吊销当前会话并清除 Refresh 与 CSRF Cookie。",
)
async def logout(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> LogoutResponse:
    """登出并失效会话。"""
    principal: Principal | None = None
    if credentials is not None:
        try:
            principal = await get_current_principal(credentials=credentials, session=session)
        except AuthError:
            principal = None

    await logout_current_session(
        request=request,
        response=response,
        session=session,
        settings=settings,
        principal=principal,
    )
    return LogoutResponse(status="ok")


@router.get(
    "/me",
    response_model=AuthMeResponse,
    summary="获取当前认证身份与有效权限全景",
    description="返回当前用户、活跃组织、有效部门、SystemRole、OrganizationRole 及权限码集合。",
)
async def me(
    principal: Principal = Depends(get_current_principal),
    x_organization_id: str | None = Header(default=None, alias="X-Organization-Id"),
    session: AsyncSession = Depends(get_db_session),
) -> AuthMeResponse:
    """返回当前登录用户的身份与权威权限集合。"""
    requested_org_id: uuid.UUID | None = None
    if x_organization_id and x_organization_id.strip():
        try:
            requested_org_id = uuid.UUID(x_organization_id.strip())
        except ValueError as exc:
            raise ForbiddenError(
                code="INVALID_ORGANIZATION_HEADER",
                message="X-Organization-Id 格式无效",
            ) from exc

    return await build_auth_me_response(
        principal=principal,
        requested_org_id=requested_org_id,
        session=session,
    )


@router.post(
    "/register",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="企业账号自助注册",
    description=(
        "创建新用户并自动加入指定组织（默认 acme-corp），仅授予最小 EMPLOYEE 角色并签发登录会话。"
    ),
)
async def register_account(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AuthTokenResponse:
    """用户自助注册接口。"""
    return await register_user_with_password(
        payload=payload,
        request=request,
        response=response,
        session=session,
        settings=settings,
    )


@router.post(
    "/change-password",
    response_model=ChangePasswordResponse,
    summary="当前用户自助修改密码",
    description="校验原密码后更新 Argon2id 密码哈希，并立即吊销除当前会话外的全部其他设备会话。",
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    session: AsyncSession = Depends(get_db_session),
) -> ChangePasswordResponse:
    """修改当前登录账号密码。"""
    return await change_own_password(
        principal=principal,
        payload=payload,
        request=request,
        session=session,
    )


@users_router.get(
    "",
    response_model=AdminUserListResponse,
    summary="分页查询全平台用户账号台账（仅限 SYSTEM_ADMIN）",
)
async def get_platform_users(
    q: str | None = Query(default=None, max_length=120, description="按邮箱或姓名模糊检索"),
    status_filter: str | None = Query(
        default=None,
        alias="status",
        max_length=32,
        description="按账号状态筛选（ACTIVE / DISABLED / LOCKED）",
    ),
    offset: int = Query(default=0, ge=0, description="分页偏移量"),
    limit: int = Query(default=50, ge=1, le=200, description="每页条数"),
    principal: Principal = Depends(require_system_permission("system:user:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUserListResponse:
    """检索全平台用户列表、平台系统角色与所属组织绑定情况。"""
    return await list_platform_users(
        principal=principal,
        session=session,
        q=q,
        status_filter=status_filter,
        offset=offset,
        limit=limit,
    )


@users_router.post(
    "",
    response_model=AdminUserItem,
    status_code=status.HTTP_201_CREATED,
    summary="平台管理员创建用户账号（仅限 SYSTEM_ADMIN）",
)
async def create_platform_user(
    payload: AdminUserCreateRequest,
    request: Request,
    principal: Principal = Depends(require_system_permission("system:user:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUserItem:
    """新建平台账号，可选授予 SYSTEM_ADMIN 并直接绑定目标组织与角色。"""
    return await admin_create_user(
        principal=principal,
        payload=payload,
        request=request,
        session=session,
    )


@users_router.patch(
    "/{user_id}",
    response_model=AdminUserItem,
    summary="平台管理员更新账号状态/密码/系统角色（仅限 SYSTEM_ADMIN）",
)
async def update_platform_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_system_permission("system:user:manage")),
    session: AsyncSession = Depends(get_db_session),
) -> AdminUserItem:
    """更新账号姓名、启用/停用状态、管理员重置密码或调整 SYSTEM_ADMIN 授权。"""
    return await admin_update_user(
        principal=principal,
        target_user_id=user_id,
        payload=payload,
        request=request,
        session=session,
    )
