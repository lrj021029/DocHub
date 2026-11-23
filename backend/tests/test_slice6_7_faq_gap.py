"""Slice 6 & 7 真实 PostgreSQL 17 + Redis 7 端到端集成测试（Scenario B 完整闭环）。

验证范围：
1. FAQ 挖掘脱敏与聚类：PII 强脱敏（手机号/身份证/邮箱/工号）、`sklearn.cluster.HDBSCAN`
   聚类与严格按 Space 隔离。
2. FAQ 三轴解耦、禁自审与 Redis Fast Path 缓存：
   - 创建人自审触发 403 (`SELF_REVIEW_FORBIDDEN`) 并写入 `SecurityAuditEvent`；
   - 同事审核通过并发布 -> Redis Fast Path 命中 (`from_cache=False` -> `from_cache=True`)；
   - 来源变更调用 `mark_faqs_stale_for_document` -> 仅置 `source_health_state="STALE"`、
     保留 `publication_state="PUBLISHED"`、同步驱逐 Redis 缓存、写入 `faq.source.stale` +
     `faq.cache.invalidate` Outbox，且 Fast Path 立即停止命中；
   - L2/L3 敏感 FAQ 绝不写入 Redis（仅 PostgreSQL 回源命中）。
3. 多来源最严交集 ACL：引用 HR + Finance 文档的 FAQ，仅持 HR 权限者被拒，双权限者放行。
4. Knowledge Gap 准入分类器与回归验证门禁：
   - `ACL_DENIED` 与 `LLM_PROVIDER_FAILURE` 绝不进 `knowledge_gaps`；
   - 「海外直邮保税仓清关延误」缺口 -> 创建任务 -> 未关联生效文档禁止 `RESOLVED` (409) ->
     补录发布有效切片文档后通过回归验证 -> `RESOLVED` 并写入 `knowledge_gap.resolved` Outbox。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.seed import resolve_seed_password, seed_database
from app.db.session import get_db_session, org_rls_session
from app.main import create_app
from app.modules.acl.models import ACLGrant, DocumentACL
from app.modules.audit.models import SecurityAuditEvent
from app.modules.documents.models import Chunk, ChunkSet, Document, DocumentVersion
from app.modules.faq.models import FAQ
from app.modules.faq.router import faq_router
from app.modules.faq.service import authorize_faq_for_principal, mark_faqs_stale_for_document
from app.modules.identity.deps import Principal
from app.modules.identity.models import User
from app.modules.knowledge_gaps.models import (
    FAQCandidate,
    KnowledgeGap,
    QuestionCluster,
    QuestionEvent,
)
from app.modules.knowledge_gaps.router import gaps_router
from app.modules.knowledge_spaces.models import KnowledgeSpace, SpaceMembership
from app.modules.organizations.models import OrganizationMember
from app.modules.outbox.models import OutboxEvent
from app.modules.roles.models import OrganizationRole, UserRoleMembership


def _make_settings() -> Settings:
    """构造连接本地真实 PostgreSQL 17 与 Redis 7 的测试配置。"""
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
async def _scenario_b_context() -> AsyncIterator[
    tuple[
        AsyncClient,
        async_sessionmaker[AsyncSession],
        dict[str, uuid.UUID],
        str,
        aioredis.Redis,
    ]
]:
    """初始化挂载 faq_router / gaps_router 的真实测试应用与 Redis 客户端。"""
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
    app.include_router(faq_router, prefix="/api/v1")
    app.include_router(gaps_router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = _override_db

    demo_password = resolve_seed_password(settings)
    redis_client = aioredis.from_url(
        settings.redis_cache_url.get_secret_value(),
        decode_responses=True,
    )
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, session_factory, seed_ids, demo_password, redis_client
    finally:
        await redis_client.aclose()
        await engine.dispose()


async def _login_headers(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> dict[str, str]:
    """登录演示账号并返回 Bearer 请求头。"""
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create_org_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    display_name: str,
    password: str,
    org_role_codes: tuple[str, ...],
    space_ids: tuple[uuid.UUID, ...] = (),
) -> User:
    """在指定组织内创建一个真实成员并授予指定的 OrganizationRole。"""
    async with session.begin():
        user = User(
            email=email,
            display_name=display_name,
            password_hash=hash_password(password),
            status="ACTIVE",
        )
        session.add(user)
        await session.flush()

    async with org_rls_session(session, org_id, user_id=user.id):
        session.add(
            OrganizationMember(
                organization_id=org_id,
                user_id=user.id,
                status="ACTIVE",
            )
        )
        roles = (
            (
                await session.execute(
                    select(OrganizationRole).where(
                        OrganizationRole.organization_id == org_id,
                        OrganizationRole.code.in_(org_role_codes),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(roles) == len(org_role_codes), f"Missing roles: {org_role_codes}"
        for role in roles:
            session.add(
                UserRoleMembership(
                    organization_id=org_id,
                    user_id=user.id,
                    organization_role_id=role.id,
                    status="ACTIVE",
                )
            )
        for sp_id in space_ids:
            session.add(
                SpaceMembership(
                    organization_id=org_id,
                    space_id=sp_id,
                    user_id=user.id,
                    role="MEMBER",
                    status="ACTIVE",
                )
            )
    return user


async def _create_active_doc_with_acl(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    space_id: uuid.UUID,
    creator_id: uuid.UUID,
    title: str,
    chunk_text: str,
    sensitivity_level: str = "L1",
    status: str = "ACTIVE",
    grant_global: bool = True,
    grant_role_codes: tuple[str, ...] = (),
    grant_user_ids: tuple[uuid.UUID, ...] = (),
) -> tuple[Document, DocumentVersion, Chunk]:
    """在 org_rls_session 内插入一份带可选切片与四维 ACL 的真实文档。"""
    async with org_rls_session(session, org_id, user_id=creator_id):
        doc = Document(
            organization_id=org_id,
            space_id=space_id,
            title=title,
            source_type="UPLOAD",
            status=status,
            sensitivity_level=sensitivity_level,
            created_by=creator_id,
        )
        session.add(doc)
        await session.flush()

        ver = DocumentVersion(
            organization_id=org_id,
            document_id=doc.id,
            version_no=1,
            filename=f"{title}.md",
            source_object_key=f"test/{org_id}/{doc.id}/v1.md",
            mime_type="text/markdown",
            size_bytes=len(chunk_text.encode("utf-8")),
            created_by=creator_id,
        )
        session.add(ver)
        await session.flush()
        if status == "ACTIVE":
            doc.current_version_id = ver.id

        chunk_set = ChunkSet(
            organization_id=org_id,
            document_version_id=ver.id,
            chunker_version="test-v1",
            config_hash="cfg-test",
            status="SUCCEEDED",
        )
        session.add(chunk_set)
        await session.flush()

        chunk = Chunk(
            organization_id=org_id,
            chunk_set_id=chunk_set.id,
            document_version_id=ver.id,
            sequence=0,
            content_text=chunk_text,
            content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
            token_count=len(chunk_text),
        )
        session.add(chunk)

        acl = DocumentACL(
            organization_id=org_id,
            document_id=doc.id,
            revision=1,
            status="ACTIVE",
        )
        session.add(acl)
        await session.flush()

        if grant_global:
            session.add(
                ACLGrant(
                    organization_id=org_id,
                    acl_id=acl.id,
                    subject_type="GLOBAL",
                    subject_id=None,
                    created_by=creator_id,
                )
            )
        if grant_role_codes:
            roles = (
                (
                    await session.execute(
                        select(OrganizationRole).where(
                            OrganizationRole.organization_id == org_id,
                            OrganizationRole.code.in_(grant_role_codes),
                        )
                    )
                )
                .scalars()
                .all()
            )
            for role in roles:
                session.add(
                    ACLGrant(
                        organization_id=org_id,
                        acl_id=acl.id,
                        subject_type="ROLE",
                        subject_id=role.id,
                        created_by=creator_id,
                    )
                )
        for uid in grant_user_ids:
            session.add(
                ACLGrant(
                    organization_id=org_id,
                    acl_id=acl.id,
                    subject_type="USER",
                    subject_id=uid,
                    created_by=creator_id,
                )
            )
        await session.flush()
    return doc, ver, chunk


@pytest.mark.asyncio
async def test_faq_mining_pii_redaction_and_space_isolation() -> None:
    """验证 PII 强脱敏、sklearn HDBSCAN 聚类以及严格按 Space 隔离绝不混入私有空间。"""
    async with _scenario_b_context() as (client, session_factory, seed_ids, pwd, _redis):
        org_id = seed_ids["acme-corp"]
        support_space_id = seed_ids["space:customer-support"]
        hr_space_id = seed_ids["space:hr-executive"]
        suffix = uuid.uuid4().hex[:8]

        km_headers = await _login_headers(client, email="km@acme.local", password=pwd)

        async with session_factory() as session:
            async with session.begin():
                km_user = (
                    await session.execute(select(User).where(User.email == "km@acme.local"))
                ).scalar_one()

            async with org_rls_session(session, org_id, user_id=km_user.id):
                mining_space = KnowledgeSpace(
                    organization_id=org_id,
                    name=f"MiningSpace-{suffix}",
                    slug=f"mining-space-{suffix}",
                    visibility="PUBLIC",
                    answer_policy="STRICT_EVIDENCE",
                    status="ACTIVE",
                )
                session.add(mining_space)
                await session.flush()
            support_space_id = mining_space.id

            await _create_active_doc_with_acl(
                session,
                org_id=org_id,
                space_id=support_space_id,
                creator_id=km_user.id,
                title=f"生鲜售后规范-{suffix}",
                chunk_text="生鲜食品破损可在签收后24小时内提交破损照片申请全额退款，无需寄回原商品。",
                grant_global=True,
            )

            # 注入包含真实 PII 的高频问题（来自 3 个不同用户）与 1 条私有 HR 空间问题
            from app.modules.faq.mining import record_question_event

            pii_questions = [
                (
                    "生鲜食品破损退款规则是什么？联系 13800138000 或 "
                    f"test_{suffix}@acme.local ({suffix})"
                ),
                f"生鲜食品破损怎么申请退款？身份证 110101199001011234 ({suffix})",
                f"生鲜食品破损退款流程有哪些？工号 EMP-889922 ({suffix})",
                f"生鲜食品破损退款规则及时效？ ({suffix})",
                f"生鲜食品破损退款需要什么凭证？ ({suffix})",
            ]
            async with org_rls_session(session, org_id, user_id=km_user.id):
                for idx, q_text in enumerate(pii_questions):
                    await record_question_event(
                        session,
                        organization_id=org_id,
                        space_id=support_space_id,
                        user_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"u-{suffix}-{idx % 3}"),
                        session_id=f"sess-{suffix}-{idx}",
                        department_id=None,
                        raw_question=q_text,
                        sensitivity_level="L1",
                        outcome="ANSWERED",
                    )
                # 另一私有空间的同类问题绝不能混入 support_space 的聚类簇
                await record_question_event(
                    session,
                    organization_id=org_id,
                    space_id=hr_space_id,
                    user_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"hr-u-{suffix}"),
                    session_id=f"hr-sess-{suffix}",
                    department_id=None,
                    raw_question=f"HR高管机密生鲜补贴发放标准 ({suffix})",
                    sensitivity_level="L1",
                    outcome="ANSWERED",
                )

        run_resp = await client.post(
            "/api/v1/faq-mining/runs",
            headers=km_headers,
            json={"space_id": str(support_space_id), "window_days": 7, "min_cluster_size": 3},
        )
        assert run_resp.status_code == 201, run_resp.text
        run_data = run_resp.json()
        assert run_data["processed_events"] >= 5
        assert run_data["clusters_created"] >= 1
        assert run_data["candidates_created"] >= 1

        # 校验数据库：无明文 PII 留存，且 HR 私有空间问题未跨入 support_space 聚类簇
        async with session_factory() as check_session, org_rls_session(check_session, org_id):
            events = (
                (
                    await check_session.execute(
                        select(QuestionEvent).where(QuestionEvent.normalized_text.contains(suffix))
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) == 6
            for ev in events:
                assert "13800138000" not in ev.normalized_text
                assert "110101199001011234" not in ev.normalized_text
                assert f"test_{suffix}@acme.local" not in ev.normalized_text
                assert "EMP-889922" not in ev.normalized_text

            clusters = (
                (
                    await check_session.execute(
                        select(QuestionCluster).where(
                            QuestionCluster.space_id == support_space_id,
                            QuestionCluster.representative_question.contains(suffix),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(clusters) >= 1
            for cl in clusters:
                assert "HR高管机密" not in cl.representative_question
                assert "13800138000" not in cl.representative_question

            candidates = (
                (
                    await check_session.execute(
                        select(FAQCandidate).where(
                            FAQCandidate.space_id == support_space_id,
                            FAQCandidate.generated_question.contains(suffix),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(candidates) >= 1
            for cand in candidates:
                assert "HR高管机密" not in cand.generated_question


@pytest.mark.asyncio
async def test_faq_3axis_anti_self_review_redis_fast_path_and_stale() -> None:
    """验证禁自审 403、同事审核发布、Redis Fast Path 命中、STALE 失活驱逐与停用闭环。"""
    async with _scenario_b_context() as (client, session_factory, seed_ids, pwd, redis_client):
        org_id = seed_ids["acme-corp"]
        support_space_id = seed_ids["space:customer-support"]
        suffix = uuid.uuid4().hex[:8]
        reviewer_email = f"km_peer_{suffix}@acme.local"

        async with session_factory() as session:
            async with session.begin():
                km_user = (
                    await session.execute(select(User).where(User.email == "km@acme.local"))
                ).scalar_one()

            async with org_rls_session(session, org_id, user_id=km_user.id):
                faq_space = KnowledgeSpace(
                    organization_id=org_id,
                    name=f"FaqFastPath-{suffix}",
                    slug=f"faq-fastpath-{suffix}",
                    visibility="PUBLIC",
                    answer_policy="STRICT_EVIDENCE",
                    status="ACTIVE",
                )
                session.add(faq_space)
                await session.flush()
            support_space_id = faq_space.id

            peer_reviewer = await _create_org_user(
                session,
                org_id=org_id,
                email=reviewer_email,
                display_name="Peer Reviewer",
                password=pwd,
                org_role_codes=("KNOWLEDGE_MANAGER",),
                space_ids=(support_space_id,),
            )
            doc, ver, _chunk = await _create_active_doc_with_acl(
                session,
                org_id=org_id,
                space_id=support_space_id,
                creator_id=km_user.id,
                title=f"退货政策文档-{suffix}",
                chunk_text="7天无理由退货需保持商品吊牌完整且不影响二次销售。",
                grant_global=True,
            )

        km_headers = await _login_headers(client, email="km@acme.local", password=pwd)
        peer_headers = await _login_headers(client, email=reviewer_email, password=pwd)
        emp_headers = await _login_headers(client, email="employee@acme.local", password=pwd)

        faq_question = f"七天无理由退货需要满足什么条件 {suffix}"
        create_resp = await client.post(
            "/api/v1/faq",
            headers=km_headers,
            json={
                "space_id": str(support_space_id),
                "question": faq_question,
                "answer": "7天无理由退货需保持商品吊牌完整且不影响二次销售。",
                "fact_basis": "依据最新售后 SOP 第 3 章第 2 条",
                "maintainer": peer_reviewer.display_name,
                "expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
                "source_refs": [
                    {
                        "document_id": str(doc.id),
                        "document_version_id": str(ver.id),
                    }
                ],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        faq_id = uuid.UUID(create_resp.json()["id"])
        assert create_resp.json()["lifecycle_state"] == "PENDING_REVIEW"
        assert create_resp.json()["publication_state"] == "UNPUBLISHED"

        # 1. 创建者自审必须被拦截返回 403 且写安全审计
        self_review_resp = await client.post(
            f"/api/v1/faq/{faq_id}/reviews",
            headers=km_headers,
            json={"decision": "APPROVE", "comments": "尝试自审通过"},
        )
        assert self_review_resp.status_code == 403, self_review_resp.text
        assert self_review_resp.json()["code"] == "SELF_REVIEW_FORBIDDEN"

        async with session_factory() as audit_sess, org_rls_session(audit_sess, org_id):
            sec_events = (
                (
                    await audit_sess.execute(
                        select(SecurityAuditEvent).where(
                            SecurityAuditEvent.event_type == "faq.review.self_review_blocked",
                            SecurityAuditEvent.resource_id == str(faq_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(sec_events) >= 1

        # 2. 非创建者同事审核通过并发布
        peer_review_resp = await client.post(
            f"/api/v1/faq/{faq_id}/reviews",
            headers=peer_headers,
            json={"decision": "APPROVE", "comments": "来源事实核验一致"},
        )
        assert peer_review_resp.status_code == 200, peer_review_resp.text
        assert peer_review_resp.json()["lifecycle_state"] == "ARCHIVED"

        pub_resp = await client.post(f"/api/v1/faq/{faq_id}/publish", headers=km_headers)
        assert pub_resp.status_code == 200, pub_resp.text
        assert pub_resp.json()["publication_state"] == "PUBLISHED"
        assert pub_resp.json()["source_health_state"] == "HEALTHY"

        # 3. 首次 Fast Path 命中写 Redis，第二次命中 Redis 缓存
        match1 = await client.post(
            "/api/v1/faq/match",
            headers=emp_headers,
            json={"space_ids": [str(support_space_id)], "question": faq_question},
        )
        assert match1.status_code == 200, match1.text
        m1 = match1.json()
        assert m1["hit"] is True
        assert m1["from_cache"] is False
        assert m1["faq_id"] == str(faq_id)

        redis_keys_before = await redis_client.keys(f"dochub:faq:{org_id}:*:{faq_id}:*")
        assert len(redis_keys_before) == 1

        match2 = await client.post(
            "/api/v1/faq/match",
            headers=emp_headers,
            json={"space_ids": [str(support_space_id)], "question": faq_question},
        )
        assert match2.status_code == 200, match2.text
        assert match2.json()["hit"] is True
        assert match2.json()["from_cache"] is True

        # 4. 来源文档发生变更 -> 置 source_health_state=STALE，保留 PUBLISHED 并驱逐 Redis
        async with session_factory() as stale_sess:
            stale_ids = await mark_faqs_stale_for_document(
                stale_sess,
                organization_id=org_id,
                document_id=doc.id,
                reason="SOURCE_DOCUMENT_UPDATED",
            )
            assert faq_id in stale_ids

        async with session_factory() as verify_sess, org_rls_session(verify_sess, org_id):
            faq_row = (await verify_sess.execute(select(FAQ).where(FAQ.id == faq_id))).scalar_one()
            assert faq_row.source_health_state == "STALE"
            assert faq_row.publication_state == "PUBLISHED"

            outbox_types = set(
                (
                    await verify_sess.execute(
                        select(OutboxEvent.event_type).where(OutboxEvent.aggregate_id == faq_id)
                    )
                )
                .scalars()
                .all()
            )
            assert "faq.source.stale" in outbox_types
            assert "faq.cache.invalidate" in outbox_types

        redis_keys_after = await redis_client.keys(f"dochub:faq:{org_id}:*:{faq_id}:*")
        assert redis_keys_after == []

        match_after_stale = await client.post(
            "/api/v1/faq/match",
            headers=emp_headers,
            json={"space_ids": [str(support_space_id)], "question": faq_question},
        )
        assert match_after_stale.status_code == 200
        assert match_after_stale.json()["hit"] is False

        # 5. 停用 FAQ 验证
        disable_resp = await client.post(f"/api/v1/faq/{faq_id}/disable", headers=km_headers)
        assert disable_resp.status_code == 200, disable_resp.text
        assert disable_resp.json()["publication_state"] == "DISABLED"


@pytest.mark.asyncio
async def test_faq_multi_source_intersection_acl_and_l2_never_cached() -> None:
    """验证多来源最严交集 ACL（缺一不可）以及 L2 高敏 FAQ 严禁写入 Redis。"""
    async with _scenario_b_context() as (client, session_factory, seed_ids, pwd, redis_client):
        org_id = seed_ids["acme-corp"]
        finance_space_id = seed_ids["space:finance-general"]
        suffix = uuid.uuid4().hex[:8]
        reviewer_email = f"km_acl_{suffix}@acme.local"
        dual_email = f"dual_hr_fin_{suffix}@acme.local"

        async with session_factory() as session:
            async with session.begin():
                km_user = (
                    await session.execute(select(User).where(User.email == "km@acme.local"))
                ).scalar_one()
                hr_user = (
                    await session.execute(select(User).where(User.email == "hr@acme.local"))
                ).scalar_one()

            await _create_org_user(
                session,
                org_id=org_id,
                email=reviewer_email,
                display_name="ACL Peer Reviewer",
                password=pwd,
                org_role_codes=("KNOWLEDGE_MANAGER",),
            )
            dual_user = await _create_org_user(
                session,
                org_id=org_id,
                email=dual_email,
                display_name="HR And Finance User",
                password=pwd,
                org_role_codes=("HR",),
            )

            # 文档 A：仅允许 HR 角色访问（hr@acme.local 与 dual_user 均可访问）
            doc_hr, v1, _c1 = await _create_active_doc_with_acl(
                session,
                org_id=org_id,
                space_id=finance_space_id,
                creator_id=km_user.id,
                title=f"HR补偿标准-{suffix}",
                chunk_text="高管离职补偿按司龄 N+2 核算。",
                sensitivity_level="L1",
                grant_global=False,
                grant_role_codes=("HR",),
            )
            # 文档 B：L2 敏感且仅允许 dual_user 与 km_user（禁止 hr@acme.local 访问）
            doc_fin, v2, _c2 = await _create_active_doc_with_acl(
                session,
                org_id=org_id,
                space_id=finance_space_id,
                creator_id=km_user.id,
                title=f"财务拨付上限-{suffix}",
                chunk_text="高管补偿财务拨付需 CFO 联签。",
                sensitivity_level="L2",
                grant_global=False,
                grant_user_ids=(dual_user.id, km_user.id),
            )

        km_headers = await _login_headers(client, email="km@acme.local", password=pwd)
        peer_headers = await _login_headers(client, email=reviewer_email, password=pwd)
        hr_headers = await _login_headers(client, email="hr@acme.local", password=pwd)
        dual_headers = await _login_headers(client, email=dual_email, password=pwd)

        faq_q = f"高管离职补偿与财务联签流程 {suffix}"
        create_resp = await client.post(
            "/api/v1/faq",
            headers=km_headers,
            json={
                "space_id": str(finance_space_id),
                "question": faq_q,
                "answer": "按 HR N+2 标准核算后由 CFO 财务联签拨付。",
                "fact_basis": "联合 HR 与财务双来源制度",
                "maintainer": km_user.display_name,
                "expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
                "source_refs": [
                    {
                        "document_id": str(doc_hr.id),
                        "document_version_id": str(v1.id),
                    },
                    {
                        "document_id": str(doc_fin.id),
                        "document_version_id": str(v2.id),
                    },
                ],
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        faq_id = uuid.UUID(create_resp.json()["id"])

        assert (
            await client.post(
                f"/api/v1/faq/{faq_id}/reviews",
                headers=peer_headers,
                json={"decision": "APPROVE"},
            )
        ).status_code == 200
        assert (
            await client.post(f"/api/v1/faq/{faq_id}/publish", headers=km_headers)
        ).status_code == 200

        # HR-only 用户仅能看 doc_hr 但无权看 doc_fin -> 多来源最严交集必须拒绝
        hr_principal = Principal(
            user_id=hr_user.id,
            email=hr_user.email,
            display_name=hr_user.display_name,
            status=hr_user.status,
            session_id=None,
            token_org_id=org_id,
            system_roles=(),
            system_permissions=frozenset(),
        )
        dual_principal = Principal(
            user_id=dual_user.id,
            email=dual_user.email,
            display_name=dual_user.display_name,
            status=dual_user.status,
            session_id=None,
            token_org_id=org_id,
            system_roles=(),
            system_permissions=frozenset(),
        )
        async with session_factory() as auth_sess:
            hr_dec = await authorize_faq_for_principal(
                auth_sess,
                principal=hr_principal,
                organization_id=org_id,
                faq_id=faq_id,
            )
            assert hr_dec.allowed is False
            dual_dec = await authorize_faq_for_principal(
                auth_sess,
                principal=dual_principal,
                organization_id=org_id,
                faq_id=faq_id,
            )
            assert dual_dec.allowed is True
            assert dual_dec.max_sensitivity_level == "L2"

        hr_match = await client.post(
            "/api/v1/faq/match",
            headers=hr_headers,
            json={"space_ids": [str(finance_space_id)], "question": faq_q},
        )
        assert hr_match.status_code == 200
        assert hr_match.json()["hit"] is False

        # 同时持有双来源权限的用户可命中，且因为最高敏感等级为 L2，绝不写入 Redis 缓存
        dual_match = await client.post(
            "/api/v1/faq/match",
            headers=dual_headers,
            json={"space_ids": [str(finance_space_id)], "question": faq_q},
        )
        assert dual_match.status_code == 200, dual_match.text
        assert dual_match.json()["hit"] is True
        assert dual_match.json()["from_cache"] is False

        l2_redis_keys = await redis_client.keys(f"dochub:faq:{org_id}:*:{faq_id}:*")
        assert l2_redis_keys == []


@pytest.mark.asyncio
async def test_knowledge_gap_admission_and_regression_closed_loop() -> None:
    """验证 Gap 准入分类器隔离 ACL/故障事件，并验证未通过回归严禁关闭(409)直至补全文档通过。"""
    async with _scenario_b_context() as (client, session_factory, seed_ids, pwd, _redis):
        org_id = seed_ids["acme-corp"]
        support_space_id = seed_ids["space:customer-support"]
        suffix = uuid.uuid4().hex[:8]
        km_headers = await _login_headers(client, email="km@acme.local", password=pwd)

        # 1. ACL_DENIED 必须路由至安全审计，绝不进入 knowledge_gaps
        acl_sig = await client.post(
            "/api/v1/knowledge-gaps/signals",
            headers=km_headers,
            json={
                "space_id": str(support_space_id),
                "question_text": f"越权查询高管期权明细 {suffix}",
                "outcome": "ACL_DENIED",
            },
        )
        assert acl_sig.status_code == 201, acl_sig.text
        assert acl_sig.json()["admitted"] is False
        assert acl_sig.json()["action"] == "ROUTED_TO_SECURITY_AUDIT"

        # 2. LLM_PROVIDER_FAILURE 必须跳过，绝不进入 knowledge_gaps
        llm_sig = await client.post(
            "/api/v1/knowledge-gaps/signals",
            headers=km_headers,
            json={
                "space_id": str(support_space_id),
                "question_text": f"模型超时导致无回复 {suffix}",
                "outcome": "LLM_PROVIDER_FAILURE",
            },
        )
        assert llm_sig.status_code == 201, llm_sig.text
        assert llm_sig.json()["admitted"] is False
        assert llm_sig.json()["action"] == "SKIPPED_OPERATIONAL_FAILURE"

        # 3. 真实知识缺口 NO_RETRIEVAL 准入（且脱敏手机号）
        gap_question = f"海外直邮保税仓清关延误如何赔付 {suffix} 联系 13911112222"
        gap_sig = await client.post(
            "/api/v1/knowledge-gaps/signals",
            headers=km_headers,
            json={
                "space_id": str(support_space_id),
                "question_text": gap_question,
                "outcome": "NO_RETRIEVAL",
                "priority": "HIGH",
            },
        )
        assert gap_sig.status_code == 201, gap_sig.text
        assert gap_sig.json()["admitted"] is True
        gap_id = uuid.UUID(gap_sig.json()["gap_id"])
        assert "13911112222" not in gap_sig.json()["redacted_text"]

        # 验证前两类伪缺口未写入 knowledge_gaps，且 ACL_DENIED 写入了 SecurityAuditEvent
        async with session_factory() as check_sess, org_rls_session(check_sess, org_id):
            bad_gaps = (
                (
                    await check_sess.execute(
                        select(KnowledgeGap).where(
                            KnowledgeGap.organization_id == org_id,
                            KnowledgeGap.title.contains(suffix),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [g.id for g in bad_gaps] == [gap_id]

            sec_ev = (
                (
                    await check_sess.execute(
                        select(SecurityAuditEvent).where(
                            SecurityAuditEvent.event_type == "GAP_SIGNAL_REJECTED_ACL_DENIED"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(sec_ev) >= 1

        # 4. 指派整改任务
        async with session_factory() as s_user, s_user.begin():
            km_user = (
                await s_user.execute(select(User).where(User.email == "km@acme.local"))
            ).scalar_one()

        task_resp = await client.post(
            f"/api/v1/knowledge-gaps/{gap_id}/tasks",
            headers=km_headers,
            json={"assignee_id": str(km_user.id), "priority": "HIGH"},
        )
        assert task_resp.status_code == 201, task_resp.text
        task_id = uuid.UUID(task_resp.json()["id"])

        # 5. 试图跳过回归直接将缺口设为 RESOLVED -> 必须被拒绝 (409)
        direct_res = await client.patch(
            f"/api/v1/knowledge-gaps/{gap_id}/status",
            headers=km_headers,
            json={"status": "RESOLVED"},
        )
        assert direct_res.status_code == 409

        # 6. 任务无关联有效文档试图设为 RESOLVED -> 409
        no_doc_res = await client.patch(
            f"/api/v1/knowledge-gap-tasks/{task_id}",
            headers=km_headers,
            json={"status": "RESOLVED"},
        )
        assert no_doc_res.status_code == 409

        # 7. 关联尚未发布（QUARANTINED）的文档试图 RESOLVED -> 409
        async with session_factory() as doc_sess:
            q_doc, _qv, _qc = await _create_active_doc_with_acl(
                doc_sess,
                org_id=org_id,
                space_id=support_space_id,
                creator_id=km_user.id,
                title=f"隔离态清关草稿-{suffix}",
                chunk_text="海外直邮保税仓清关延误赔付草稿。",
                status="QUARANTINED",
            )
            active_doc, _av, _ac = await _create_active_doc_with_acl(
                doc_sess,
                org_id=org_id,
                space_id=support_space_id,
                creator_id=km_user.id,
                title=f"海外直邮保税仓清关延误赔付正式规范-{suffix}",
                chunk_text=(
                    "海外直邮保税仓清关延误超过7个工作日时，客服核实运单后赔付30元无门槛运费券"
                    f"（编号 {suffix}）。"
                ),
                status="ACTIVE",
                grant_global=True,
            )

        quarantined_res = await client.patch(
            f"/api/v1/knowledge-gap-tasks/{task_id}",
            headers=km_headers,
            json={
                "status": "RESOLVED",
                "related_document_id": str(q_doc.id),
                "run_regression": True,
            },
        )
        assert quarantined_res.status_code == 409

        # 8. 关联已发布生效的规范文档并触发回归验证 -> 200, regression_passed=True, gap=RESOLVED
        passed_res = await client.patch(
            f"/api/v1/knowledge-gap-tasks/{task_id}",
            headers=km_headers,
            json={
                "status": "RESOLVED",
                "related_document_id": str(active_doc.id),
                "run_regression": True,
            },
        )
        assert passed_res.status_code == 200, passed_res.text
        res_data = passed_res.json()
        assert res_data["status"] == "RESOLVED"
        assert res_data["regression_passed"] is True

        list_resp = await client.get(
            f"/api/v1/knowledge-gaps?space_id={support_space_id}&status=RESOLVED",
            headers=km_headers,
        )
        assert list_resp.status_code == 200, list_resp.text
        matched_items = [it for it in list_resp.json()["items"] if it["id"] == str(gap_id)]
        assert len(matched_items) == 1
        assert matched_items[0]["status"] == "RESOLVED"
        assert matched_items[0]["tasks"][0]["regression_passed"] is True

        async with session_factory() as ev_sess, org_rls_session(ev_sess, org_id):
            gap_outbox = (
                (
                    await ev_sess.execute(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_id == gap_id,
                            OutboxEvent.event_type == "knowledge_gap.resolved",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(gap_outbox) == 1
