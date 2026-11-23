"""Slice 8 运营分析、双审计分离、Provider 脱敏配置与 Benchmark 真实 PostgreSQL 17 集成测试。

覆盖范围：
1. `0003` 迁移新增全部 27 张业务租户表均开启 `ENABLE + FORCE ROW LEVEL SECURITY`，
   空 RLS 上下文直查全部返回 0 行（Fail-Closed）。
2. `/api/v1/audit/events` 与 `/api/v1/audit/security-events` 权限分离与 Cursor 分页：
   普通员工与 SystemAdmin 访问 `/security-events` 返回 403，安全审计员 `auditor@acme.local` 可读。
3. `/api/v1/providers/config` 读取与修改：响应严禁包含任何明文 API Key，普通用户禁止修改（403）。
4. `/api/v1/analytics/{overview,faq,knowledge-gaps}` 与 `/api/v1/benchmarks` 真实聚合与持久化，
   强制验证 `acl_violations == 0`、`cross_org_leaks == 0`、`unauthorized_prompt_chunks == 0`。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.seed import resolve_seed_password, seed_database
from app.db.session import get_db_session, org_rls_session
from app.main import create_app
from app.modules.audit.models import SecurityAuditEvent
from app.modules.faq.models import FAQ
from app.modules.knowledge_gaps.models import KnowledgeGap, QuestionEvent

SLICE4_8_TENANT_TABLES: tuple[str, ...] = (
    "faqs",
    "faq_versions",
    "faq_segments",
    "faq_source_refs",
    "faq_acls",
    "faq_reviews",
    "faq_publish_records",
    "faq_cache_indexes",
    "conversations",
    "conversation_spaces",
    "messages",
    "message_segments",
    "answer_citations",
    "retrieval_audits",
    "retrieval_candidates",
    "retrieval_decisions",
    "llm_usages",
    "message_feedbacks",
    "question_events",
    "question_clusters",
    "question_cluster_members",
    "faq_candidates",
    "knowledge_gaps",
    "knowledge_gap_tasks",
    "organization_provider_policies",
    "benchmark_runs",
    "benchmark_case_results",
)

_FAKE_LLM_SECRET = "sk-plaintext-llm-secret-should-never-leak-9988"
_FAKE_QWEN_SECRET = "sk-plaintext-qwen-secret-should-never-leak-7766"
_FAKE_MINERU_SECRET = "mineru-plaintext-token-never-expose-5544"


def _make_settings() -> Settings:
    """构造带测试密钥标识的真实数据库连接配置。"""
    base = get_settings()
    return base.model_copy(
        update={
            "app_env": "test",
            "app_debug": False,
            "otel_enabled": False,
            "cookie_secure": False,
            "llm_api_key": SecretStr(_FAKE_LLM_SECRET),
            "qwen_api_key": SecretStr(_FAKE_QWEN_SECRET),
            "mineru_api_key": SecretStr(_FAKE_MINERU_SECRET),
        }
    )


@asynccontextmanager
async def _governance_context() -> AsyncIterator[
    tuple[AsyncClient, async_sessionmaker[AsyncSession], dict[str, uuid.UUID], str]
]:
    """构建绑定当前事件循环的真实 PostgreSQL 测试上下文。"""
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

    app = create_app(settings=settings)
    app.dependency_overrides[get_db_session] = _override_db

    demo_password = resolve_seed_password(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, session_factory, seed_ids, demo_password
    finally:
        await engine.dispose()


async def _login_headers(client: AsyncClient, *, email: str, password: str) -> dict[str, str]:
    """登录并返回 Bearer 认证头。"""
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_0003_all_27_tables_force_rls_fail_closed() -> None:
    """验证 0003 迁移全部 27 张新表均强制启用 ENABLE + FORCE RLS 且零上下文返回 0 行。"""
    async with (
        _governance_context() as (_client, session_factory, _seed_ids, _pwd),
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

        for table_name in SLICE4_8_TENANT_TABLES:
            assert table_name in rls_map, f"缺少表 {table_name}"
            assert rls_map[table_name] == (True, True), f"表 {table_name} 未启用 FORCE RLS"
            count = (await session.execute(text(f"SELECT COUNT(*) FROM {table_name}"))).scalar_one()
            assert count == 0, f"表 {table_name} 在空上下文泄漏了 {count} 行"


@pytest.mark.asyncio
async def test_audit_separation_and_cursor_pagination() -> None:
    """验证业务审计与安全审计分离权限控制及 Cursor 分页工作正常。"""
    async with _governance_context() as (client, session_factory, seed_ids, demo_password):
        acme_id = seed_ids["acme-corp"]

        # 预先写入多条安全审计事件用于 Cursor 分页测试
        async with session_factory() as session, org_rls_session(session, acme_id):
            for idx in range(3):
                session.add(
                    SecurityAuditEvent(
                        organization_id=acme_id,
                        actor_user_id=None,
                        event_type=f"test.security.probe.{idx}",
                        severity="MEDIUM",
                        resource_type="probe",
                        resource_id=str(idx),
                        details_json={"reason": f"测试探针 {idx}"},
                    )
                )

        emp_headers = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )
        admin_headers = await _login_headers(
            client, email="admin@acme.local", password=demo_password
        )
        auditor_headers = await _login_headers(
            client, email="auditor@acme.local", password=demo_password
        )

        # 普通员工无权读任何审计
        assert (await client.get("/api/v1/audit/events", headers=emp_headers)).status_code == 403
        assert (
            await client.get("/api/v1/audit/security-events", headers=emp_headers)
        ).status_code == 403

        # SystemAdmin 可读业务审计，但严禁读取安全审计（职责分离）
        assert (await client.get("/api/v1/audit/events", headers=admin_headers)).status_code == 200
        assert (
            await client.get("/api/v1/audit/security-events", headers=admin_headers)
        ).status_code == 403

        # SecurityAuditor 可读取全部安全审计且 Cursor 分页连续不重复
        page1 = await client.get(
            "/api/v1/audit/security-events?limit=2",
            headers=auditor_headers,
        )
        assert page1.status_code == 200, page1.text
        p1_body = page1.json()
        assert len(p1_body["items"]) == 2
        assert p1_body["next_cursor"]

        page2 = await client.get(
            f"/api/v1/audit/security-events?limit=2&cursor={p1_body['next_cursor']}",
            headers=auditor_headers,
        )
        assert page2.status_code == 200
        p2_body = page2.json()
        assert len(p2_body["items"]) >= 1
        p1_ids = {item["id"] for item in p1_body["items"]}
        p2_ids = {item["id"] for item in p2_body["items"]}
        assert p1_ids.isdisjoint(p2_ids)


@pytest.mark.asyncio
async def test_providers_config_never_leaks_plaintext_keys() -> None:
    """验证 /api/v1/providers/config 绝不回显任何明文密钥，且权限校验严格。"""
    async with _governance_context() as (client, _sf, _seed_ids, demo_password):
        emp_headers = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )
        km_headers = await _login_headers(client, email="km@acme.local", password=demo_password)
        admin_headers = await _login_headers(
            client, email="admin@acme.local", password=demo_password
        )

        # 普通员工禁止读取和修改 Provider 配置
        assert (
            await client.get("/api/v1/providers/config", headers=emp_headers)
        ).status_code == 403
        assert (
            await client.patch(
                "/api/v1/providers/config",
                headers=emp_headers,
                json={"embedding_primary_provider": "LOCAL_BGE"},
            )
        ).status_code == 403

        # 知识库管理员仅可只读查看，不可篡改平台 Provider 开关
        km_get = await client.get("/api/v1/providers/config", headers=km_headers)
        assert km_get.status_code == 200
        assert (
            await client.patch(
                "/api/v1/providers/config",
                headers=km_headers,
                json={"local_ocr_enabled": True},
            )
        ).status_code == 403

        # SystemAdmin 读取与更新 Provider 配置，严禁泄露任何明文 Key
        admin_get = await client.get("/api/v1/providers/config", headers=admin_headers)
        assert admin_get.status_code == 200
        raw_text = admin_get.text
        assert _FAKE_LLM_SECRET not in raw_text
        assert _FAKE_QWEN_SECRET not in raw_text
        assert _FAKE_MINERU_SECRET not in raw_text

        body = admin_get.json()
        assert body["llm"]["has_api_key_configured"] is True
        assert body["llm"]["masked_key_hint"] == "****9988"
        assert body["parser"]["has_mineru_key_configured"] is True
        assert body["parser"]["masked_mineru_key_hint"] == "****5544"

        patch_resp = await client.patch(
            "/api/v1/providers/config",
            headers=admin_headers,
            json={
                "allow_l1_external_egress": False,
                "embedding_primary_provider": "LOCAL_BGE",
                "local_bge_enabled": True,
                "local_ocr_enabled": True,
            },
        )
        assert patch_resp.status_code == 200, patch_resp.text
        updated = patch_resp.json()
        assert updated["allow_l1_external_egress"] is False
        assert updated["embedding"]["primary_provider"] == "LOCAL_BGE"
        assert updated["embedding"]["local_bge_enabled"] is True
        assert updated["parser"]["local_ocr_enabled"] is True
        assert _FAKE_LLM_SECRET not in patch_resp.text
        assert _FAKE_QWEN_SECRET not in patch_resp.text
        assert _FAKE_MINERU_SECRET not in patch_resp.text


@pytest.mark.asyncio
async def test_analytics_aggregation_and_benchmarks_zero_violation_gate() -> None:
    """验证运营分析真实聚合及 Benchmark 触发落盘（ACL Violation == 0 硬门禁）。"""
    async with _governance_context() as (client, session_factory, seed_ids, demo_password):
        acme_id = seed_ids["acme-corp"]
        finance_space_id = seed_ids["space:finance-general"]

        # 写入测试问题事件、FAQ 与 KnowledgeGap 用于真实聚合校验
        async with session_factory() as session, org_rls_session(session, acme_id):
            session.add(
                QuestionEvent(
                    organization_id=acme_id,
                    space_id=finance_space_id,
                    user_hash="u_hash_1",
                    session_hash="s_hash_1",
                    normalized_text="差旅报销标准是多少",
                    sensitivity_level="L1",
                    outcome="FAQ_HIT",
                )
            )
            session.add(
                FAQ(
                    organization_id=acme_id,
                    space_id=finance_space_id,
                    lifecycle_state="ARCHIVED",
                    publication_state="PUBLISHED",
                    source_health_state="STALE",
                    hit_count=5,
                )
            )
            session.add(
                KnowledgeGap(
                    organization_id=acme_id,
                    space_id=finance_space_id,
                    title="海外差旅报销细则缺失",
                    status="DETECTED",
                    priority="HIGH",
                    question_count=3,
                    unique_user_count=2,
                )
            )

        km_headers = await _login_headers(client, email="km@acme.local", password=demo_password)
        emp_headers = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )

        # 普通员工无 analytics:read 权限 -> 403
        assert (
            await client.get("/api/v1/analytics/overview", headers=emp_headers)
        ).status_code == 403

        ov_resp = await client.get("/api/v1/analytics/overview", headers=km_headers)
        assert ov_resp.status_code == 200, ov_resp.text
        ov = ov_resp.json()
        assert ov["pv"] >= 1
        assert ov["authorized_spaces"] >= 3
        assert ov["open_gap_count"] >= 1

        faq_resp = await client.get("/api/v1/analytics/faq", headers=km_headers)
        assert faq_resp.status_code == 200
        assert faq_resp.json()["published_count"] >= 1
        assert faq_resp.json()["stale_count"] >= 1

        gap_resp = await client.get("/api/v1/analytics/knowledge-gaps", headers=km_headers)
        assert gap_resp.status_code == 200
        assert gap_resp.json()["total_gaps"] >= 1

        # 触发真实 Benchmark 运行与持久化
        bench_run = await client.post(
            "/api/v1/benchmarks",
            headers={**km_headers, "Idempotency-Key": f"idem-bench-{uuid.uuid4().hex[:8]}"},
            json={"suite_type": "RAG_ACL_REGRESSION"},
        )
        assert bench_run.status_code == 201, bench_run.text
        run_data = bench_run.json()
        assert run_data["status"] == "COMPLETED"
        assert run_data["acl_violations"] == 0
        assert run_data["cross_org_leaks"] == 0
        assert run_data["unauthorized_prompt_chunks"] == 0
        assert len(run_data["cases"]) == 3

        bench_list = await client.get("/api/v1/benchmarks", headers=km_headers)
        assert bench_list.status_code == 200
        assert any(item["id"] == run_data["id"] for item in bench_list.json())
