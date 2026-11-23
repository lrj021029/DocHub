"""Slice 4 & 5 场景 A 端到端真实集成测试（PG 17 + MinIO + Qdrant + SSE + 动态重鉴权）。

覆盖验证目标（PRD 场景 A & TECHNICAL_SPEC §6.8 / §9）：
1. 真实导入并索引《财务差旅报销标准》（Space 全局可读）与《高管薪酬与股权激励细则》（仅 HR 授权）。
2. 普通员工提问报销 -> 200 POST Fetch SSE 依次收到 `message_start` / `retrieval_started` /
   `retrieval_completed` / `citation` / `token` / `message_complete` 及反馈保存。
3. 普通员工提问高管薪酬 -> SSE 与正文零出现“高管薪酬”或任何受限内容，Reranker/LLM 收到 0 条
   未授权切片，且 `QuestionEvent.outcome == 'ACL_DENIED'`。
4. HR 员工提问高管薪酬 -> 正常获答并可打开 `GET /api/v1/citations/{id}`（含页码/摘录/ACL 版本）。
5. 权限撤销 + 历史重开 -> 撤销 HR 对薪酬文档权限后，重开 `GET /chat/conversations/{id}` 与
   `GET /citations/{id}` 即时脱敏隐藏（`redacted=True` / 403），底层审计不可变留存。
6. `is_splittable=False` 不可拆分回答任一来源撤权后整条隐藏；进入 LLM 前二次 ACL 复核与流式取消。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.db.seed import resolve_seed_password, seed_database
from app.db.session import get_db_session, org_rls_session
from app.main import create_app
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.chat import (
    CancellationRegistry,
    ChatRuntimeDeps,
    chat_router,
    get_chat_runtime_deps,
)
from app.modules.chat.models import (
    Message,
    MessageFeedback,
    MessageSegment,
    RetrievalAudit,
    RetrievalCandidate,
    RetrievalDecision,
)
from app.modules.citations import citations_router
from app.modules.citations.models import AnswerCitation
from app.modules.documents.storage import MinIOStorageService
from app.modules.embeddings import (
    ChineseSparseVectorizer,
    EmbeddingRouter,
    LocalBGEEmbeddingProvider,
    QwenEmbeddingProvider,
)
from app.modules.indexing import (
    DocumentIngestionPipeline,
    QdrantIndexStore,
    resolve_collection_name,
)
from app.modules.indexing.models import EmbeddingProfile, EmbeddingProvider
from app.modules.knowledge_gaps.models import QuestionEvent
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.providers import (
    OpenAICompatibleLLMProvider,
    QwenRerankerProvider,
    RerankerRouter,
)


def _make_settings() -> Settings:
    base = get_settings()
    return base.model_copy(
        update={
            "app_env": "test",
            "app_debug": False,
            "otel_enabled": False,
            "cookie_secure": False,
            "qwen_api_key": SecretStr("sk-test-qwen"),
            "llm_api_key": SecretStr("sk-test-llm"),
            "embedding_primary_provider": "qwen",
            "reranker_primary_provider": "qwen",
        }
    )


def _scenario_a_vector(text: str, dims: int = 64) -> list[float]:
    """根据业务关键词生成确定性 64 维归一化语义向量（无关主题近正交）。"""
    vec = [0.0] * dims
    vec[-1] = 0.01
    if any(k in text for k in ("报销", "差旅", "高铁", "发票", "财务")):
        vec[0] += 1.0
    if any(k in text for k in ("高管", "薪酬", "股权", "激励", "年薪", "发放", "周期")):
        vec[1] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    return [round(v / norm, 6) for v in vec]


def _parse_sse_frames(raw_text: str) -> list[tuple[str, dict[str, Any]]]:
    """解析服务端返回的标准 SSE 文本流为 `(event_name, data_dict)` 序列。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        ev_name = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if ev_name and data_lines:
            frames.append((ev_name, json.loads("\n".join(data_lines))))
    return frames


@asynccontextmanager
async def _scenario_a_env() -> AsyncIterator[
    tuple[
        Settings,
        AsyncClient,
        async_sessionmaker[AsyncSession],
        dict[str, uuid.UUID],
        str,
        MinIOStorageService,
        QdrantIndexStore,
        EmbeddingRouter,
        str,
        list[str],
        list[str],
        CancellationRegistry,
    ]
]:
    """构建直连真实 PG 17 + MinIO + Qdrant 的场景 A 全栈测试环境。"""
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

    vectorizer = ChineseSparseVectorizer()
    qdrant_store = QdrantIndexStore(settings=settings, sparse_vectorizer=vectorizer)
    collection_prefix = f"test_s45_{uuid.uuid4().hex[:8]}"

    reranker_seen_docs: list[str] = []
    llm_seen_prompts: list[str] = []

    def qwen_emb_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs = body["input"]
        dims = int(body.get("dimensions") or 64)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": _scenario_a_vector(txt, dims=dims)}
                    for i, txt in enumerate(inputs)
                ],
                "usage": {"total_tokens": len(inputs) * 10},
            },
        )

    def bge_emb_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs = body["inputs"]
        return httpx.Response(
            200,
            json=[_scenario_a_vector(txt, dims=64) for txt in inputs],
        )

    def qwen_rerank_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        docs: list[str] = body["input"]["documents"]
        reranker_seen_docs.extend(docs)
        results = [
            {"index": i, "relevance_score": round(0.95 - i * 0.05, 4)} for i in range(len(docs))
        ]
        return httpx.Response(200, json={"output": {"results": results}})

    def llm_stream_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        msgs = body.get("messages") or []
        prompt_joined = "\n".join(str(m.get("content") or "") for m in msgs)
        llm_seen_prompts.append(prompt_joined)

        # 仅依据 <untrusted_evidence> 中收到的授权证据生成模拟回答
        evidence_section = prompt_joined.split("</untrusted_evidence>")[0]
        if "高管薪酬" in evidence_section or "股权激励" in evidence_section:
            part1 = "依据《高管薪酬与股权激励细则》，高管年度绩效奖金按"
            part2 = "年度考核结果于次年 Q1 发放，股权激励分 4 年归属 [C1]。"
        else:
            part1 = "依据《财务差旅报销标准》，员工出差可乘坐高铁二等座，"
            part2 = "并需附电子发票于 15 日内提交财务审批 [C1]。"

        c1 = json.dumps(
            {
                "model": "gpt-4o-mini",
                "choices": [{"delta": {"content": part1}, "finish_reason": None}],
            },
            ensure_ascii=False,
        )
        c2 = json.dumps(
            {
                "model": "gpt-4o-mini",
                "choices": [{"delta": {"content": part2}, "finish_reason": "stop"}],
            },
            ensure_ascii=False,
        )
        c3 = json.dumps(
            {
                "model": "gpt-4o-mini",
                "choices": [],
                "usage": {"prompt_tokens": 88, "completion_tokens": 36, "total_tokens": 124},
            },
            ensure_ascii=False,
        )
        sse_bytes = f"data: {c1}\n\ndata: {c2}\n\ndata: {c3}\n\ndata: [DONE]\n\n".encode()
        return httpx.Response(
            200,
            content=sse_bytes,
            headers={"content-type": "text/event-stream"},
        )

    emb_router = EmbeddingRouter(
        settings=settings,
        qwen_provider=QwenEmbeddingProvider(
            settings=settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(qwen_emb_handler)),
        ),
        local_bge_provider=LocalBGEEmbeddingProvider(
            settings=settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(bge_emb_handler)),
            protocol="tei",
        ),
    )
    reranker_router = RerankerRouter(
        settings=settings,
        qwen_provider=QwenRerankerProvider(
            settings=settings,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(qwen_rerank_handler)),
        ),
    )
    llm_provider = OpenAICompatibleLLMProvider(
        settings=settings,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(llm_stream_handler)),
    )
    cancel_reg = CancellationRegistry()
    chat_deps = ChatRuntimeDeps(
        qdrant_store=qdrant_store,
        embedding_router=emb_router,
        reranker_router=reranker_router,
        llm_provider=llm_provider,
        acl_engine=AuthoritativeACLEngine(),
        cancellation_registry=cancel_reg,
    )

    app = create_app(settings=settings)
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(citations_router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_chat_runtime_deps] = lambda: chat_deps

    demo_password = resolve_seed_password(settings)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield (
                settings,
                client,
                session_factory,
                seed_ids,
                demo_password,
                storage,
                qdrant_store,
                emb_router,
                collection_prefix,
                reranker_seen_docs,
                llm_seen_prompts,
                cancel_reg,
            )
    finally:
        cols_resp = await qdrant_store._client.get_collections()  # noqa: SLF001
        for col in cols_resp.collections:
            if col.name.startswith(collection_prefix):
                await qdrant_store.delete_collection(col.name)
        await qdrant_store.close()
        await engine.dispose()


async def _login_headers(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


async def _setup_scenario_a_corpus(
    *,
    settings: Settings,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    acme_org_id: uuid.UUID,
    km_headers: dict[str, str],
    storage: MinIOStorageService,
    qdrant_store: QdrantIndexStore,
    emb_router: EmbeddingRouter,
    collection_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """创建隔离空间并完成《财务差旅报销标准》(GLOBAL) 与《高管薪酬》(仅 HR) 建档、发布与索引。"""
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as session, org_rls_session(session, acme_org_id):
        provider = (
            await session.execute(
                select(EmbeddingProvider).where(
                    EmbeddingProvider.organization_id == acme_org_id,
                    EmbeddingProvider.code == "qwen-official",
                )
            )
        ).scalar_one()
        profile = EmbeddingProfile(
            organization_id=acme_org_id,
            provider_id=provider.id,
            code=f"s45-qwen-64d-{suffix}",
            model_id="text-embedding-v3",
            model_version="v1",
            dimensions=64,
            language_config={"languages": ["zh-CN"]},
            status="ACTIVE",
        )
        session.add(profile)
        await session.flush()

        space = KnowledgeSpace(
            organization_id=acme_org_id,
            name=f"场景 A 综合制度空间 {suffix}",
            slug=f"scenario-a-{suffix}",
            visibility="PUBLIC",
            answer_policy="STRICT_EVIDENCE",
            default_embedding_profile_id=profile.id,
            status="ACTIVE",
            config_json={"org_allow_external_l1": True},
            config_version=1,
        )
        session.add(space)
        await session.flush()
        space_id = space.id
        _ = resolve_collection_name(profile, collection_prefix=collection_prefix)

    # 查出 HR 部门 ID 与 HR 角色 ID
    depts_resp = await client.get(
        f"/api/v1/organizations/{acme_org_id}/departments",
        headers=km_headers,
    )
    assert depts_resp.status_code == 200
    hr_dept_id = uuid.UUID(next(d["id"] for d in depts_resp.json() if d["name"] == "人力资源部"))

    roles_resp = await client.get(
        f"/api/v1/organizations/{acme_org_id}/roles",
        headers=km_headers,
    )
    assert roles_resp.status_code == 200
    hr_role_id = uuid.UUID(next(r["id"] for r in roles_resp.json() if r["code"] == "HR"))

    pipeline = DocumentIngestionPipeline(
        settings=settings,
        storage=storage,
        embedding_router=emb_router,
        qdrant_store=qdrant_store,
        collection_prefix=collection_prefix,
    )

    # 1) 文档 A：《财务差旅报销标准》（L0, GLOBAL）
    travel_md = (
        "# 财务差旅报销标准\n\n"
        "员工出差交通工具原则上选择高铁二等座或经济舱机票，"
        "需取得合规电子发票并于返回后 15 日内提交财务审批。\n"
    ).encode()
    up_travel = await client.post(
        f"/api/v1/spaces/{space_id}/documents/uploads",
        headers=km_headers,
        files={"file": ("travel_policy.md", travel_md, "text/markdown")},
        data={"title": "《财务差旅报销标准》", "sensitivity_level": "L0"},
    )
    assert up_travel.status_code == 201, up_travel.text
    travel_doc_id = uuid.UUID(up_travel.json()["document_id"])
    travel_ver_id = uuid.UUID(up_travel.json()["document_version_id"])

    acl_travel = await client.post(
        f"/api/v1/documents/{travel_doc_id}/acl/revisions",
        headers={**km_headers, "Idempotency-Key": f"acl-travel-{suffix}"},
        json={
            "expected_revision": 0,
            "reason": "差旅报销标准向空间全体成员公开",
            "grants": [{"subject_type": "GLOBAL", "inheritance_mode": "DYNAMIC"}],
        },
    )
    assert acl_travel.status_code == 200, acl_travel.text

    pub_travel = await client.post(
        f"/api/v1/document-versions/{travel_ver_id}/publish",
        headers={**km_headers, "Idempotency-Key": f"pub-travel-{suffix}"},
    )
    assert pub_travel.status_code == 200, pub_travel.text

    async with session_factory() as session:
        res_travel = await pipeline.process_document_version(
            session,
            organization_id=acme_org_id,
            document_version_id=travel_ver_id,
        )
    assert res_travel.status == "SUCCEEDED"

    # 2) 文档 B：《高管薪酬与股权激励细则》（L1, 仅人力资源部 + HR 角色）
    salary_md = (
        "# 高管薪酬与股权激励细则\n\n"
        "高管年度绩效奖金依据年度考核结果于次年 Q1 发放，"
        "核心高管股权激励计划分 4 年匀速归属，严禁向非 HR 授权人员披露。\n"
    ).encode()
    up_salary = await client.post(
        f"/api/v1/spaces/{space_id}/documents/uploads",
        headers=km_headers,
        files={"file": ("exec_comp.md", salary_md, "text/markdown")},
        data={"title": "《高管薪酬与股权激励细则》", "sensitivity_level": "L1"},
    )
    assert up_salary.status_code == 201, up_salary.text
    salary_doc_id = uuid.UUID(up_salary.json()["document_id"])
    salary_ver_id = uuid.UUID(up_salary.json()["document_version_id"])

    acl_salary = await client.post(
        f"/api/v1/documents/{salary_doc_id}/acl/revisions",
        headers={**km_headers, "Idempotency-Key": f"acl-salary-{suffix}"},
        json={
            "expected_revision": 0,
            "reason": "高管薪酬仅授权人力资源部与 HR 角色查阅",
            "grants": [
                {
                    "subject_type": "DEPARTMENT",
                    "subject_id": str(hr_dept_id),
                    "include_descendants": False,
                    "inheritance_mode": "DYNAMIC",
                },
                {
                    "subject_type": "ROLE",
                    "subject_id": str(hr_role_id),
                    "include_descendants": False,
                    "inheritance_mode": "DYNAMIC",
                },
            ],
        },
    )
    assert acl_salary.status_code == 200, acl_salary.text

    pub_salary = await client.post(
        f"/api/v1/document-versions/{salary_ver_id}/publish",
        headers={**km_headers, "Idempotency-Key": f"pub-salary-{suffix}"},
    )
    assert pub_salary.status_code == 200, pub_salary.text

    async with session_factory() as session:
        res_salary = await pipeline.process_document_version(
            session,
            organization_id=acme_org_id,
            document_version_id=salary_ver_id,
        )
    assert res_salary.status == "SUCCEEDED"

    return space_id, travel_doc_id, salary_doc_id, hr_dept_id, hr_role_id


# ==============================================================================
# 1. PRD 场景 A 全链路测试：授权问答 + 越权零泄漏 + 引用重鉴权 + 撤权历史脱敏
# ==============================================================================


@pytest.mark.asyncio
async def test_scenario_a_acl_rag_sse_citations_and_history_reauth() -> None:
    """完整跑通 PRD 场景 A：授权 SSE、越权拦截零泄漏、引用重鉴权、撤权历史即时脱敏。"""
    async with _scenario_a_env() as (
        settings,
        client,
        session_factory,
        seed_ids,
        demo_password,
        storage,
        qdrant_store,
        emb_router,
        collection_prefix,
        reranker_seen_docs,
        llm_seen_prompts,
        _cancel_reg,
    ):
        acme_org_id = seed_ids["acme-corp"]
        km_headers, km_login = await _login_headers(
            client, email="km@acme.local", password=demo_password
        )
        emp_headers, _ = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )
        hr_headers, _ = await _login_headers(client, email="hr@acme.local", password=demo_password)
        km_user_id = str(km_login["user"]["id"])

        space_id, _travel_doc_id, salary_doc_id, _, _ = await _setup_scenario_a_corpus(
            settings=settings,
            client=client,
            session_factory=session_factory,
            acme_org_id=acme_org_id,
            km_headers=km_headers,
            storage=storage,
            qdrant_store=qdrant_store,
            emb_router=emb_router,
            collection_prefix=collection_prefix,
        )

        # ------------------------------------------------------------------
        # A1. 普通员工创建会话并提问报销 -> 200 SSE 完整事件流 + 引用 + 反馈
        # ------------------------------------------------------------------
        emp_conv_resp = await client.post(
            "/api/v1/chat/conversations",
            headers=emp_headers,
            json={"title": "员工差旅咨询", "space_ids": [str(space_id)]},
        )
        assert emp_conv_resp.status_code == 201, emp_conv_resp.text
        emp_conv_id = emp_conv_resp.json()["id"]

        # 验证 Cursor 分页查询会话列表
        conv_list_resp = await client.get("/api/v1/chat/conversations", headers=emp_headers)
        assert conv_list_resp.status_code == 200
        assert any(item["id"] == emp_conv_id for item in conv_list_resp.json()["items"])

        travel_sse = await client.post(
            f"/api/v1/chat/conversations/{emp_conv_id}/messages:stream",
            headers=emp_headers,
            json={"query": "出差报销高铁和发票有什么规定？"},
        )
        assert travel_sse.status_code == 200
        frames_a1 = _parse_sse_frames(travel_sse.text)
        event_names_a1 = [name for name, _ in frames_a1]
        assert event_names_a1[0] == "message_start"
        assert "retrieval_started" in event_names_a1
        assert "retrieval_completed" in event_names_a1
        assert "citation" in event_names_a1
        assert "token" in event_names_a1
        assert event_names_a1[-1] == "message_complete"

        emp_msg_id = next(d["message_id"] for n, d in frames_a1 if n == "message_start")
        cit_events = [d for n, d in frames_a1 if n == "citation"]
        assert cit_events[0]["document_title"] == "《财务差旅报销标准》"
        tokens_text = "".join(d["text"] for n, d in frames_a1 if n == "token")
        assert "高铁二等座" in tokens_text

        fb_resp = await client.post(
            f"/api/v1/chat/messages/{emp_msg_id}/feedback",
            headers=emp_headers,
            json={"rating": "up", "comment": "引用准确"},
        )
        assert fb_resp.status_code == 200
        assert fb_resp.json()["rating"] == "HELPFUL"

        # ------------------------------------------------------------------
        # A2. 普通员工提问高管薪酬 -> 零出现受限内容，0 进 Reranker/LLM，QuestionEvent=ACL_DENIED
        # ------------------------------------------------------------------
        reranker_count_before = len(reranker_seen_docs)
        llm_count_before = len(llm_seen_prompts)

        denied_sse = await client.post(
            f"/api/v1/chat/conversations/{emp_conv_id}/messages:stream",
            headers=emp_headers,
            json={"query": "高管薪酬与股权激励细则怎么发放？"},
        )
        assert denied_sse.status_code == 200
        raw_sse_text = denied_sse.text
        assert "高管薪酬" not in raw_sse_text
        assert "股权激励" not in raw_sse_text
        assert "4 年匀速归属" not in raw_sse_text

        frames_a2 = _parse_sse_frames(raw_sse_text)
        assert not any(n == "citation" for n, _ in frames_a2)
        rc_a2 = next(d for n, d in frames_a2 if n == "retrieval_completed")
        assert rc_a2["authorized_count"] == 0
        # 确认未授权切片 0 进入 Reranker 与 LLM
        assert len(reranker_seen_docs) == reranker_count_before
        assert len(llm_seen_prompts) == llm_count_before

        denied_msg_id = uuid.UUID(
            next(d["message_id"] for n, d in frames_a2 if n == "message_start")
        )
        async with session_factory() as session, org_rls_session(session, acme_org_id):
            qe = (
                await session.execute(
                    select(QuestionEvent)
                    .where(QuestionEvent.organization_id == acme_org_id)
                    .order_by(QuestionEvent.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            assert qe.outcome == "ACL_DENIED"

            ra = (
                await session.execute(
                    select(RetrievalAudit).where(
                        RetrievalAudit.organization_id == acme_org_id,
                        RetrievalAudit.message_id == denied_msg_id,
                    )
                )
            ).scalar_one()
            assert ra.authorized_count == 0
            assert ra.denied_count >= 1
            r_decs = (
                (
                    await session.execute(
                        select(RetrievalDecision).where(
                            RetrievalDecision.organization_id == acme_org_id,
                            RetrievalDecision.audit_id == ra.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert r_decs and all(d.decision == "DENY" for d in r_decs)

        # ------------------------------------------------------------------
        # A3. HR 员工提问高管薪酬（含多轮追问）-> 正常获答与打开引用，随后撤权并重开历史
        # ------------------------------------------------------------------
        hr_conv_resp = await client.post(
            "/api/v1/chat/conversations",
            headers=hr_headers,
            json={"title": "HR 薪酬方案核查", "space_ids": [str(space_id)]},
        )
        assert hr_conv_resp.status_code == 201
        hr_conv_id = hr_conv_resp.json()["id"]

        hr_sse = await client.post(
            f"/api/v1/chat/conversations/{hr_conv_id}/messages:stream",
            headers=hr_headers,
            json={"query": "高管薪酬与股权激励细则的奖金和归属期是多少？"},
        )
        assert hr_sse.status_code == 200
        frames_hr = _parse_sse_frames(hr_sse.text)
        hr_cits = [d for n, d in frames_hr if n == "citation"]
        assert len(hr_cits) >= 1
        salary_cit_id = hr_cits[0]["citation_id"]
        assert hr_cits[0]["document_title"] == "《高管薪酬与股权激励细则》"
        hr_answer_text = "".join(d["text"] for n, d in frames_hr if n == "token")
        assert "次年 Q1 发放" in hr_answer_text

        # 多轮历史追问（Query Rewrite 不覆盖原文，且每轮重新计算当前权限）
        hr_followup = await client.post(
            f"/api/v1/chat/conversations/{hr_conv_id}/messages:stream",
            headers=hr_headers,
            json={"query": "它的发放周期是多久？"},
        )
        assert hr_followup.status_code == 200
        assert "次年 Q1 发放" in "".join(
            d["text"] for n, d in _parse_sse_frames(hr_followup.text) if n == "token"
        )

        # HR 点击引用卡片实时重鉴权 -> 200 OK
        cit_detail_ok = await client.get(
            f"/api/v1/citations/{salary_cit_id}",
            headers=hr_headers,
        )
        assert cit_detail_ok.status_code == 200, cit_detail_ok.text
        cit_body = cit_detail_ok.json()
        assert cit_body["authorized"] is True
        assert cit_body["document_title"] == "《高管薪酬与股权激励细则》"
        assert cit_body["acl_revision"] == 1
        assert "股权激励计划分 4 年" in cit_body["quote_excerpt"]

        # 普通员工尝试横向访问 HR 的引用 ID -> 403 零泄露
        cit_cross = await client.get(
            f"/api/v1/citations/{salary_cit_id}",
            headers=emp_headers,
        )
        assert cit_cross.status_code == 403
        assert "高管薪酬" not in cit_cross.text

        # --- 撤销 HR 对薪酬文档的访问权限（仅保留 km_user_id） ---
        revoke_resp = await client.post(
            f"/api/v1/documents/{salary_doc_id}/acl/revisions",
            headers={**km_headers, "Idempotency-Key": f"revoke-hr-{uuid.uuid4().hex[:8]}"},
            json={
                "expected_revision": 1,
                "reason": "审计整改：收回 HR 专员对高管薪酬文档的阅读权限",
                "grants": [
                    {
                        "subject_type": "USER",
                        "subject_id": km_user_id,
                        "inheritance_mode": "DYNAMIC",
                    }
                ],
            },
        )
        assert revoke_resp.status_code == 200, revoke_resp.text
        assert revoke_resp.json()["revision"] == 2

        # 撤权后 HR 重新打开历史会话 -> 段落级即时脱敏（redacted=True）+ 引用隐藏
        reopen_conv = await client.get(
            f"/api/v1/chat/conversations/{hr_conv_id}",
            headers=hr_headers,
        )
        assert reopen_conv.status_code == 200, reopen_conv.text
        reopen_json = reopen_conv.json()
        assistant_msgs = [m for m in reopen_json["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 2
        for a_msg in assistant_msgs:
            assert a_msg["redacted"] is True
            assert a_msg["citations"] == []
            assert "次年 Q1 发放" not in a_msg["content"]
            assert a_msg["segments"][0]["redacted"] is True
            assert "脱敏隐藏" in a_msg["segments"][0]["content"]

        # 撤权后 HR 再次点击原引用 -> 403 Forbidden（绝不泄露标题或摘录）
        cit_revoked = await client.get(
            f"/api/v1/citations/{salary_cit_id}",
            headers=hr_headers,
        )
        assert cit_revoked.status_code == 403
        assert "高管薪酬" not in cit_revoked.text
        assert "4 年匀速归属" not in cit_revoked.text

        # 验证数据库底层审计与原始 AnswerCitation / MessageSegment 仍完整不可变留存
        async with session_factory() as session, org_rls_session(session, acme_org_id):
            cit_row = await session.get(AnswerCitation, uuid.UUID(salary_cit_id))
            assert cit_row is not None
            assert cit_row.acl_revision == 1
            seg_rows = (
                (
                    await session.execute(
                        select(MessageSegment).where(
                            MessageSegment.organization_id == acme_org_id,
                            MessageSegment.id == cit_row.message_segment_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(seg_rows) == 1
            assert "次年 Q1 发放" in seg_rows[0].content


# ==============================================================================
# 2. 不可拆分敏感回答（is_splittable=False）整条隐藏 + 取消 + 反馈持久验证
# ==============================================================================


@pytest.mark.asyncio
async def test_non_splittable_message_redaction_and_cancel() -> None:
    """验证 is_splittable=False 消息在任一来源撤权后整条隐藏，以及流式取消接口。"""
    async with _scenario_a_env() as (
        settings,
        client,
        session_factory,
        seed_ids,
        demo_password,
        storage,
        qdrant_store,
        emb_router,
        collection_prefix,
        _reranker_seen,
        _llm_seen,
        cancel_reg,
    ):
        acme_org_id = seed_ids["acme-corp"]
        km_headers, km_login = await _login_headers(
            client, email="km@acme.local", password=demo_password
        )
        hr_headers, _ = await _login_headers(client, email="hr@acme.local", password=demo_password)
        km_user_id = str(km_login["user"]["id"])

        space_id, travel_doc_id, salary_doc_id, _, _ = await _setup_scenario_a_corpus(
            settings=settings,
            client=client,
            session_factory=session_factory,
            acme_org_id=acme_org_id,
            km_headers=km_headers,
            storage=storage,
            qdrant_store=qdrant_store,
            emb_router=emb_router,
            collection_prefix=collection_prefix,
        )

        # HR 创建会话并以 is_splittable=False 发起混合问题（同时召回报销 L0 + 高管薪酬 L1）
        conv_resp = await client.post(
            "/api/v1/chat/conversations",
            headers=hr_headers,
            json={"title": "综合敏感问答", "space_ids": [str(space_id)]},
        )
        assert conv_resp.status_code == 201
        conv_id = conv_resp.json()["id"]

        mixed_sse = await client.post(
            f"/api/v1/chat/conversations/{conv_id}/messages:stream",
            headers=hr_headers,
            json={
                "query": "差旅报销标准与高管薪酬激励制度分别是怎样的？",
                "is_splittable": False,
            },
        )
        assert mixed_sse.status_code == 200
        frames = _parse_sse_frames(mixed_sse.text)
        msg_id = uuid.UUID(next(d["message_id"] for n, d in frames if n == "message_start"))

        # 确保 MessageSegment 同时绑定了 travel_doc_id（仍有效）与 salary_doc_id（将被撤权）
        async with session_factory() as session, org_rls_session(session, acme_org_id):
            seg = (
                await session.execute(
                    select(MessageSegment).where(
                        MessageSegment.organization_id == acme_org_id,
                        MessageSegment.message_id == msg_id,
                    )
                )
            ).scalar_one()
            seg.source_document_ids = [str(travel_doc_id), str(salary_doc_id)]

        # 撤销 HR 对 salary_doc_id 的访问权（travel_doc_id 仍 GLOBAL 可读）
        revoke_resp = await client.post(
            f"/api/v1/documents/{salary_doc_id}/acl/revisions",
            headers={**km_headers, "Idempotency-Key": f"revoke-nonsplit-{uuid.uuid4().hex[:8]}"},
            json={
                "expected_revision": 1,
                "reason": "撤权测试不可拆分回答整体隐藏",
                "grants": [{"subject_type": "USER", "subject_id": km_user_id}],
            },
        )
        assert revoke_resp.status_code == 200

        # 重开会话：因为 is_splittable=False 且任一来源撤权，整条消息必须整体脱敏隐藏！
        reopen = await client.get(f"/api/v1/chat/conversations/{conv_id}", headers=hr_headers)
        assert reopen.status_code == 200
        a_msg = next(m for m in reopen.json()["messages"] if m["role"] == "assistant")
        assert a_msg["is_splittable"] is False
        assert a_msg["redacted"] is True
        assert a_msg["citations"] == []
        assert "无法按段落安全拆分" in a_msg["content"]
        assert "次年 Q1 发放" not in a_msg["content"]
        assert "高铁二等座" not in a_msg["content"]

        # 测试取消接口（模拟正在 STREAMING 的消息被立即标记 CANCELLED）
        async with session_factory() as session, org_rls_session(session, acme_org_id):
            streaming_msg = Message(
                organization_id=acme_org_id,
                conversation_id=uuid.UUID(conv_id),
                role="ASSISTANT",
                content="",
                status="STREAMING",
            )
            session.add(streaming_msg)
            await session.flush()
            streaming_msg_id = streaming_msg.id

        cancel_resp = await client.post(
            f"/api/v1/chat/messages/{streaming_msg_id}/cancel",
            headers=hr_headers,
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["cancelled"] is True
        assert cancel_resp.json()["status"] == "CANCELLED"
        assert cancel_reg.is_cancelled(streaming_msg_id) is True

        # 验证反馈持久化查询
        fb_resp = await client.post(
            f"/api/v1/chat/messages/{msg_id}/feedback",
            headers=hr_headers,
            json={"rating": "down", "comment": "需要更细粒度段落拆分"},
        )
        assert fb_resp.status_code == 200
        assert fb_resp.json()["rating"] == "UNHELPFUL"

        async with session_factory() as session, org_rls_session(session, acme_org_id):
            fb_row = (
                await session.execute(
                    select(MessageFeedback).where(
                        MessageFeedback.organization_id == acme_org_id,
                        MessageFeedback.message_id == msg_id,
                    )
                )
            ).scalar_one()
            assert fb_row.rating == "UNHELPFUL"
            assert fb_row.comment == "需要更细粒度段落拆分"

        # 验证 RetrievalCandidate 未保存任何文本列
        assert not hasattr(RetrievalCandidate, "content_text")
        assert not hasattr(RetrievalCandidate, "text")
