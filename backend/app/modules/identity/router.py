"""身份认证 HTTP 路由（`/api/v1/auth/*`）。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AuthError, ForbiddenError
from app.db.session import get_db_session
from app.modules.identity.deps import Principal, get_current_principal
from app.modules.identity.schemas import (
    AuthMeResponse,
    AuthTokenResponse,
    LoginRequest,
    LogoutResponse,
)
from app.modules.identity.service import (
    build_auth_me_response,
    login_with_password,
    logout_current_session,
    rotate_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["Auth"])
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
