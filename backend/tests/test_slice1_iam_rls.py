"""Slice 1 真实 PostgreSQL 17 集成测试与 FORCE RLS 安全硬测试。

全部测试均直连真实运行中的 PostgreSQL 17（`app_runtime` 受限角色，无 SUPERUSER / 无 BYPASSRLS）：
1. 登录、Refresh Token 轮换、重放攻击整族吊销与安全审计落盘、CSRF 校验、登出即刻失效。
2. 多组织创建与切换、跨组织 IDOR 拦截（403）与 Header/Path 组织一致性校验。
3. PostgreSQL FORCE RLS 硬测试：
   - 不设 `app.current_organization_id` / `app.current_user_id` 查询全部 9 张租户表返回 0 行；
   - 设置 Org A 绝不返回 Org B，且越权写入 Org B 被数据库 RLS `WITH CHECK` 硬拒绝；
   - 20 并发协程在受限连接池（pool_size=4）上交替读写 Org A / Org B，验证零上下文泄漏。
4. 部门树 Closure Table 建树、子树迁移、防环检测与多部门兼职有效期过滤。
5. 后端 `require_permission` 强制鉴权、`SYSTEM_ADMIN` 绝不隐式具备 `document:read` 权限验证。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.ids import new_uuid7_str
from app.db.seed import resolve_seed_password, seed_database
from app.db.session import get_db_session, org_rls_session
from app.main import create_app
from app.modules.audit.models import AuditEvent, SecurityAuditEvent
from app.modules.departments.models import Department, DepartmentClosure
from app.modules.identity.deps import OrgContext, require_permission
from app.modules.organizations.models import OrganizationMember

ALL_TENANT_TABLES: tuple[str, ...] = (
    "organization_members",
    "departments",
    "department_closures",
    "department_memberships",
    "organization_roles",
    "organization_role_permissions",
    "user_role_memberships",
    "audit_events",
    "security_audit_events",
)


def _make_integration_settings() -> Settings:
    """加载真实本地连接配置并关闭 OTLP 外部上报以免干扰测试速度。"""
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
async def _isolated_app_context(
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession], dict[str, uuid.UUID], str]]:
    """为每个异步测试构建独立事件循环绑定的真实 app_runtime 引擎、FastAPI 实例与种子数据。"""
    settings = _make_integration_settings()
    engine = create_async_engine(
        settings.runtime_database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    async def _override_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                if session.in_transaction():
                    await session.rollback()
                raise
            finally:
                if session.in_transaction():
                    await session.rollback()

    # 幂等初始化种子数据
    async with session_factory() as seed_session:
        org_ids = await seed_database(seed_session, settings=settings)

    app = create_app(settings=settings)
    app.dependency_overrides[get_db_session] = _override_db_session

    # 挂载受 document:read 保护的探测路由，用于硬验证 SystemAdmin 绝无隐式文档读取权
    probe_router = APIRouter(prefix="/api/v1/_slice1_probe")

    @probe_router.get("/organizations/{org_id}/documents-probe")
    async def documents_read_probe(
        org_id: uuid.UUID,
        org_ctx: OrgContext = Depends(require_permission("document:read")),
    ) -> dict[str, str]:
        _ = org_id
        return {
            "organization_id": str(org_ctx.organization_id),
            "user_id": str(org_ctx.principal.user_id),
        }

    app.include_router(probe_router)

    demo_password = resolve_seed_password(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, session_factory, org_ids, demo_password
    finally:
        await engine.dispose()


async def _login(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> tuple[dict[str, object], str, str]:
    """辅助登录函数：返回 (响应 JSON, refresh_cookie, csrf_token)。"""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    refresh_cookie = resp.cookies.get("dochub_refresh_token")
    csrf_cookie = resp.cookies.get("dochub_csrf")
    body = resp.json()
    assert refresh_cookie
    assert csrf_cookie
    assert body["csrf_token"] == csrf_cookie
    return body, refresh_cookie, csrf_cookie


def _cookie_header(refresh_token: str, csrf_token: str) -> str:
    """构造显式 Cookie 请求头，避免触发 httpx 单次请求 cookies 参数弃用警告。"""
    return f"dochub_refresh_token={refresh_token}; dochub_csrf={csrf_token}"


@pytest.mark.asyncio
async def test_auth_login_refresh_rotation_replay_revocation_csrf_and_logout() -> None:
    """验证登录、CSRF 强制校验、Refresh 轮换、重放攻击整族吊销、安全审计与登出失效。"""
    async with _isolated_app_context() as (client, session_factory, org_ids, demo_password):
        # 1. 密码错误拒绝
        bad_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "admin@acme.local", "password": "WrongPassword!"},
        )
        assert bad_resp.status_code == 401

        # 2. 正常登录（获取 T1）
        login_body, refresh_t1, csrf_t1 = await _login(
            client,
            email="admin@acme.local",
            password=demo_password,
        )
        access_t1 = str(login_body["access_token"])
        assert login_body["active_organization_id"] == str(org_ids["acme-corp"])

        # 3. /auth/me 验证 SystemAdmin 不包含 document:read
        me_resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_t1}"},
        )
        assert me_resp.status_code == 200
        me_body = me_resp.json()
        assert me_body["user"]["email"] == "admin@acme.local"
        assert "SYSTEM_ADMIN" in [r["code"] for r in me_body["system_roles"]]
        assert "document:read" not in me_body["permissions"]

        # 清空客户端自动保存的 Cookie，确保以下测试精确控制提交的 Cookie 与 CSRF 头
        client.cookies.clear()

        # 4. 缺失 X-CSRF-Token 触发 403 阻断
        no_csrf_resp = await client.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": _cookie_header(refresh_t1, csrf_t1)},
        )
        assert no_csrf_resp.status_code == 403
        assert no_csrf_resp.json()["code"] == "CSRF_MISSING"

        # 5. 篡改 X-CSRF-Token 触发 403 阻断
        bad_csrf_resp = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "X-CSRF-Token": "tampered.signature",
                "Cookie": _cookie_header(refresh_t1, csrf_t1),
            },
        )
        assert bad_csrf_resp.status_code == 403
        assert bad_csrf_resp.json()["code"] == "CSRF_INVALID"

        # 6. 合法 CSRF + Refresh Cookie 执行轮换（得到 T2）
        rot_resp = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "X-CSRF-Token": csrf_t1,
                "Cookie": _cookie_header(refresh_t1, csrf_t1),
            },
        )
        assert rot_resp.status_code == 200, rot_resp.text
        refresh_t2 = rot_resp.cookies.get("dochub_refresh_token")
        csrf_t2 = rot_resp.cookies.get("dochub_csrf")
        access_t2 = rot_resp.json()["access_token"]
        assert refresh_t2 and refresh_t2 != refresh_t1
        assert csrf_t2

        # 7. 重放攻击：再次使用已轮换的旧令牌 T1 -> 立即吊销整个家族（包括 T2）并落盘安全审计
        client.cookies.clear()
        replay_resp = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "X-CSRF-Token": csrf_t1,
                "Cookie": _cookie_header(refresh_t1, csrf_t1),
            },
        )
        assert replay_resp.status_code == 401
        assert replay_resp.json()["code"] == "REFRESH_TOKEN_REUSED"

        # 验证新令牌 T2 的 Access JWT 与 Refresh Cookie 均已随家族连坐吊销
        t2_me_resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_t2}"},
        )
        assert t2_me_resp.status_code == 401
        assert t2_me_resp.json()["code"] == "SESSION_REVOKED"

        client.cookies.clear()
        t2_refresh_resp = await client.post(
            "/api/v1/auth/refresh",
            headers={
                "X-CSRF-Token": csrf_t2,
                "Cookie": _cookie_header(refresh_t2, csrf_t2),
            },
        )
        assert t2_refresh_resp.status_code == 401

        # 验证安全审计表真实写入了 CRITICAL 重放事件
        async with (
            session_factory() as verify_session,
            org_rls_session(verify_session, org_ids["acme-corp"]),
        ):
            replay_events = (
                await verify_session.execute(
                    select(SecurityAuditEvent).where(
                        SecurityAuditEvent.event_type == "auth.refresh.replay_detected",
                        SecurityAuditEvent.severity == "CRITICAL",
                    )
                )
            ).scalars().all()
            assert len(replay_events) >= 1

        # 8. 重新登录并验证登出后 Access Token 与 Refresh Token 立即失效
        client.cookies.clear()
        fresh_body, fresh_refresh, fresh_csrf = await _login(
            client,
            email="employee@acme.local",
            password=demo_password,
        )
        fresh_access = str(fresh_body["access_token"])
        client.cookies.clear()
        logout_resp = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Authorization": f"Bearer {fresh_access}",
                "X-CSRF-Token": fresh_csrf,
                "Cookie": _cookie_header(fresh_refresh, fresh_csrf),
            },
        )
        assert logout_resp.status_code == 200

        after_logout_me = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {fresh_access}"},
        )
        assert after_logout_me.status_code == 401


@pytest.mark.asyncio
async def test_multi_org_switch_and_cross_org_idor_blocking() -> None:
    """验证多组织创建与切换，以及跨组织 IDOR 严格拦截（403）。"""
    async with _isolated_app_context() as (client, _sf, org_ids, demo_password):
        acme_id = org_ids["acme-corp"]
        beta_id = org_ids["beta-corp"]

        admin_body, _, _ = await _login(
            client,
            email="admin@acme.local",
            password=demo_password,
        )
        admin_token = str(admin_body["access_token"])

        # 1. 跨组织 IDOR：admin@acme.local 非 beta-corp 成员，任何操作均应被 403 拦截
        for method, path in (
            ("POST", f"/api/v1/organizations/{beta_id}/switch"),
            ("GET", f"/api/v1/organizations/{beta_id}/members"),
            ("GET", f"/api/v1/organizations/{beta_id}/departments"),
            ("GET", f"/api/v1/organizations/{beta_id}/roles"),
        ):
            resp = await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert resp.status_code == 403, f"{method} {path} 未被拦截: {resp.status_code}"

        # 2. Header 与 Path 组织不一致必须被拦截
        mismatch_resp = await client.get(
            f"/api/v1/organizations/{acme_id}/members",
            headers={
                "Authorization": f"Bearer {admin_token}",
                "X-Organization-Id": str(beta_id),
            },
        )
        assert mismatch_resp.status_code == 403
        assert mismatch_resp.json()["code"] == "ORG_CONTEXT_MISMATCH"

        # 3. SystemAdmin 创建新组织并合法切换
        unique_slug = f"gamma-{new_uuid7_str()[:8]}"
        create_org_resp = await client.post(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"name": "Gamma 测试组织", "slug": unique_slug},
        )
        assert create_org_resp.status_code == 201, create_org_resp.text
        gamma_id = create_org_resp.json()["id"]

        orgs_resp = await client.get(
            "/api/v1/organizations",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert orgs_resp.status_code == 200
        visible_slugs = {item["slug"] for item in orgs_resp.json()}
        assert "acme-corp" in visible_slugs
        assert unique_slug in visible_slugs
        assert "beta-corp" not in visible_slugs

        switch_resp = await client.post(
            f"/api/v1/organizations/{gamma_id}/switch",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert switch_resp.status_code == 200, switch_resp.text
        switched_token = switch_resp.json()["access_token"]
        assert switch_resp.json()["active_organization_id"] == gamma_id

        me_after_switch = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {switched_token}"},
        )
        assert me_after_switch.status_code == 200
        assert me_after_switch.json()["active_organization_id"] == gamma_id

        # 4. bob@beta.local 无法访问 acme-corp
        bob_body, _, _ = await _login(
            client,
            email="bob@beta.local",
            password=demo_password,
        )
        bob_token = str(bob_body["access_token"])
        bob_idor = await client.get(
            f"/api/v1/organizations/{acme_id}/departments",
            headers={"Authorization": f"Bearer {bob_token}"},
        )
        assert bob_idor.status_code == 403


@pytest.mark.asyncio
async def test_postgresql_force_rls_fail_closed_and_20_concurrent_pool_isolation() -> None:
    """PostgreSQL FORCE RLS 硬测试：零上下文 Fail-Closed、跨租户写阻断与 20 并发池复用零泄漏。"""
    # 故意限制连接池大小（pool_size=4, max_overflow=2）强制 20 个并发协程高频复用同一批物理连接
    async with _isolated_app_context(pool_size=4, max_overflow=2) as (
        _client,
        session_factory,
        org_ids,
        _demo_password,
    ):
        acme_id = org_ids["acme-corp"]
        beta_id = org_ids["beta-corp"]

        # 1. 核验真实运行角色为 app_runtime 且无 SUPERUSER / BYPASSRLS
        async with session_factory() as session, session.begin():
            role_row = (
                await session.execute(
                    text(
                        "SELECT current_user, rolsuper, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).one()
            assert role_row[0] == "app_runtime"
            assert role_row[1] is False
            assert role_row[2] is False

            # 不设置任何 RLS 变量查询全部 9 张租户表，必须严格 Fail-Closed 返回 0 行
            for table_name in ALL_TENANT_TABLES:
                count = (
                    await session.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
                ).scalar_one()
                assert count == 0, f"表 {table_name} 在未设置 RLS 上下文时泄漏了 {count} 行"

        # 2. 设置 Org A 上下文绝不返回 Org B；越权写入 Org B 必须被 RLS WITH CHECK 拒绝
        async with session_factory() as session, org_rls_session(session, acme_id):
            depts = (await session.execute(select(Department))).scalars().all()
            assert len(depts) >= 5
            assert all(d.organization_id == acme_id for d in depts)

            members = (await session.execute(select(OrganizationMember))).scalars().all()
            assert len(members) >= 5
            assert all(m.organization_id == acme_id for m in members)

        async with session_factory() as session:
            with pytest.raises(DBAPIError):
                async with org_rls_session(session, acme_id):
                    # 尝试在 Org A 上下文中强行插入 Org B 的部门，PostgreSQL 必须直接报错阻断
                    session.add(
                        Department(
                            organization_id=beta_id,
                            parent_id=None,
                            name=f"forged-{new_uuid7_str()[:8]}",
                        )
                    )
                    await session.flush()

        # 3. 20 个并发协程 × 5 轮交替在 Org A / Org B 读写，验证连接池复用零上下文污染
        async def _worker(worker_idx: int) -> None:
            for round_idx in range(5):
                target_org = acme_id if (worker_idx + round_idx) % 2 == 0 else beta_id
                marker = f"w{worker_idx}-r{round_idx}-{new_uuid7_str()[:6]}"
                async with session_factory() as session, org_rls_session(session, target_org):
                    # 写入一条属于当前租户的审计记录
                    session.add(
                        AuditEvent(
                            organization_id=target_org,
                            actor_user_id=None,
                            action="rls.concurrency.probe",
                            resource_type="probe",
                            resource_id=marker,
                            details_json={"worker": worker_idx, "round": round_idx},
                        )
                    )
                    await session.flush()

                    # 并发读取部门与审计记录，断言绝无另一租户行混入
                    visible_depts = (await session.execute(select(Department))).scalars().all()
                    assert len(visible_depts) > 0
                    assert all(d.organization_id == target_org for d in visible_depts)

                    visible_audits = (await session.execute(select(AuditEvent))).scalars().all()
                    assert any(ev.resource_id == marker for ev in visible_audits)
                    assert all(ev.organization_id == target_org for ev in visible_audits)

        await asyncio.gather(*(_worker(idx) for idx in range(20)))

        # 4. 并发压测结束后再次空上下文直查，确认连接池无任何残留 SET LOCAL
        async with session_factory() as post_session, post_session.begin():
            for table_name in ALL_TENANT_TABLES:
                count = (
                    await post_session.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
                ).scalar_one()
                assert count == 0


@pytest.mark.asyncio
async def test_department_closure_tree_move_cycle_detection_and_validity_windows() -> None:
    """验证部门 Closure Table 建树、移动子树、防环阻断及多部门兼职有效期过滤。"""
    async with _isolated_app_context() as (client, session_factory, org_ids, demo_password):
        acme_id = org_ids["acme-corp"]
        admin_body, _, _ = await _login(
            client,
            email="admin@acme.local",
            password=demo_password,
        )
        admin_headers = {"Authorization": f"Bearer {admin_body['access_token']}"}

        # 1. 查询现有部门树
        depts_resp = await client.get(
            f"/api/v1/organizations/{acme_id}/departments",
            headers=admin_headers,
        )
        assert depts_resp.status_code == 200
        dept_map = {item["name"]: item for item in depts_resp.json()}
        assert {"总部", "财务部", "人力资源部", "客服部", "研发部"}.issubset(dept_map.keys())
        hq_id = uuid.UUID(dept_map["总部"]["id"])
        rd_id = uuid.UUID(dept_map["研发部"]["id"])
        cs_id = uuid.UUID(dept_map["客服部"]["id"])
        fin_id = uuid.UUID(dept_map["财务部"]["id"])

        # 2. 在研发部下建立多级子树：研发部 -> 平台组 -> 存储小组
        suffix = new_uuid7_str()[:6]
        plat_resp = await client.post(
            f"/api/v1/organizations/{acme_id}/departments",
            headers=admin_headers,
            json={"name": f"平台组-{suffix}", "parent_id": str(rd_id)},
        )
        assert plat_resp.status_code == 201, plat_resp.text
        plat_id = uuid.UUID(plat_resp.json()["id"])
        assert plat_resp.json()["depth"] == 2

        storage_resp = await client.post(
            f"/api/v1/organizations/{acme_id}/departments",
            headers=admin_headers,
            json={"name": f"存储小组-{suffix}", "parent_id": str(plat_id)},
        )
        assert storage_resp.status_code == 201, storage_resp.text
        storage_id = uuid.UUID(storage_resp.json()["id"])
        assert storage_resp.json()["depth"] == 3

        # 3. 防环检测：将研发部移动到其孙节点存储小组下，或设为自身 -> 必须返回 409
        cycle_desc_resp = await client.patch(
            f"/api/v1/organizations/{acme_id}/departments/{rd_id}",
            headers=admin_headers,
            json={"parent_id": str(storage_id)},
        )
        assert cycle_desc_resp.status_code == 409
        assert cycle_desc_resp.json()["code"] == "DEPARTMENT_CYCLE_DETECTED"

        cycle_self_resp = await client.patch(
            f"/api/v1/organizations/{acme_id}/departments/{plat_id}",
            headers=admin_headers,
            json={"parent_id": str(plat_id)},
        )
        assert cycle_self_resp.status_code == 409
        assert cycle_self_resp.json()["code"] == "DEPARTMENT_CYCLE_DETECTED"

        # 4. 合法移动子树：将平台组（携存储小组）从研发部迁移至客服部下
        move_resp = await client.patch(
            f"/api/v1/organizations/{acme_id}/departments/{plat_id}",
            headers=admin_headers,
            json={"parent_id": str(cs_id)},
        )
        assert move_resp.status_code == 200, move_resp.text
        assert move_resp.json()["parent_id"] == str(cs_id)

        # 核验 PostgreSQL 中 department_closures 已精确更新且彻底移除旧祖先路径
        async with session_factory() as verify_session, org_rls_session(verify_session, acme_id):
            closures = (
                await verify_session.execute(
                    select(DepartmentClosure).where(
                        DepartmentClosure.descendant_id == storage_id
                    )
                )
            ).scalars().all()
            depth_by_ancestor = {c.ancestor_id: c.depth for c in closures}
            assert depth_by_ancestor == {
                storage_id: 0,
                plat_id: 1,
                cs_id: 2,
                hq_id: 3,
            }
            assert rd_id not in depth_by_ancestor

        # 5. 多部门兼职与有效期过滤测试
        emp_body, _, _ = await _login(
            client,
            email="employee@acme.local",
            password=demo_password,
        )
        emp_user_id = emp_body["user"]["id"]
        emp_headers = {"Authorization": f"Bearer {emp_body['access_token']}"}
        now = datetime.now(UTC)

        # 分配有效兼职部门（财务部）
        sec_resp = await client.post(
            f"/api/v1/organizations/{acme_id}/departments/{fin_id}/memberships",
            headers=admin_headers,
            json={
                "user_id": emp_user_id,
                "membership_type": "SECONDARY",
                "is_primary": False,
                "valid_from": (now - timedelta(hours=1)).isoformat(),
                "valid_until": (now + timedelta(days=30)).isoformat(),
            },
        )
        assert sec_resp.status_code == 201, sec_resp.text

        # 分配已过期临时部门（客服部）
        exp_resp = await client.post(
            f"/api/v1/organizations/{acme_id}/departments/{cs_id}/memberships",
            headers=admin_headers,
            json={
                "user_id": emp_user_id,
                "membership_type": "TEMPORARY",
                "is_primary": False,
                "valid_from": (now - timedelta(days=5)).isoformat(),
                "valid_until": (now - timedelta(days=1)).isoformat(),
            },
        )
        assert exp_resp.status_code == 201, exp_resp.text

        emp_me = await client.get("/api/v1/auth/me", headers=emp_headers)
        assert emp_me.status_code == 200
        active_dept_names = {d["department_name"] for d in emp_me.json()["departments"]}
        assert "研发部" in active_dept_names
        assert "财务部" in active_dept_names
        assert "客服部" not in active_dept_names  # 过期归属自动失效


@pytest.mark.asyncio
async def test_rbac_enforcement_and_system_admin_no_implicit_document_read() -> None:
    """验证 require_permission 强校验、角色物理分离与 SystemAdmin 零隐式文档读取权。"""
    async with _isolated_app_context() as (client, _sf, org_ids, demo_password):
        acme_id = org_ids["acme-corp"]

        admin_body, _, _ = await _login(
            client,
            email="admin@acme.local",
            password=demo_password,
        )
        admin_headers = {"Authorization": f"Bearer {admin_body['access_token']}"}

        emp_body, _, _ = await _login(
            client,
            email="employee@acme.local",
            password=demo_password,
        )
        emp_headers = {"Authorization": f"Bearer {emp_body['access_token']}"}

        # 1. SystemAdmin 访问需要 document:read 的接口必须返回 403（绝无隐式万能旁路）
        admin_doc_probe = await client.get(
            f"/api/v1/_slice1_probe/organizations/{acme_id}/documents-probe",
            headers=admin_headers,
        )
        assert admin_doc_probe.status_code == 403
        assert admin_doc_probe.json()["code"] == "PERMISSION_DENIED"

        # 2. 拥有 EMPLOYEE 组织角色的员工访问 document:read 探针允许通过
        emp_doc_probe = await client.get(
            f"/api/v1/_slice1_probe/organizations/{acme_id}/documents-probe",
            headers=emp_headers,
        )
        assert emp_doc_probe.status_code == 200

        # 3. 普通员工无权管理组织成员或角色（403 PERMISSION_DENIED）
        emp_create_dept = await client.post(
            f"/api/v1/organizations/{acme_id}/departments",
            headers=emp_headers,
            json={"name": "越权新建部门"},
        )
        assert emp_create_dept.status_code == 403
        assert emp_create_dept.json()["code"] == "PERMISSION_DENIED"

        # 4. 禁止组织角色绑定 SYSTEM 作用域权限
        bad_role_resp = await client.post(
            f"/api/v1/organizations/{acme_id}/roles",
            headers=admin_headers,
            json={
                "code": f"BAD_ROLE_{new_uuid7_str()[:4].upper()}",
                "name": "非法越权角色",
                "permission_codes": ["system:org:manage"],
            },
        )
        assert bad_role_resp.status_code == 422
        assert bad_role_resp.json()["code"] == "SYSTEM_PERMISSION_FORBIDDEN_IN_ORG_ROLE"

        # 5. GET /api/v1/permissions 正确分离 SYSTEM 与 ORGANIZATION 作用域
        perm_resp = await client.get("/api/v1/permissions", headers=admin_headers)
        assert perm_resp.status_code == 200
        perm_body = perm_resp.json()
        scopes = {p["scope"] for p in perm_body["permissions"]}
        assert scopes == {"SYSTEM", "ORGANIZATION"}
        assert any(sr["code"] == "SYSTEM_ADMIN" for sr in perm_body["system_roles"])
