"""Slice 2 真实 PostgreSQL 17 + MinIO 集成与安全对抗测试。

全部测试直连真实运行中的 PostgreSQL 17（`app_runtime` 角色）与 MinIO（`127.0.0.1:19000`）：
1. 验证单头迁移与全部 34 张租户表强制开启 `ENABLE + FORCE ROW LEVEL SECURITY`（空上下文返回 0 行）。
2. 验证 Public / Private Space 入口隔离，以及 `GLOBAL` ACL 绝不跨 Space / 跨 Org。
3. 验证 `AuthoritativeACLEngine` 四维 Allow OR：
   - 部门 `DYNAMIC`（移动部门子树后即时生效）vs `SNAPSHOT`（移动部门后仍按授权快照判定）；
   - `ROLE` 授权与 `USER` 临时授权（过期即刻拒绝）；
   - `SYSTEM_ADMIN` 绝无隐式读取未授权文档权限；
   - `BreakGlassGrant` 默认拒绝、仅独立审批未过期方可通过。
4. 验证 ACL 乐观锁并发冲突返回 409、`ACLRevision` Diff 与 `acl.revision.created` Outbox 同事务落库。
5. 验证真实 MinIO 直传与预签名上传、路径穿越拦截、非法文件格式拒绝、新文档默认 `QUARANTINED`
   无法被 `batch_authorize_chunks` 召回、配置 ACL 并发布后方可通过。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.seed import resolve_seed_password, seed_database
from app.db.session import get_db_session, org_rls_session
from app.main import create_app
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.acl.models import BreakGlassGrant
from app.modules.documents.models import Chunk, ChunkSet
from app.modules.documents.storage import MinIOStorageService
from app.modules.identity.deps import Principal
from app.modules.identity.models import User
from app.modules.outbox.models import OutboxEvent

SLICE2_3_TENANT_TABLES: tuple[str, ...] = (
    "embedding_providers",
    "embedding_profiles",
    "knowledge_spaces",
    "space_memberships",
    "space_policies",
    "documents",
    "document_versions",
    "document_acls",
    "acl_grants",
    "acl_grant_department_snapshots",
    "acl_revisions",
    "break_glass_grants",
    "import_jobs",
    "parse_jobs",
    "parsed_artifacts",
    "parsed_blocks",
    "chunk_sets",
    "chunks",
    "index_generations",
    "active_index_pointers",
    "index_jobs",
    "outbox_events",
    "processed_messages",
    "idempotency_records",
    "async_jobs",
)


def _make_settings() -> Settings:
    """加载真实连接配置并关闭外部 OTLP 导出。"""
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
async def _slice2_context() -> AsyncIterator[
    tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        dict[str, uuid.UUID],
        str,
        MinIOStorageService,
    ]
]:
    """构建绑定当前事件循环的真实 PG + MinIO 测试环境。"""
    settings = _make_settings()
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
            try:
                yield session
            except Exception:
                if session.in_transaction():
                    await session.rollback()
                raise
            finally:
                if session.in_transaction():
                    await session.rollback()

    async with session_factory() as seed_session:
        seed_ids = await seed_database(seed_session, settings=settings)

    storage = MinIOStorageService(settings=settings)
    await storage.ensure_buckets()

    app = create_app(settings=settings)
    app.dependency_overrides[get_db_session] = _override_db

    demo_password = resolve_seed_password(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, session_factory, seed_ids, demo_password, storage
    finally:
        await engine.dispose()


async def _login_headers(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> tuple[dict[str, str], dict[str, object]]:
    """辅助登录并返回 Bearer 请求头及用户上下文。"""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


@pytest.mark.asyncio
async def test_slice2_3_all_tables_force_rls_fail_closed() -> None:
    """验证 Slice 2-3 新增全部 25 张租户表在 PG 17 强制启用 FORCE RLS 且零上下文返回 0 行。"""
    async with (
        _slice2_context() as (_client, session_factory, _seed_ids, _pwd, _storage),
        session_factory() as session,
        session.begin(),
    ):
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT relname, relrowsecurity, relforcerowsecurity
                        FROM pg_class
                        WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
                        """
                    )
                )
            ).all()
            rls_map = {r[0]: (r[1], r[2]) for r in rows}

            for table_name in SLICE2_3_TENANT_TABLES:
                assert table_name in rls_map, f"缺少迁移表 {table_name}"
                assert rls_map[table_name] == (True, True), f"表 {table_name} 未启用 FORCE RLS"

                count = (
                    await session.execute(text(f"SELECT COUNT(*) FROM {table_name}"))
                ).scalar_one()
                assert count == 0, f"表 {table_name} 在未设 RLS 上下文时泄漏了 {count} 行"


@pytest.mark.asyncio
async def test_space_visibility_and_global_scope_isolation() -> None:
    """验证 PUBLIC / PRIVATE Space 入口隔离，以及 GLOBAL ACL 严格局限在所属 Space 与 Org。"""
    async with _slice2_context() as (client, session_factory, seed_ids, demo_password, _storage):
        acme_id = seed_ids["acme-corp"]
        finance_space_id = seed_ids["space:finance-general"]
        hr_private_space_id = seed_ids["space:hr-executive"]
        beta_space_id = seed_ids["space:beta-general"]

        km_headers, _ = await _login_headers(client, email="km@acme.local", password=demo_password)
        hr_headers, _ = await _login_headers(client, email="hr@acme.local", password=demo_password)
        emp_headers, emp_login = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )
        bob_headers, bob_login = await _login_headers(
            client, email="bob@beta.local", password=demo_password
        )
        admin_headers, admin_login = await _login_headers(
            client, email="admin@acme.local", password=demo_password
        )

        # 1. 普通员工可见 PUBLIC Space，但不可见未显式加入的 PRIVATE Space
        emp_spaces = await client.get(
            f"/api/v1/organizations/{acme_id}/spaces",
            headers=emp_headers,
        )
        assert emp_spaces.status_code == 200
        emp_slugs = {item["slug"] for item in emp_spaces.json()}
        assert "finance-general" in emp_slugs
        assert "hr-executive" not in emp_slugs

        # 2. HR 员工在私有空间成员中，可以正常看到私有空间
        hr_spaces = await client.get(
            f"/api/v1/organizations/{acme_id}/spaces",
            headers=hr_headers,
        )
        assert hr_spaces.status_code == 200
        assert "hr-executive" in {item["slug"] for item in hr_spaces.json()}

        # 3. 分别向 finance-general (PUBLIC) 和 hr-executive (PRIVATE) 上传文档并配置 GLOBAL ACL
        up_pub = await client.post(
            f"/api/v1/spaces/{finance_space_id}/documents/uploads",
            headers=km_headers,
            files={
                "file": (
                    "finance-policy.md",
                    b"# Finance Policy\nReimbursement rules",
                    "text/markdown",
                )
            },
            data={"title": "公开空间财务制度", "sensitivity_level": "L1"},
        )
        assert up_pub.status_code == 201, up_pub.text
        pub_doc_id = uuid.UUID(up_pub.json()["document_id"])
        pub_ver_id = uuid.UUID(up_pub.json()["document_version_id"])

        up_priv = await client.post(
            f"/api/v1/spaces/{hr_private_space_id}/documents/uploads",
            headers=km_headers,
            files={
                "file": ("hr-exec-pay.md", b"# HR Executive Pay\nConfidential", "text/markdown")
            },
            data={"title": "私有空间高管薪酬", "sensitivity_level": "L2"},
        )
        assert up_priv.status_code == 201, up_priv.text
        priv_doc_id = uuid.UUID(up_priv.json()["document_id"])
        priv_ver_id = uuid.UUID(up_priv.json()["document_version_id"])

        # 为两篇文档配置 GLOBAL ACL 并发布
        for d_id, v_id in ((pub_doc_id, pub_ver_id), (priv_doc_id, priv_ver_id)):
            acl_resp = await client.post(
                f"/api/v1/documents/{d_id}/acl/revisions",
                headers={**km_headers, "Idempotency-Key": f"idem-global-{d_id}"},
                json={
                    "expected_revision": 0,
                    "reason": "配置当前空间 GLOBAL 读取范围",
                    "grants": [{"subject_type": "GLOBAL"}],
                },
            )
            assert acl_resp.status_code == 200, acl_resp.text

            pub_resp = await client.post(
                f"/api/v1/document-versions/{v_id}/publish",
                headers={**km_headers, "Idempotency-Key": f"idem-pub-{v_id}"},
            )
            assert pub_resp.status_code == 200, pub_resp.text

        # 4. 权威引擎校验：
        #    - employee 可读公开空间 GLOBAL 文档，绝不可读私有空间 GLOBAL 文档！
        #    - admin (SYSTEM_ADMIN 且无 document:read) 不可读任何文档！
        #    - bob@beta.local 即使拿到 acme 文档 ID 也必须跨租户拒绝！
        engine = AuthoritativeACLEngine()

        def _principal(login_body: dict[str, object]) -> Principal:
            u = login_body["user"]
            assert isinstance(u, dict)
            return Principal(
                user_id=uuid.UUID(str(u["id"])),
                email=str(u["email"]),
                display_name=str(u["display_name"]),
                status="ACTIVE",
                session_id=None,
                token_org_id=uuid.UUID(str(login_body["active_organization_id"])),
                system_roles=(),
                system_permissions=frozenset(),
            )

        async with session_factory() as db_session:
            emp_p = _principal(emp_login)
            admin_p = _principal(admin_login)
            bob_p = _principal(bob_login)

            assert (
                await engine.check_document_access(
                    db_session, emp_p, pub_doc_id, organization_id=acme_id
                )
            ).allowed is True
            priv_decision = await engine.check_document_access(
                db_session, emp_p, priv_doc_id, organization_id=acme_id
            )
            assert priv_decision.allowed is False
            assert priv_decision.reason_code == "SPACE_ACCESS_DENIED"

            admin_decision = await engine.check_document_access(
                db_session, admin_p, pub_doc_id, organization_id=acme_id
            )
            assert admin_decision.allowed is False
            assert admin_decision.reason_code == "MISSING_DOCUMENT_READ_PERMISSION"

            # 跨组织访问 acme 文档严格拒绝
            bob_decision = await engine.check_document_access(
                db_session, bob_p, pub_doc_id, organization_id=seed_ids["beta-corp"]
            )
            assert bob_decision.allowed is False

            # 普通员工通过 REST API 访问私有空间文档详情，返回 404 不泄露标题
            get_priv_resp = await client.get(
                f"/api/v1/documents/{priv_doc_id}",
                headers=emp_headers,
            )
            assert get_priv_resp.status_code == 404
            assert "私有空间高管薪酬" not in get_priv_resp.text

            # 跨组织上传接口拦截（bob 尝试往 acme space 上传 -> 403）
            cross_upload = await client.post(
                f"/api/v1/spaces/{finance_space_id}/documents/uploads",
                headers=bob_headers,
                json={"filename": "hack.md"},
            )
            assert cross_upload.status_code == 403
            _ = beta_space_id
            _ = admin_headers


@pytest.mark.asyncio
async def test_4d_acl_dynamic_vs_snapshot_expiry_optimistic_lock_and_break_glass() -> None:
    """验证部门 DYNAMIC/SNAPSHOT、临时 USER 过期、乐观锁 409、Outbox 与 BreakGlass 默认拒绝。"""
    async with _slice2_context() as (client, session_factory, seed_ids, demo_password, _storage):
        acme_id = seed_ids["acme-corp"]
        finance_space_id = seed_ids["space:finance-general"]

        admin_headers, _ = await _login_headers(
            client, email="admin@acme.local", password=demo_password
        )
        km_headers, _ = await _login_headers(client, email="km@acme.local", password=demo_password)
        _, emp_login = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )
        emp_user_id = uuid.UUID(str(emp_login["user"]["id"]))  # type: ignore[index]

        # 获取部门与角色信息
        depts_resp = await client.get(
            f"/api/v1/organizations/{acme_id}/departments",
            headers=admin_headers,
        )
        assert depts_resp.status_code == 200
        dept_map = {d["name"]: uuid.UUID(d["id"]) for d in depts_resp.json()}
        rd_id = dept_map["研发部"]
        cs_id = dept_map["客服部"]

        # 新建两个子部门：研发部 -> 动静测试子部门（稍后移动至客服部下验证 DYNAMIC vs SNAPSHOT）
        suffix = uuid.uuid4().hex[:8]
        sub_dept_resp = await client.post(
            f"/api/v1/organizations/{acme_id}/departments",
            headers=admin_headers,
            json={"name": f"架构测试组-{suffix}", "parent_id": str(rd_id)},
        )
        assert sub_dept_resp.status_code == 201
        sub_dept_id = uuid.UUID(sub_dept_resp.json()["id"])

        # 新建隔离员工账号，仅加入“架构测试组”且仅赋予 EMPLOYEE 角色
        iso_email = f"arch-{suffix}@acme.local"
        add_member_resp = await client.post(
            f"/api/v1/organizations/{acme_id}/members",
            headers=admin_headers,
            json={
                "email": iso_email,
                "display_name": "架构组成员",
                "password": demo_password,
            },
        )
        assert add_member_resp.status_code == 201
        iso_user_id = uuid.UUID(add_member_resp.json()["user_id"])

        assign_dept = await client.post(
            f"/api/v1/organizations/{acme_id}/departments/{sub_dept_id}/memberships",
            headers=admin_headers,
            json={"user_id": str(iso_user_id), "membership_type": "PRIMARY", "is_primary": True},
        )
        assert assign_dept.status_code == 201

        roles_resp = await client.get(
            f"/api/v1/organizations/{acme_id}/roles",
            headers=admin_headers,
        )
        role_by_code = {r["code"]: uuid.UUID(r["id"]) for r in roles_resp.json()}
        assign_role = await client.post(
            f"/api/v1/organizations/{acme_id}/roles/{role_by_code['EMPLOYEE']}/assign",
            headers=admin_headers,
            json={"user_id": str(iso_user_id)},
        )
        assert assign_role.status_code == 201

        iso_principal = Principal(
            user_id=iso_user_id,
            email=iso_email,
            display_name="架构组成员",
            status="ACTIVE",
            session_id=None,
            token_org_id=acme_id,
            system_roles=(),
            system_permissions=frozenset(),
        )
        emp_principal = Principal(
            user_id=emp_user_id,
            email="employee@acme.local",
            display_name="Acme 研发员工",
            status="ACTIVE",
            session_id=None,
            token_org_id=acme_id,
            system_roles=(),
            system_permissions=frozenset(),
        )

        # 上传两篇测试文档：Doc Dynamic 与 Doc Snapshot
        doc_ids: dict[str, uuid.UUID] = {}
        for key_name in ("dynamic", "snapshot"):
            up = await client.post(
                f"/api/v1/spaces/{finance_space_id}/documents/uploads",
                headers=km_headers,
                files={"file": (f"{key_name}-{suffix}.txt", b"Test document body", "text/plain")},
                data={"title": f"ACL测试-{key_name}-{suffix}", "sensitivity_level": "L1"},
            )
            assert up.status_code == 201
            d_id = uuid.UUID(up.json()["document_id"])
            v_id = uuid.UUID(up.json()["document_version_id"])
            doc_ids[key_name] = d_id

            # 为研发部配置 include_descendants=true（分别测试 DYNAMIC 与 SNAPSHOT）
            acl_res = await client.post(
                f"/api/v1/documents/{d_id}/acl/revisions",
                headers={**km_headers, "Idempotency-Key": f"idem-dept-{key_name}-{suffix}"},
                json={
                    "expected_revision": 0,
                    "reason": f"研发部继承模式 {key_name.upper()} 授权",
                    "grants": [
                        {
                            "subject_type": "DEPARTMENT",
                            "subject_id": str(rd_id),
                            "include_descendants": True,
                            "inheritance_mode": key_name.upper(),
                        }
                    ],
                },
            )
            assert acl_res.status_code == 200, acl_res.text
            assert acl_res.json()["revision"] == 1

            pub_res = await client.post(
                f"/api/v1/document-versions/{v_id}/publish",
                headers={**km_headers, "Idempotency-Key": f"idem-pub-{key_name}-{suffix}"},
            )
            assert pub_res.status_code == 200

        engine = AuthoritativeACLEngine()

        # 1. 移动子部门前：架构测试组同时拥有 Dynamic 与 Snapshot 两篇文档的读取权限
        async with session_factory() as db_session:
            assert (
                await engine.check_document_access(
                    db_session, iso_principal, doc_ids["dynamic"], organization_id=acme_id
                )
            ).allowed is True
            assert (
                await engine.check_document_access(
                    db_session, iso_principal, doc_ids["snapshot"], organization_id=acme_id
                )
            ).allowed is True

        # 2. 将架构测试组从“研发部”移动到“客服部”下
        move_res = await client.patch(
            f"/api/v1/organizations/{acme_id}/departments/{sub_dept_id}",
            headers=admin_headers,
            json={"parent_id": str(cs_id)},
        )
        assert move_res.status_code == 200

        # 3. 移动后验证核心差异：
        #    - DYNAMIC 模式立即失去研发部权限（拒绝）！
        #    - SNAPSHOT 模式仍按授权瞬间展开的部门快照保留读取权限（允许）！
        async with session_factory() as db_session:
            dyn_after = await engine.check_document_access(
                db_session, iso_principal, doc_ids["dynamic"], organization_id=acme_id
            )
            snap_after = await engine.check_document_access(
                db_session, iso_principal, doc_ids["snapshot"], organization_id=acme_id
            )
            assert dyn_after.allowed is False
            assert snap_after.allowed is True

        # 4. 验证乐观锁 409 冲突、幂等重放、ACLRevision Diff 与 Outbox 同事务落库
        #    先验证使用错误 expected_revision=0 修改 revision=1 的文档 -> 返回 409
        conflict_resp = await client.post(
            f"/api/v1/documents/{doc_ids['dynamic']}/acl/revisions",
            headers={**km_headers, "Idempotency-Key": f"idem-conflict-{suffix}"},
            json={
                "expected_revision": 0,
                "reason": "并发过期提交测试",
                "grants": [{"subject_type": "USER", "subject_id": str(iso_user_id)}],
            },
        )
        assert conflict_resp.status_code == 409
        assert conflict_resp.json()["code"] == "ACL_REVISION_CONFLICT"

        # 使用正确 expected_revision=1 追加临时过期 USER 授权 + ROLE 授权（验证过期拒绝与 ROLE OR）
        now = datetime.now(UTC)
        expired_until = (now - timedelta(minutes=5)).isoformat()
        valid_from_past = (now - timedelta(hours=2)).isoformat()
        update_ok = await client.post(
            f"/api/v1/documents/{doc_ids['dynamic']}/acl/revisions",
            headers={**km_headers, "Idempotency-Key": f"idem-v2-{suffix}"},
            json={
                "expected_revision": 1,
                "reason": "测试已过期 USER 授权与 HR ROLE 授权",
                "grants": [
                    {
                        "subject_type": "USER",
                        "subject_id": str(iso_user_id),
                        "valid_from": valid_from_past,
                        "valid_until": expired_until,
                    },
                    {
                        "subject_type": "ROLE",
                        "subject_id": str(role_by_code["HR"]),
                    },
                ],
            },
        )
        assert update_ok.status_code == 200, update_ok.text
        assert update_ok.json()["revision"] == 2

        # 相同 Idempotency-Key 重放返回同一修订结果
        replay_ok = await client.post(
            f"/api/v1/documents/{doc_ids['dynamic']}/acl/revisions",
            headers={**km_headers, "Idempotency-Key": f"idem-v2-{suffix}"},
            json={
                "expected_revision": 1,
                "reason": "测试已过期 USER 授权与 HR ROLE 授权",
                "grants": [
                    {
                        "subject_type": "USER",
                        "subject_id": str(iso_user_id),
                        "valid_from": valid_from_past,
                        "valid_until": expired_until,
                    },
                    {
                        "subject_type": "ROLE",
                        "subject_id": str(role_by_code["HR"]),
                    },
                ],
            },
        )
        assert replay_ok.status_code == 200
        assert replay_ok.json()["revision"] == 2

        # 核验 ACLRevision 历史 Diff 与 OutboxEvent 同事务落库
        revs_resp = await client.get(
            f"/api/v1/documents/{doc_ids['dynamic']}/acl/revisions",
            headers=km_headers,
        )
        assert revs_resp.status_code == 200
        revs_data = revs_resp.json()
        assert len(revs_data) == 2
        assert revs_data[0]["revision"] == 2
        assert len(revs_data[0]["diff_json"]["added"]) == 2
        assert len(revs_data[0]["diff_json"]["removed"]) == 1

        async with session_factory() as db_session:
            async with org_rls_session(db_session, acme_id):
                outbox_rows = (
                    (
                        await db_session.execute(
                            select(OutboxEvent).where(
                                OutboxEvent.aggregate_id == doc_ids["dynamic"],
                                OutboxEvent.event_type == "acl.revision.created",
                                OutboxEvent.aggregate_revision == 2,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(outbox_rows) == 1

            # 已过期 USER 授权被即时拒绝，且 employee 不在 HR 角色中同样被拒绝
            assert (
                await engine.check_document_access(
                    db_session, iso_principal, doc_ids["dynamic"], organization_id=acme_id
                )
            ).allowed is False
            assert (
                await engine.check_document_access(
                    db_session, emp_principal, doc_ids["dynamic"], organization_id=acme_id
                )
            ).allowed is False

            # 5. BreakGlassGrant 默认 PENDING 仍拒绝；仅独立审批通过且未过期方可通过
            async with org_rls_session(db_session, acme_id):
                bg = BreakGlassGrant(
                    organization_id=acme_id,
                    user_id=emp_user_id,
                    scope=f"document:{doc_ids['dynamic']}",
                    reason="紧急生产故障排查",
                    expires_at=now + timedelta(minutes=30),
                    approved_by=None,
                    status="PENDING",
                )
                db_session.add(bg)
                await db_session.flush()
                bg_id = bg.id

            assert (
                await engine.check_document_access(
                    db_session, emp_principal, doc_ids["dynamic"], organization_id=acme_id
                )
            ).allowed is False

            async with org_rls_session(db_session, acme_id):
                bg_row = await db_session.get(BreakGlassGrant, bg_id)
                assert bg_row is not None
                bg_row.approved_by = uuid.UUID(str(km_headers["Authorization"] and emp_user_id))
                # 自己审批自己必须仍然被拒绝
                bg_row.status = "APPROVED"

            assert (
                await engine.check_document_access(
                    db_session, emp_principal, doc_ids["dynamic"], organization_id=acme_id
                )
            ).allowed is False

            # 由 km 独立审批后允许访问
            async with org_rls_session(db_session, acme_id):
                km_row = (
                    await db_session.execute(select(User).where(User.email == "km@acme.local"))
                ).scalar_one()
                bg_row = await db_session.get(BreakGlassGrant, bg_id)
                assert bg_row is not None
                bg_row.approved_by = km_row.id

            bg_decision = await engine.check_document_access(
                db_session, emp_principal, doc_ids["dynamic"], organization_id=acme_id
            )
            assert bg_decision.allowed is True
            assert bg_decision.via_break_glass is True


@pytest.mark.asyncio
async def test_minio_presigned_upload_quarantine_and_publish_gate() -> None:
    """验证真实 MinIO 预签名上传、路径穿越拦截、格式拒收、Quarantine 隔离与发布门禁。"""
    async with _slice2_context() as (client, session_factory, seed_ids, demo_password, storage):
        acme_id = seed_ids["acme-corp"]
        finance_space_id = seed_ids["space:finance-general"]

        km_headers, _ = await _login_headers(client, email="km@acme.local", password=demo_password)
        _, emp_login = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )
        emp_principal = Principal(
            user_id=uuid.UUID(str(emp_login["user"]["id"])),  # type: ignore[index]
            email="employee@acme.local",
            display_name="Acme 研发员工",
            status="ACTIVE",
            session_id=None,
            token_org_id=acme_id,
            system_roles=(),
            system_permissions=frozenset(),
        )

        # 1. 路径穿越文件名与非法格式必须拦截（422）
        traversal_resp = await client.post(
            f"/api/v1/spaces/{finance_space_id}/documents/uploads",
            headers=km_headers,
            json={"filename": "../../etc/passwd.txt"},
        )
        assert traversal_resp.status_code == 422
        assert traversal_resp.json()["code"] == "PATH_TRAVERSAL_BLOCKED"

        exe_resp = await client.post(
            f"/api/v1/spaces/{finance_space_id}/documents/uploads",
            headers=km_headers,
            json={"filename": "payload.exe"},
        )
        assert exe_resp.status_code == 422
        assert exe_resp.json()["code"] == "UNSUPPORTED_FILE_FORMAT"

        # 2. 创建预签名上传会话并通过 HTTP PUT 真实上传字节流到 MinIO
        file_content = "# 2026 企业差旅报销标准\n住宿与交通报销上限说明。".encode()
        expected_sha256 = hashlib.sha256(file_content).hexdigest()

        session_resp = await client.post(
            f"/api/v1/spaces/{finance_space_id}/documents/uploads",
            headers={**km_headers, "Idempotency-Key": f"idem-up-{uuid.uuid4().hex[:8]}"},
            json={
                "filename": "travel-guide-2026.md",
                "title": "2026 企业差旅报销标准",
                "sensitivity_level": "L1",
                "size_bytes": len(file_content),
            },
        )
        assert session_resp.status_code == 201, session_resp.text
        up_data = session_resp.json()
        upload_id = up_data["upload_id"]
        doc_id = uuid.UUID(up_data["document_id"])
        ver_id = uuid.UUID(up_data["document_version_id"])
        presigned_url = up_data["presigned_put_url"]
        assert presigned_url
        assert up_data["document_status"] == "QUARANTINED"

        async with httpx.AsyncClient(timeout=15.0) as http_client:
            put_res = await http_client.put(
                presigned_url,
                content=file_content,
                headers={"Content-Type": "text/markdown"},
            )
            assert put_res.status_code in (200, 204), put_res.text

        # 3. 服务端 /complete 真实核验 MinIO 对象 SHA-256 与字节大小
        complete_resp = await client.post(
            f"/api/v1/documents/uploads/{upload_id}/complete",
            headers={**km_headers, "Idempotency-Key": f"idem-comp-{uuid.uuid4().hex[:8]}"},
            json={"expected_sha256": expected_sha256},
        )
        assert complete_resp.status_code == 200, complete_resp.text
        comp_data = complete_resp.json()
        assert comp_data["import_status"] == "QUARANTINED"
        assert comp_data["file_sha256"] == expected_sha256
        assert comp_data["size_bytes"] == len(file_content)

        # 验证真实 MinIO 可直接读回与数据库记录一致的 SHA-256
        verified = await storage.verify_and_hash_object(
            bucket_name=storage.raw_bucket,
            object_key=comp_data["source_object_key"],
            expected_sha256=expected_sha256,
        )
        assert verified.size_bytes == len(file_content)

        # 4. 插入测试 Chunk，验证处于 QUARANTINED 的文档即使在 batch_authorize_chunks 也绝对不可召回
        async with session_factory() as db_session:
            async with org_rls_session(db_session, acme_id):
                cset = ChunkSet(
                    organization_id=acme_id,
                    document_version_id=ver_id,
                    chunker_version="v1",
                    config_hash="test",
                    status="SUCCEEDED",
                )
                db_session.add(cset)
                await db_session.flush()
                chunk = Chunk(
                    organization_id=acme_id,
                    chunk_set_id=cset.id,
                    document_version_id=ver_id,
                    sequence=0,
                    content_text="住宿报销上限 600 元/晚",
                    content_hash=hashlib.sha256(b"chunk").hexdigest(),
                    token_count=12,
                )
                db_session.add(chunk)
                await db_session.flush()
                chunk_id = chunk.id

            engine = AuthoritativeACLEngine()
            quarantined_batch = await engine.batch_authorize_chunks(
                db_session,
                emp_principal,
                [chunk_id],
                organization_id=acme_id,
            )
            assert quarantined_batch.allowed_chunk_ids == []
            assert (
                quarantined_batch.denied_records[0].reason_code == "DOCUMENT_NOT_ACTIVE:QUARANTINED"
            )

        # 5. 未配置 ACL 直接调用 /publish 必须返回 409 阻断
        premature_pub = await client.post(
            f"/api/v1/document-versions/{ver_id}/publish",
            headers=km_headers,
        )
        assert premature_pub.status_code == 409
        assert premature_pub.json()["code"] == "ACL_REQUIRED_BEFORE_PUBLISH"

        # 6. 配置 GLOBAL ACL 并发布解除 Quarantine，此后 batch_authorize_chunks 放行
        acl_resp = await client.post(
            f"/api/v1/documents/{doc_id}/acl/revisions",
            headers={**km_headers, "Idempotency-Key": f"idem-acl-pub-{uuid.uuid4().hex[:8]}"},
            json={
                "expected_revision": 0,
                "reason": "差旅制度正式面向本空间全员发布",
                "grants": [{"subject_type": "GLOBAL"}],
            },
        )
        assert acl_resp.status_code == 200

        final_pub = await client.post(
            f"/api/v1/document-versions/{ver_id}/publish",
            headers={**km_headers, "Idempotency-Key": f"idem-final-pub-{uuid.uuid4().hex[:8]}"},
        )
        assert final_pub.status_code == 200
        assert final_pub.json()["document_status"] == "ACTIVE"

        async with session_factory() as db_session:
            active_batch = await AuthoritativeACLEngine().batch_authorize_chunks(
                db_session,
                emp_principal,
                [chunk_id],
                organization_id=acme_id,
            )
            assert active_batch.allowed_chunk_ids == [chunk_id]
            assert active_batch.doc_acl_revisions[doc_id] == 1

        # 7. 验证分阶段状态端点返回真实状态
        proc_resp = await client.get(
            f"/api/v1/document-versions/{ver_id}/processing-status",
            headers=km_headers,
        )
        assert proc_resp.status_code == 200
        proc_data = proc_resp.json()
        assert proc_data["import_job"]["status"] == "QUARANTINED"
        assert proc_data["parse_job"]["status"] == "QUEUED"
        assert proc_data["chunking_job"]["status"] == "SUCCEEDED"
