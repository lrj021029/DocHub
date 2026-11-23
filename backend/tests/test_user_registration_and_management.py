"""账号自助注册、自助改密与平台账号管理真实 PostgreSQL 17 集成测试。

全部用例直连真实运行中的 PostgreSQL 17（`app_runtime` 受限角色 + FORCE RLS）：
1. 自助注册成功（201）：自动绑定默认 `acme-corp` 组织、主部门与最小 `EMPLOYEE` 角色，
   绝不授予 `SYSTEM_ADMIN` 或受限权限；
2. 重复邮箱注册返回 409 `EMAIL_ALREADY_REGISTERED`，弱密码返回 422；
3. 当前用户自助改密（`POST /api/v1/auth/change-password`）：
   旧密码错误返回 401，改密后其他设备会话立即被吊销；
4. 平台账号管理（`GET/POST/PATCH /api/v1/users`）：
   - 非 `SYSTEM_ADMIN`（如普通员工）访问全部返回 403；
   - `SYSTEM_ADMIN` 可分页检索全平台账号、新建带组织/角色的账号、
     停用账号并立即吊销其活跃会话、管理员重置密码并吊销会话、切换 `SYSTEM_ADMIN`；
   - 禁止管理员停用自身或撤销自身的 `SYSTEM_ADMIN`（返回 422）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.ids import new_uuid7_str
from app.db.seed import resolve_seed_password, seed_database
from app.db.session import get_db_session
from app.main import create_app
from app.modules.identity.models import User


def _make_test_settings() -> Settings:
    """加载真实数据库配置并关闭外部 OTLP 上报。"""
    base = get_settings()
    return base.model_copy(
        update={
            "app_env": "test",
            "app_debug": False,
            "otel_enabled": False,
            "cookie_secure": False,
        }
    )


@asynccontextmanager
async def _app_ctx() -> AsyncIterator[
    tuple[AsyncClient, async_sessionmaker[AsyncSession], dict[str, uuid.UUID], str]
]:
    """构建独立事件循环绑定的真实 app_runtime AsyncEngine 与 FastAPI 测试客户端。"""
    settings = _make_test_settings()
    engine = create_async_engine(
        settings.runtime_database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    async with session_factory() as seed_session:
        seed_ids = await seed_database(seed_session, settings=settings)

    app = create_app(settings=settings)
    app.dependency_overrides[get_db_session] = _override_db
    demo_pwd = resolve_seed_password(settings)

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, session_factory, seed_ids, demo_pwd
    finally:
        await engine.dispose()


async def _cleanup_users(
    session_factory: async_sessionmaker[AsyncSession],
    emails: list[str],
) -> None:
    """清理本次测试创建的临时用户（级联清理会话与成员关系）。"""
    if not emails:
        return
    async with session_factory() as session, session.begin():
        await session.execute(delete(User).where(User.email.in_(emails)))


@pytest.mark.asyncio
async def test_self_registration_security_and_password_change() -> None:
    """验证自助注册最小权限边界、重复冲突、弱口令拦截以及改密吊销其他会话。"""
    created_emails: list[str] = []
    async with _app_ctx() as (client, session_factory, _seed_ids, _demo_pwd):
        try:
            suffix = new_uuid7_str()[:8]
            email = f"selfreg-{suffix}@acme.local"
            created_emails.append(email)

            # 1. 弱密码（纯字母无数字）应被 422 拒绝
            weak_resp = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "display_name": "测试注册用户",
                    "password": "OnlyLettersPassword",
                    "organization_slug": "acme-corp",
                },
            )
            assert weak_resp.status_code == 422

            # 2. 合法注册返回 201，自动绑定 acme-corp 且仅授予 EMPLOYEE 角色
            reg_resp = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "display_name": "新注册员工",
                    "password": "InitPassword#2026",
                    "organization_slug": "acme-corp",
                },
            )
            assert reg_resp.status_code == 201, reg_resp.text
            reg_data = reg_resp.json()
            token_device_a = reg_data["access_token"]
            assert reg_data["user"]["email"] == email
            assert reg_data["active_organization_id"] is not None
            assert len(reg_data["organizations"]) == 1
            assert reg_data["organizations"][0]["slug"] == "acme-corp"

            # 3. 核验 /auth/me：仅含 EMPLOYEE 角色，绝不含 SYSTEM_ADMIN 或系统管理权限
            me_resp = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token_device_a}"},
            )
            assert me_resp.status_code == 200
            me_data = me_resp.json()
            assert me_data["system_roles"] == []
            assert [r["code"] for r in me_data["organization_roles"]] == ["EMPLOYEE"]
            assert "system:user:manage" not in me_data["permissions"]
            assert "chat:use" in me_data["permissions"]

            # 4. 重复邮箱注册返回 409 EMAIL_ALREADY_REGISTERED
            dup_resp = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "display_name": "重复用户",
                    "password": "AnotherPassword#2026",
                    "organization_slug": "acme-corp",
                },
            )
            assert dup_resp.status_code == 409
            assert dup_resp.json()["code"] == "EMAIL_ALREADY_REGISTERED"

            # 5. 新设备登录（设备 B）
            login_b = await client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "InitPassword#2026"},
            )
            assert login_b.status_code == 200
            token_device_b = login_b.json()["access_token"]

            # 6. 设备 A 错误原密码改密 -> 401
            bad_pwd = await client.post(
                "/api/v1/auth/change-password",
                headers={"Authorization": f"Bearer {token_device_a}"},
                json={
                    "current_password": "WrongPassword#1",
                    "new_password": "UpdatedPass#2026",
                },
            )
            assert bad_pwd.status_code == 401

            # 7. 设备 A 正确改密 -> 设备 A 仍有效，设备 B 会话被立即吊销
            ok_pwd = await client.post(
                "/api/v1/auth/change-password",
                headers={"Authorization": f"Bearer {token_device_a}"},
                json={
                    "current_password": "InitPassword#2026",
                    "new_password": "UpdatedPass#2026",
                },
            )
            assert ok_pwd.status_code == 200
            assert ok_pwd.json()["revoked_other_sessions"] >= 1

            # 设备 A 继续可用
            still_ok_a = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token_device_a}"},
            )
            assert still_ok_a.status_code == 200

            # 设备 B 已被立即吊销（401 SESSION_REVOKED）
            revoked_b = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {token_device_b}"},
            )
            assert revoked_b.status_code == 401
            assert revoked_b.json()["code"] == "SESSION_REVOKED"
        finally:
            await _cleanup_users(session_factory, created_emails)


@pytest.mark.asyncio
async def test_platform_user_management_admin_and_rbac_guards() -> None:
    """验证 /api/v1/users 的 SYSTEM_ADMIN 强鉴权、创建/停用/改密即时吊销与防自锁保护。"""
    created_emails: list[str] = []
    async with _app_ctx() as (client, session_factory, seed_ids, demo_pwd):
        try:
            # 1. 普通员工访问 /api/v1/users 全部返回 403
            emp_login = await client.post(
                "/api/v1/auth/login",
                json={"email": "employee@acme.local", "password": demo_pwd},
            )
            assert emp_login.status_code == 200
            emp_token = emp_login.json()["access_token"]

            denied_list = await client.get(
                "/api/v1/users",
                headers={"Authorization": f"Bearer {emp_token}"},
            )
            assert denied_list.status_code == 403

            # 2. SYSTEM_ADMIN 登录
            admin_login = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@acme.local", "password": demo_pwd},
            )
            assert admin_login.status_code == 200
            admin_token = admin_login.json()["access_token"]
            admin_user_id = admin_login.json()["user"]["id"]
            acme_org_id = admin_login.json()["active_organization_id"]
            assert acme_org_id is not None

            # 3. 管理员列表检索（跨组织遵循 FORCE RLS 统计所属组织）
            list_resp = await client.get(
                "/api/v1/users?q=acme.local",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert list_resp.status_code == 200
            list_data = list_resp.json()
            assert list_data["total"] >= 5
            admin_row = next(u for u in list_data["items"] if u["email"] == "admin@acme.local")
            assert "SYSTEM_ADMIN" in admin_row["system_roles"]
            assert any(org["slug"] == "acme-corp" for org in admin_row["organizations"])

            # 4. 管理员创建新用户并绑定到 acme-corp + KNOWLEDGE_MANAGER 角色
            suffix = new_uuid7_str()[:8]
            managed_email = f"managed-{suffix}@acme.local"
            created_emails.append(managed_email)

            create_resp = await client.post(
                "/api/v1/users",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "email": managed_email,
                    "display_name": "受管知识专员",
                    "password": "ManagedUser#2026",
                    "grant_system_admin": False,
                    "organization_id": acme_org_id,
                    "organization_role_code": "KNOWLEDGE_MANAGER",
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            created_user = create_resp.json()
            managed_user_id = created_user["id"]
            assert created_user["status"] == "ACTIVE"
            assert any(org["slug"] == "acme-corp" for org in created_user["organizations"])

            # 5. 受管用户登录并验证拿到 KNOWLEDGE_MANAGER 角色
            managed_login = await client.post(
                "/api/v1/auth/login",
                json={"email": managed_email, "password": "ManagedUser#2026"},
            )
            assert managed_login.status_code == 200
            managed_token = managed_login.json()["access_token"]

            managed_me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {managed_token}"},
            )
            assert managed_me.status_code == 200
            assert "KNOWLEDGE_MANAGER" in [
                r["code"] for r in managed_me.json()["organization_roles"]
            ]

            # 6. 管理员停用该账号 -> 该用户已签发的 Access Token 立即失效（401）
            disable_resp = await client.patch(
                f"/api/v1/users/{managed_user_id}",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"status": "DISABLED"},
            )
            assert disable_resp.status_code == 200
            assert disable_resp.json()["status"] == "DISABLED"

            blocked_me = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": f"Bearer {managed_token}"},
            )
            assert blocked_me.status_code == 401

            # 7. 重新启用并由管理员重置密码 -> 旧密码失效，新密码可用
            reset_resp = await client.patch(
                f"/api/v1/users/{managed_user_id}",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={
                    "status": "ACTIVE",
                    "new_password": "ResetByAdmin#2026",
                    "grant_system_admin": True,
                },
            )
            assert reset_resp.status_code == 200
            assert "SYSTEM_ADMIN" in reset_resp.json()["system_roles"]

            old_login_fail = await client.post(
                "/api/v1/auth/login",
                json={"email": managed_email, "password": "ManagedUser#2026"},
            )
            assert old_login_fail.status_code == 401

            new_login_ok = await client.post(
                "/api/v1/auth/login",
                json={"email": managed_email, "password": "ResetByAdmin#2026"},
            )
            assert new_login_ok.status_code == 200

            # 8. 防自锁安全守卫：禁止停用自身、禁止撤销自身 SYSTEM_ADMIN
            self_disable = await client.patch(
                f"/api/v1/users/{admin_user_id}",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"status": "DISABLED"},
            )
            assert self_disable.status_code == 422
            assert self_disable.json()["code"] == "CANNOT_DISABLE_SELF"

            self_demote = await client.patch(
                f"/api/v1/users/{admin_user_id}",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"grant_system_admin": False},
            )
            assert self_demote.status_code == 422
            assert self_demote.json()["code"] == "CANNOT_DEMOTE_SELF"
        finally:
            await _cleanup_users(session_factory, created_emails)
