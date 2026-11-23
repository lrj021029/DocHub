"""Qdrant 混合检索、多 Space 鉴权编排器与 Celery 拓扑真实集成测试。

全部向量存储与检索测试均直连本机真实运行中的 `dochub-qdrant`（`127.0.0.1:6333`），
使用带随机后缀的独立测试 Collection 并在测试结束后彻底清理。
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import ValidationError
from app.modules.embeddings import (
    ChineseSparseVectorizer,
    EmbeddingProfile,
    EmbeddingRouter,
    QwenEmbeddingProvider,
)
from app.modules.indexing import (
    ACLPrefilter,
    ACLProjectionPayload,
    QdrantChunkPoint,
    QdrantIndexStore,
)
from app.modules.providers import (
    AnswerPolicyMode,
    QwenRerankerProvider,
    RerankerRouter,
    SensitivityLevel,
)
from app.modules.retrieval import (
    AuthorizedChunkBody,
    ChunkAuthorizationResult,
    ContextBuilder,
    MultiSpaceHybridRetriever,
    SpaceRetrievalTarget,
)
from app.workers import (
    ALL_CELERY_QUEUES,
    CELERY_BEAT_SCHEDULE,
    CELERY_TASK_ROUTES,
    celery_app,
    ping_worker,
)


def _live_qdrant_settings() -> Settings:
    """构造连接本机真实运行中 Qdrant 的测试配置。"""
    return Settings(
        qdrant_host="127.0.0.1",
        qdrant_port=6333,
        qdrant_api_key=SecretStr("dochub_qdrant_dev_key"),
        qdrant_https=False,
    )


def _unique_collection(prefix: str) -> str:
    return f"test_{prefix}_{uuid.uuid4().hex[:10]}"


# ==============================================================================
# 1. Qdrant 真实写入、混合检索 RRF 召回与租户/空间/代次硬隔离测试
# ==============================================================================


@pytest.mark.asyncio
async def test_qdrant_hybrid_search_and_tenant_space_generation_isolation() -> None:
    """直连真实 Qdrant 验证共享 Collection 创建、Dense+Sparse RRF 召回与跨租户 0 泄漏。"""
    collection = _unique_collection("hybrid_iso")
    vectorizer = ChineseSparseVectorizer()

    async with QdrantIndexStore(
        settings=_live_qdrant_settings(),
        sparse_vectorizer=vectorizer,
    ) as store:
        try:
            created = await store.ensure_collection(collection, dense_dimensions=4)
            assert created is True
            # 再次调用应幂等返回 False
            assert await store.ensure_collection(collection, dense_dimensions=4) is False

            points = [
                # 目标命中的合法文档（Org-A / Space-1 / Gen-V1）
                QdrantChunkPoint(
                    chunk_id="chunk-a1-target",
                    organization_id="org-a",
                    space_id="space-1",
                    document_id="doc-a1",
                    document_version_id="ver-a1",
                    parent_chunk_id="parent-a1",
                    index_generation_id="gen-v1",
                    acl_revision=3,
                    dense_vector=[0.95, 0.05, 0.0, 0.0],
                    text_for_sparse="多租户知识库 PostgreSQL 权威 ACL 权限校验与混合检索架构",
                    acl_projection=ACLProjectionPayload(acl_global=True),
                ),
                # 同空间次相关文档（Org-A / Space-1 / Gen-V1）
                QdrantChunkPoint(
                    chunk_id="chunk-a1-other",
                    organization_id="org-a",
                    space_id="space-1",
                    document_id="doc-a2",
                    document_version_id="ver-a2",
                    index_generation_id="gen-v1",
                    acl_revision=1,
                    dense_vector=[0.1, 0.9, 0.0, 0.0],
                    text_for_sparse="企业员工差旅报销流程与发票抬头规范",
                    acl_projection=ACLProjectionPayload(acl_global=True),
                ),
                # 跨组织干扰项（Org-B / 同 Space-1 / 同 Gen-V1，向量完全一致但必须 0 召回）
                QdrantChunkPoint(
                    chunk_id="chunk-b1-cross-org",
                    organization_id="org-b",
                    space_id="space-1",
                    document_id="doc-b1",
                    document_version_id="ver-b1",
                    index_generation_id="gen-v1",
                    acl_revision=1,
                    dense_vector=[1.0, 0.0, 0.0, 0.0],
                    text_for_sparse="多租户知识库 PostgreSQL 权威 ACL 权限校验",
                    acl_projection=ACLProjectionPayload(acl_global=True),
                ),
                # 跨空间干扰项（Org-A / Space-2 / Gen-V1）
                QdrantChunkPoint(
                    chunk_id="chunk-a2-cross-space",
                    organization_id="org-a",
                    space_id="space-2",
                    document_id="doc-a3",
                    document_version_id="ver-a3",
                    index_generation_id="gen-v1",
                    acl_revision=1,
                    dense_vector=[1.0, 0.0, 0.0, 0.0],
                    text_for_sparse="多租户知识库 PostgreSQL 权威 ACL 权限校验",
                    acl_projection=ACLProjectionPayload(acl_global=True),
                ),
                # 跨代次干扰项（Org-A / Space-1 / Gen-V2）
                QdrantChunkPoint(
                    chunk_id="chunk-a1-other-gen",
                    organization_id="org-a",
                    space_id="space-1",
                    document_id="doc-a4",
                    document_version_id="ver-a4",
                    index_generation_id="gen-v2",
                    acl_revision=1,
                    dense_vector=[1.0, 0.0, 0.0, 0.0],
                    text_for_sparse="多租户知识库 PostgreSQL 权威 ACL 权限校验",
                    acl_projection=ACLProjectionPayload(acl_global=True),
                ),
            ]
            written = await store.upsert_chunks(collection, points)
            assert written == 5

            q_sparse = vectorizer.encode_query("PostgreSQL 权威 ACL 权限校验")
            hits = await store.search_hybrid(
                collection,
                organization_id="org-a",
                space_id="space-1",
                index_generation_id="gen-v1",
                query_dense=[1.0, 0.0, 0.0, 0.0],
                query_sparse=q_sparse,
                top_k=10,
            )

            hit_ids = [h.chunk_id for h in hits]
            assert hit_ids[0] == "chunk-a1-target"
            assert set(hit_ids) == {"chunk-a1-target", "chunk-a1-other"}
            # 严格断言跨组织、跨空间、跨代次返回 0 条
            assert "chunk-b1-cross-org" not in hit_ids
            assert "chunk-a2-cross-space" not in hit_ids
            assert "chunk-a1-other-gen" not in hit_ids
            # 确认返回候选模型严格不含正文文本字段
            assert not hasattr(hits[0], "text")
            assert not hasattr(hits[0], "content")

            # 任一必填隔离字段为空时必须立即抛 ValidationError，禁止裸查
            with pytest.raises(ValidationError):
                await store.search_hybrid(
                    collection,
                    organization_id="",
                    space_id="space-1",
                    index_generation_id="gen-v1",
                    query_dense=[1.0, 0.0, 0.0, 0.0],
                )
            with pytest.raises(ValidationError):
                await store.search_hybrid(
                    collection,
                    organization_id="org-a",
                    space_id="",
                    index_generation_id="gen-v1",
                    query_dense=[1.0, 0.0, 0.0, 0.0],
                )
            with pytest.raises(ValidationError):
                await store.search_hybrid(
                    collection,
                    organization_id="org-a",
                    space_id="space-1",
                    index_generation_id="",
                    query_dense=[1.0, 0.0, 0.0, 0.0],
                )
        finally:
            await store.delete_collection(collection)


# ==============================================================================
# 2. ACL Payload 同步更新与蓝绿代次删除真实生效测试
# ==============================================================================


@pytest.mark.asyncio
async def test_qdrant_acl_payload_update_and_generation_deletion() -> None:
    """直连真实 Qdrant 验证 update_document_acl_payload 与 delete_generation_points。"""
    collection = _unique_collection("acl_gen")

    async with QdrantIndexStore(settings=_live_qdrant_settings()) as store:
        try:
            await store.ensure_collection(collection, dense_dimensions=4)
            await store.upsert_chunks(
                collection,
                [
                    QdrantChunkPoint(
                        chunk_id="chunk-doc1-v1",
                        organization_id="org-a",
                        space_id="space-1",
                        document_id="doc-1",
                        document_version_id="ver-1",
                        index_generation_id="gen-v1",
                        acl_revision=1,
                        dense_vector=[1.0, 0.0, 0.0, 0.0],
                        text_for_sparse="保密研发路线图文档",
                        acl_projection=ACLProjectionPayload(
                            acl_global=False,
                            acl_user_ids=["user-admin"],
                        ),
                    ),
                    QdrantChunkPoint(
                        chunk_id="chunk-doc1-v2",
                        organization_id="org-a",
                        space_id="space-1",
                        document_id="doc-1",
                        document_version_id="ver-1",
                        index_generation_id="gen-v2",
                        acl_revision=1,
                        dense_vector=[1.0, 0.0, 0.0, 0.0],
                        text_for_sparse="保密研发路线图文档",
                        acl_projection=ACLProjectionPayload(acl_global=True),
                    ),
                ],
            )

            # 更新前 user-alice 不在 acl_user_ids，预过滤召回 0 条
            before_hits = await store.search_hybrid(
                collection,
                organization_id="org-a",
                space_id="space-1",
                index_generation_id="gen-v1",
                query_dense=[1.0, 0.0, 0.0, 0.0],
                acl_prefilter=ACLPrefilter(user_id="user-alice", include_global=False),
            )
            assert before_hits == []

            # 响应 Outbox 同步更新 ACL Payload（授予 user-alice 且递增 revision=5）
            await store.update_document_acl_payload(
                collection,
                organization_id="org-a",
                document_id="doc-1",
                acl_revision=5,
                acl_projection=ACLProjectionPayload(
                    acl_global=False,
                    acl_user_ids=["user-admin", "user-alice"],
                ),
            )

            after_hits = await store.search_hybrid(
                collection,
                organization_id="org-a",
                space_id="space-1",
                index_generation_id="gen-v1",
                query_dense=[1.0, 0.0, 0.0, 0.0],
                acl_prefilter=ACLPrefilter(user_id="user-alice", include_global=False),
            )
            assert len(after_hits) == 1
            assert after_hits[0].chunk_id == "chunk-doc1-v1"
            assert after_hits[0].acl_revision == 5

            # 蓝绿退休清理 gen-v1：gen-v1 返回 0 条，gen-v2 完整保留
            await store.delete_generation_points(
                collection,
                organization_id="org-a",
                index_generation_id="gen-v1",
            )
            gen1_after_del = await store.search_hybrid(
                collection,
                organization_id="org-a",
                space_id="space-1",
                index_generation_id="gen-v1",
                query_dense=[1.0, 0.0, 0.0, 0.0],
            )
            gen2_after_del = await store.search_hybrid(
                collection,
                organization_id="org-a",
                space_id="space-1",
                index_generation_id="gen-v2",
                query_dense=[1.0, 0.0, 0.0, 0.0],
            )
            assert gen1_after_del == []
            assert len(gen2_after_del) == 1
            assert gen2_after_del[0].chunk_id == "chunk-doc1-v2"
        finally:
            await store.delete_collection(collection)


# ==============================================================================
# 3. ACL 延迟窗口安全红线 + 多 Space 异构维度并行检索与排名 RRF 融合测试
# ==============================================================================


@pytest.mark.asyncio
async def test_multi_space_retriever_acl_delay_window_and_heterogeneous_dimensions() -> None:
    """直连真实 Qdrant 验证：
    1. ACL 延迟窗口下：Qdrant 尚未撤权但 PG 权威 ACL 拒绝，
       正文加载器/Reranker/ContextBuilder 均收到 0 条未授权；
    2. 异构维度（8 维 + 16 维不同 Profile Collection）并行检索并通过排名 RRF 正确融合。
    """
    col_8d = _unique_collection("space_a_8d")
    col_16d = _unique_collection("space_b_16d")

    vec_8d = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    vec_16d = [0.0, 1.0] + [0.0] * 14

    loader_seen_ids: list[str] = []
    reranker_seen_texts: list[str] = []

    def mock_qwen_embedding_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        dims = int(body.get("dimensions") or 8)
        emb = vec_8d if dims == 8 else vec_16d
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": emb}],
                "usage": {"total_tokens": 8},
            },
        )

    def mock_qwen_rerank_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        docs: list[str] = body["input"]["documents"]
        reranker_seen_texts.extend(docs)
        results = [
            {"index": i, "relevance_score": round(0.95 - i * 0.1, 4)} for i in range(len(docs))
        ]
        return httpx.Response(200, json={"output": {"results": results}})

    provider_settings = Settings(
        qdrant_host="127.0.0.1",
        qdrant_port=6333,
        qdrant_api_key=SecretStr("dochub_qdrant_dev_key"),
        qdrant_https=False,
        qwen_api_key=SecretStr("sk-qwen-test"),
        embedding_primary_provider="qwen",
        reranker_primary_provider="qwen",
    )

    async with (
        QdrantIndexStore(settings=provider_settings) as store,
        httpx.AsyncClient(transport=httpx.MockTransport(mock_qwen_embedding_handler)) as emb_client,
        httpx.AsyncClient(transport=httpx.MockTransport(mock_qwen_rerank_handler)) as rr_client,
    ):
        try:
            # 创建两个异构维度的真实 Collection（8 维 vs 16 维）
            await store.ensure_collection(col_8d, dense_dimensions=8)
            await store.ensure_collection(col_16d, dense_dimensions=16)

            # Space-A（8 维）：包含 1 条已撤权但 Qdrant 尚未同步的文档 + 1 条合法授权文档
            await store.upsert_chunks(
                col_8d,
                [
                    QdrantChunkPoint(
                        chunk_id="chunk-revoked-secret",
                        organization_id="org-acme",
                        space_id="space-a",
                        document_id="doc-revoked",
                        document_version_id="ver-revoked",
                        index_generation_id="gen-a1",
                        acl_revision=2,
                        dense_vector=vec_8d,  # 向量完全匹配，模拟高分召回
                        text_for_sparse="员工绩效考核与薪酬绝密方案",
                        acl_projection=ACLProjectionPayload(
                            acl_global=True
                        ),  # 模拟 Qdrant 同步延迟
                    ),
                    QdrantChunkPoint(
                        chunk_id="chunk-space-a-ok",
                        organization_id="org-acme",
                        space_id="space-a",
                        document_id="doc-a-ok",
                        document_version_id="ver-a-ok",
                        index_generation_id="gen-a1",
                        acl_revision=4,
                        dense_vector=[0.8, 0.2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                        text_for_sparse="员工年假制度与绩效考核公开指南",
                        acl_projection=ACLProjectionPayload(acl_global=True),
                    ),
                ],
            )

            # Space-B（16 维）：包含 1 条合法授权文档
            await store.upsert_chunks(
                col_16d,
                [
                    QdrantChunkPoint(
                        chunk_id="chunk-space-b-ok",
                        organization_id="org-acme",
                        space_id="space-b",
                        document_id="doc-b-ok",
                        document_version_id="ver-b-ok",
                        index_generation_id="gen-b1",
                        acl_revision=7,
                        dense_vector=vec_16d,
                        text_for_sparse="研发中心绩效考核周期与晋升标准",
                        acl_projection=ACLProjectionPayload(acl_global=True),
                    )
                ],
            )

            emb_router = EmbeddingRouter(
                settings=provider_settings,
                qwen_provider=QwenEmbeddingProvider(
                    settings=provider_settings,
                    http_client=emb_client,
                ),
            )
            rr_router = RerankerRouter(
                settings=provider_settings,
                qwen_provider=QwenRerankerProvider(
                    settings=provider_settings,
                    http_client=rr_client,
                ),
            )
            retriever = MultiSpaceHybridRetriever(
                qdrant_store=store,
                embedding_router=emb_router,
                reranker_router=rr_router,
                context_builder=ContextBuilder(max_context_tokens=1000),
            )

            # 权威 PostgreSQL ACL 复核：明确拒绝已撤权的 chunk-revoked-secret
            async def pg_authorize_chunks(candidate_ids: list[str]) -> ChunkAuthorizationResult:
                allowed = {cid for cid in candidate_ids if cid != "chunk-revoked-secret"}
                denied = set(candidate_ids) - allowed
                return ChunkAuthorizationResult(
                    allowed_chunk_ids=allowed,
                    denied_chunk_ids=denied,
                    doc_acl_revisions={"doc-a-ok": 4, "doc-b-ok": 7},
                )

            chunk_db: dict[str, AuthorizedChunkBody] = {
                "chunk-revoked-secret": AuthorizedChunkBody(
                    chunk_id="chunk-revoked-secret",
                    document_id="doc-revoked",
                    document_version_id="ver-revoked",
                    space_id="space-a",
                    document_title="绝密薪酬红头文件",
                    content="TOP_SECRET_SALARY_SHOULD_NEVER_LEAK",
                    sensitivity_level=SensitivityLevel.L3,
                ),
                "chunk-space-a-ok": AuthorizedChunkBody(
                    chunk_id="chunk-space-a-ok",
                    document_id="doc-a-ok",
                    document_version_id="ver-a-ok",
                    space_id="space-a",
                    document_title="人力资源公开指南",
                    content="员工入职满一年享有 5 天带薪年假，绩效考核每半年开展一次。",
                    page_number=2,
                    heading_path=["假期与考核"],
                    sensitivity_level=SensitivityLevel.L0,
                ),
                "chunk-space-b-ok": AuthorizedChunkBody(
                    chunk_id="chunk-space-b-ok",
                    document_id="doc-b-ok",
                    document_version_id="ver-b-ok",
                    space_id="space-b",
                    document_title="研发晋升手册",
                    content="研发中心每年 Q2 与 Q4 开展技术职级晋升答辩。",
                    page_number=5,
                    heading_path=["职级晋升"],
                    sensitivity_level=SensitivityLevel.L0,
                ),
            }

            async def pg_load_authorized_chunks(
                allowed_ids: list[str],
            ) -> list[AuthorizedChunkBody]:
                loader_seen_ids.extend(allowed_ids)
                return [chunk_db[cid] for cid in allowed_ids if cid in chunk_db]

            targets = [
                SpaceRetrievalTarget(
                    space_id="space-a",
                    collection_name=col_8d,
                    index_generation_id="gen-a1",
                    embedding_profile_id="profile-8d",
                    embedding_profile=EmbeddingProfile(
                        provider="qwen",
                        model_id="qwen-emb-8d",
                        dimensions=64,  # 仅做 Profile 标识，Mock 按 8/16 返回
                    ),
                ),
                SpaceRetrievalTarget(
                    space_id="space-b",
                    collection_name=col_16d,
                    index_generation_id="gen-b1",
                    embedding_profile_id="profile-16d",
                    embedding_profile=EmbeddingProfile(
                        provider="qwen",
                        model_id="qwen-emb-16d",
                        dimensions=128,
                    ),
                ),
            ]

            # 为精确匹配 8 维与 16 维 Collection，按 Profile 维度覆盖 Mock 响应
            def dim_aware_embedding_handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                model_id = str(body.get("model") or "")
                emb = vec_8d if "8d" in model_id else vec_16d
                return httpx.Response(
                    200,
                    json={
                        "data": [{"index": 0, "embedding": emb}],
                        "usage": {"total_tokens": 8},
                    },
                )

            emb_client._transport = httpx.MockTransport(dim_aware_embedding_handler)  # noqa: SLF001

            result = await retriever.retrieve(
                query="员工绩效考核与研发晋升周期是什么？",
                organization_id="org-acme",
                spaces=targets,
                authorize_chunks_fn=pg_authorize_chunks,
                load_authorized_chunks_fn=pg_load_authorized_chunks,
                answer_policy=AnswerPolicyMode.STRICT_EVIDENCE,
            )

            # 1) ACL 延迟窗口安全红线：正文加载器、Reranker、ContextBuilder 均 0 接触未授权 Chunk
            assert "chunk-revoked-secret" in result.audit_trace.candidate_chunk_ids
            assert result.audit_trace.denied_count == 1
            assert "chunk-revoked-secret" not in loader_seen_ids
            assert all("TOP_SECRET_SALARY_SHOULD_NEVER_LEAK" not in t for t in reranker_seen_texts)
            assert all(item.chunk.chunk_id != "chunk-revoked-secret" for item in result.items)
            full_prompt_text = "\n".join(m.content for m in result.context.messages)
            assert "TOP_SECRET_SALARY_SHOULD_NEVER_LEAK" not in full_prompt_text
            assert "绝密薪酬红头文件" not in full_prompt_text

            # 2) 多 Space 异构维度成功并行召回并通过排名 RRF 融合
            fused_ids = {item.chunk.chunk_id for item in result.items}
            assert fused_ids == {"chunk-space-a-ok", "chunk-space-b-ok"}
            assert all(
                item.rrf_score == pytest.approx(1.0 / 61.0, rel=1e-4) for item in result.items
            )
            assert "<untrusted_evidence>" in full_prompt_text
            assert 'citation_id="C1"' in full_prompt_text
            assert 'citation_id="C2"' in full_prompt_text
        finally:
            await store.delete_collection(col_8d)
            await store.delete_collection(col_16d)


# ==============================================================================
# 4. Celery 应用与 5 队列隔离拓扑测试
# ==============================================================================


def test_celery_app_queues_routes_and_beat_topology() -> None:
    """验证 Celery 真实 Redis 配置、5 队列拓扑、路由映射与心跳探针任务。"""
    assert set(ALL_CELERY_QUEUES) == {"default", "ingestion", "indexing", "faq", "outbox"}
    configured_queues = {q.name for q in celery_app.conf.task_queues}
    assert configured_queues == set(ALL_CELERY_QUEUES)
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert "redis://" in str(celery_app.conf.broker_url)
    assert "redis://" in str(celery_app.conf.result_backend)

    assert CELERY_TASK_ROUTES["app.workers.tasks.ingestion.*"]["queue"] == "ingestion"
    assert CELERY_TASK_ROUTES["app.workers.tasks.outbox.*"]["queue"] == "outbox"
    assert "outbox-relay-sweep-every-15s" in CELERY_BEAT_SCHEDULE
    assert ping_worker() == {"status": "ok", "service": "dochub-worker"}
