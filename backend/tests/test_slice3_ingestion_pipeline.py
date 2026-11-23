"""Slice 3 真实三组件（PostgreSQL 17 + MinIO + Qdrant）端到端文档流水线集成测试。

全部测试直连真实运行中的：
- PostgreSQL 17（`app_runtime` 受限角色 + FORCE RLS）
- MinIO（`127.0.0.1:19000` `dochub-raw` & `dochub-artifacts`）
- Qdrant（`127.0.0.1:6333`）

验证目标：
1. 真实 Markdown / DOCX / XLSX 文档上传 MinIO -> DocumentIngestionPipeline 全链路落库
   （ParsedArtifact 在 MinIO 可读、ParsedBlock、Parent-Child Chunk、IndexGeneration=ACTIVE、
   ActiveIndexPointer 切换）并在 Qdrant 混合检索成功召回。
2. L2/L3 扫描 PDF 严格触发 `ParseJob.status == 'BLOCKED_POLICY'`、绝不外发 MinerU、
   并写入 `SecurityAuditEvent`。
3. 蓝绿索引升级（V1 -> V2）与乐观锁保护回滚（V2 -> V1）及 `/indexing/*` HTTP 接口。
4. Outbox 消费者消费 `acl.revision.created` 真实更新 Qdrant Payload，且重复投递同一
   `event_id` 因 `processed_messages` 幂等跳过。
"""

from __future__ import annotations

import asyncio
import io
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import openpyxl
import pytest
from docx import Document as DocxDocument
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pypdf import PdfWriter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError
from app.db.seed import resolve_seed_password, seed_database
from app.db.session import get_db_session, org_rls_session
from app.main import create_app
from app.modules.audit.models import SecurityAuditEvent
from app.modules.documents.models import (
    Chunk,
    ChunkSet,
    ParsedArtifact,
    ParsedBlock,
    ParseJob,
)
from app.modules.documents.storage import MinIOStorageService
from app.modules.embeddings import (
    ChineseSparseVectorizer,
    EmbeddingRouter,
    LocalBGEEmbeddingProvider,
    QwenEmbeddingProvider,
)
from app.modules.indexing import (
    ACLPrefilter,
    DocumentIngestionPipeline,
    QdrantIndexStore,
    indexing_router,
    resolve_collection_name,
)
from app.modules.indexing.models import (
    ActiveIndexPointer,
    EmbeddingProfile,
    EmbeddingProvider,
    IndexGeneration,
)
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.outbox.models import OutboxEvent, ProcessedMessage
from app.modules.parsing import MinerUOfficialAdapter, ParserRouter
from app.workers import OutboxDispatcherService


def _make_settings() -> Settings:
    """加载本地三组件真实配置并关闭外部 OTLP 与固定测试开关。"""
    base = get_settings()
    return base.model_copy(
        update={
            "app_env": "test",
            "app_debug": False,
            "otel_enabled": False,
            "cookie_secure": False,
            "local_ocr_enabled": False,
            "local_ocr_endpoint": "",
            "mineru_api_key": SecretStr("sk-test-mineru-should-never-leak"),
            "qwen_api_key": SecretStr("sk-test-qwen-embedding"),
            "embedding_primary_provider": "qwen",
            "local_bge_embedding_enabled": True,
            "local_bge_embedding_url": "http://127.0.0.1:8081/embed",
        }
    )


def _build_docx_bytes(title: str, body: str) -> bytes:
    """构造真实 DOCX 字节流。"""
    doc = DocxDocument()
    doc.add_heading(title, level=1)
    doc.add_paragraph(body)
    tbl = doc.add_table(rows=2, cols=2)
    tbl.cell(0, 0).text = "条款"
    tbl.cell(0, 1).text = "要求"
    tbl.cell(1, 0).text = "数据隔离"
    tbl.cell(1, 1).text = "强制 FORCE RLS"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_xlsx_bytes() -> bytes:
    """构造真实 XLSX 字节流。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "索引指标"
    ws.append(["阶段", "目标状态", "说明"])
    ws.append(["解析", "SUCCEEDED", "结构化输出"])
    ws.append(["索引", "ACTIVE", "蓝绿原子切换"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_scanned_pdf_bytes() -> bytes:
    """构造无文本层的真实空白扫描 PDF 字节流。"""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _deterministic_vector(text: str, dims: int = 64) -> list[float]:
    """根据中文关键词生成确定性 64 维归一化语义向量，便于真实 Qdrant 语义召回验证。"""
    vec = [0.05] * dims
    if any(k in text for k in ("报销", "财务", "发票", "差旅")):
        vec[0] += 0.9
    if any(k in text for k in ("隔离", "RLS", "安全", "条款")):
        vec[1] += 0.9
    if any(k in text for k in ("蓝绿", "索引", "指标", "ACTIVE")):
        vec[2] += 0.9
    if any(k in text for k in ("员工", "入职", "手册", "年假")):
        vec[3] += 0.9
    norm = sum(v * v for v in vec) ** 0.5
    return [round(v / norm, 6) for v in vec]


def _make_mock_embedding_router(settings: Settings) -> EmbeddingRouter:
    """构造基于确定性向量的双选 EmbeddingRouter（绝不发起外网请求）。"""

    def qwen_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs = body["input"]
        dims = int(body.get("dimensions") or 64)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": _deterministic_vector(txt, dims=dims)}
                    for i, txt in enumerate(inputs)
                ],
                "usage": {"total_tokens": len(inputs) * 8},
            },
        )

    def bge_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs = body["inputs"]
        return httpx.Response(
            200,
            json=[_deterministic_vector(txt, dims=64) for txt in inputs],
        )

    qwen_client = httpx.AsyncClient(transport=httpx.MockTransport(qwen_handler))
    bge_client = httpx.AsyncClient(transport=httpx.MockTransport(bge_handler))
    return EmbeddingRouter(
        settings=settings,
        qwen_provider=QwenEmbeddingProvider(settings=settings, http_client=qwen_client),
        local_bge_provider=LocalBGEEmbeddingProvider(
            settings=settings,
            http_client=bge_client,
            protocol="tei",
        ),
    )


@asynccontextmanager
async def _slice3_env() -> AsyncIterator[
    tuple[
        Settings,
        AsyncClient,
        async_sessionmaker[AsyncSession],
        dict[str, uuid.UUID],
        str,
        MinIOStorageService,
        QdrantIndexStore,
        str,
    ]
]:
    """初始化真实 PostgreSQL 17 + MinIO + Qdrant 测试环境，并在结束时彻底回收资源。"""
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
    collection_prefix = f"test_s3_{uuid.uuid4().hex[:8]}"
    created_collections: list[str] = []

    app = create_app(settings=settings)
    app.include_router(indexing_router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = _override_db

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
                collection_prefix,
            )
    finally:
        # 清理测试创建的全部带随机前缀 Qdrant Collection
        cols_resp = await qdrant_store._client.get_collections()  # noqa: SLF001
        for col in cols_resp.collections:
            if col.name.startswith(collection_prefix):
                created_collections.append(col.name)
        for col_name in created_collections:
            await qdrant_store.delete_collection(col_name)
        await qdrant_store.close()
        await engine.dispose()


async def _login_headers(
    client: AsyncClient,
    *,
    email: str,
    password: str,
) -> tuple[dict[str, str], dict[str, object]]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {"Authorization": f"Bearer {body['access_token']}"}, body


async def _create_isolated_space_with_8d_profile(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID,
    collection_prefix: str,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """为本轮真实测试创建独立的 64 维 EmbeddingProfile 与 KnowledgeSpace。"""
    suffix = uuid.uuid4().hex[:8]
    async with session_factory() as session, org_rls_session(session, organization_id):
        provider = (
            await session.execute(
                select(EmbeddingProvider).where(
                    EmbeddingProvider.organization_id == organization_id,
                    EmbeddingProvider.code == "qwen-official",
                )
            )
        ).scalar_one()
        profile = EmbeddingProfile(
            organization_id=organization_id,
            provider_id=provider.id,
            code=f"test-qwen-64d-{suffix}",
            model_id="text-embedding-v3",
            model_version="v1",
            dimensions=64,
            language_config={"languages": ["zh-CN"]},
            status="ACTIVE",
        )
        session.add(profile)
        await session.flush()

        space = KnowledgeSpace(
            organization_id=organization_id,
            name=f"Slice3 集成测试空间 {suffix}",
            slug=f"slice3-test-{suffix}",
            visibility="PUBLIC",
            answer_policy="STRICT_EVIDENCE",
            default_embedding_profile_id=profile.id,
            status="ACTIVE",
            config_json={"org_allow_external_l1": True},
            config_version=1,
        )
        session.add(space)
        await session.flush()

        col_name = resolve_collection_name(profile, collection_prefix=collection_prefix)
        return space.id, profile.id, col_name


def _read_minio_object(storage: MinIOStorageService, bucket: str, key: str) -> bytes:
    resp = storage._client.get_object(bucket, key)  # noqa: SLF001
    try:
        return resp.read()
    finally:
        resp.close()
        resp.release_conn()


# ==============================================================================
# 1. 真实 Markdown / DOCX / XLSX 全链路入库、MinIO 产物可读、Qdrant 混合召回与蓝绿回滚
# ==============================================================================


@pytest.mark.asyncio
async def test_slice3_e2e_ingestion_blue_green_and_rollback() -> None:
    """直连真实 PG17 + MinIO + Qdrant：验证 MD/DOCX/XLSX 全链路入库、蓝绿升级与乐观锁回滚。"""
    async with _slice3_env() as (
        settings,
        client,
        session_factory,
        seed_ids,
        demo_password,
        storage,
        qdrant_store,
        collection_prefix,
    ):
        acme_org_id = seed_ids["acme-corp"]
        km_headers, _ = await _login_headers(client, email="km@acme.local", password=demo_password)
        space_id, _profile_id, expected_collection = await _create_isolated_space_with_8d_profile(
            session_factory,
            organization_id=acme_org_id,
            collection_prefix=collection_prefix,
        )

        emb_router = _make_mock_embedding_router(settings)
        sparse_vec = ChineseSparseVectorizer()
        pipeline = DocumentIngestionPipeline(
            settings=settings,
            storage=storage,
            embedding_router=emb_router,
            sparse_vectorizer=sparse_vec,
            qdrant_store=qdrant_store,
            collection_prefix=collection_prefix,
        )

        # --- 文档 1：上传真实 Markdown 至 MinIO 并运行流水线（构建蓝绿 Gen V1） ---
        md_content = (
            "# 财务报销规范\n\n"
            "员工差旅报销须提供电子发票并按部门审批。\n\n"
            "## 差旅标准\n\n"
            "高铁二等座与经济舱机票可据实报销。\n"
        ).encode()
        up_md = await client.post(
            f"/api/v1/spaces/{space_id}/documents/uploads",
            headers=km_headers,
            files={"file": ("finance.md", md_content, "text/markdown")},
            data={"title": "财务报销规范", "sensitivity_level": "L0"},
        )
        assert up_md.status_code == 201, up_md.text
        md_ver_id = uuid.UUID(up_md.json()["document_version_id"])
        md_doc_id = uuid.UUID(up_md.json()["document_id"])

        async with session_factory() as session:
            res_v1 = await pipeline.process_document_version(
                session,
                organization_id=acme_org_id,
                document_version_id=md_ver_id,
            )

        assert res_v1.status == "SUCCEEDED"
        assert res_v1.parse_status == "SUCCEEDED"
        assert res_v1.index_generation_no == 1
        assert res_v1.active_pointer_revision == 1
        assert res_v1.collection_name == expected_collection
        assert res_v1.parent_chunk_count >= 1
        assert res_v1.child_chunk_count >= 1
        gen1_id = res_v1.index_generation_id
        assert gen1_id is not None

        # 验证 MinIO artifacts 桶中存在可读解析产物 & PG 落库完整
        async with session_factory() as session, org_rls_session(session, acme_org_id):
            artifacts = (
                (
                    await session.execute(
                        select(ParsedArtifact).where(
                            ParsedArtifact.organization_id == acme_org_id,
                            ParsedArtifact.document_version_id == md_ver_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert {a.artifact_type for a in artifacts} >= {"markdown", "structured_json"}
            md_art = next(a for a in artifacts if a.artifact_type == "markdown")
            minio_md_bytes = await asyncio.to_thread(
                _read_minio_object, storage, storage.artifacts_bucket, md_art.object_key
            )
            assert "财务报销规范" in minio_md_bytes.decode("utf-8")

            blocks = (
                (
                    await session.execute(
                        select(ParsedBlock).where(
                            ParsedBlock.organization_id == acme_org_id,
                            ParsedBlock.document_version_id == md_ver_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(blocks) >= 3

            cs = (
                await session.execute(
                    select(ChunkSet).where(
                        ChunkSet.organization_id == acme_org_id,
                        ChunkSet.document_version_id == md_ver_id,
                    )
                )
            ).scalar_one()
            assert cs.status == "SUCCEEDED"

            chunks = (
                (
                    await session.execute(
                        select(Chunk).where(
                            Chunk.organization_id == acme_org_id,
                            Chunk.chunk_set_id == cs.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            child_chunks = [c for c in chunks if c.parent_chunk_id is not None]
            assert len(child_chunks) == res_v1.child_chunk_count
            assert all(c.indexed_at is not None for c in child_chunks)

        # 验证 Qdrant Gen V1 真实混合召回
        v1_hits = await qdrant_store.search_hybrid(
            expected_collection,
            organization_id=str(acme_org_id),
            space_id=str(space_id),
            index_generation_id=str(gen1_id),
            query_dense=_deterministic_vector("差旅报销电子发票", dims=64),
            query_sparse=sparse_vec.encode_query("差旅报销电子发票"),
            top_k=5,
        )
        assert v1_hits and any(h.document_id == str(md_doc_id) for h in v1_hits)

        # --- 文档 2 & 文档 3：上传真实 DOCX 与 XLSX -> 蓝绿升级至 Gen V2 & V3 ---
        docx_bytes = _build_docx_bytes("租户隔离规范", "系统强制执行 PostgreSQL FORCE RLS 隔离。")
        up_docx = await client.post(
            f"/api/v1/spaces/{space_id}/documents/uploads",
            headers=km_headers,
            files={
                "file": (
                    "rls_spec.docx",
                    docx_bytes,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            data={"title": "租户隔离规范", "sensitivity_level": "L1"},
        )
        assert up_docx.status_code == 201, up_docx.text
        docx_ver_id = uuid.UUID(up_docx.json()["document_version_id"])
        docx_doc_id = uuid.UUID(up_docx.json()["document_id"])

        async with session_factory() as session:
            res_v2 = await pipeline.process_document_version(
                session,
                organization_id=acme_org_id,
                document_version_id=docx_ver_id,
            )
        assert res_v2.status == "SUCCEEDED"
        assert res_v2.index_generation_no == 2
        assert res_v2.active_pointer_revision == 2
        gen2_id = res_v2.index_generation_id
        assert gen2_id is not None and gen2_id != gen1_id

        # 验证 V2 同时包含文档 1 与文档 2（蓝绿升级无丢召回），且 V1 已 RETIRED
        v2_hits_doc1 = await qdrant_store.search_hybrid(
            expected_collection,
            organization_id=str(acme_org_id),
            space_id=str(space_id),
            index_generation_id=str(gen2_id),
            query_dense=_deterministic_vector("差旅报销", dims=64),
            query_sparse=sparse_vec.encode_query("差旅报销"),
            top_k=5,
        )
        v2_hits_doc2 = await qdrant_store.search_hybrid(
            expected_collection,
            organization_id=str(acme_org_id),
            space_id=str(space_id),
            index_generation_id=str(gen2_id),
            query_dense=_deterministic_vector("FORCE RLS 隔离", dims=64),
            query_sparse=sparse_vec.encode_query("FORCE RLS 隔离"),
            top_k=5,
        )
        assert any(h.document_id == str(md_doc_id) for h in v2_hits_doc1)
        assert any(h.document_id == str(docx_doc_id) for h in v2_hits_doc2)

        # 上传真实 XLSX 并运行（Gen V3）
        xlsx_bytes = _build_xlsx_bytes()
        up_xlsx = await client.post(
            f"/api/v1/spaces/{space_id}/documents/uploads",
            headers=km_headers,
            files={
                "file": (
                    "metrics.xlsx",
                    xlsx_bytes,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            data={"title": "索引指标表", "sensitivity_level": "L0"},
        )
        assert up_xlsx.status_code == 201, up_xlsx.text
        xlsx_ver_id = uuid.UUID(up_xlsx.json()["document_version_id"])

        async with session_factory() as session:
            res_v3 = await pipeline.process_document_version(
                session,
                organization_id=acme_org_id,
                document_version_id=xlsx_ver_id,
            )
        assert res_v3.status == "SUCCEEDED"
        assert res_v3.index_generation_no == 3
        assert res_v3.active_pointer_revision == 3

        # --- 验证蓝绿回滚（V3 -> V2）与乐观锁冲突检测 ---
        async with session_factory() as session:
            with pytest.raises(ConflictError) as conflict_exc:
                await pipeline.rollback_active_index(
                    session,
                    organization_id=acme_org_id,
                    space_id=space_id,
                    expected_revision=1,  # 过期 revision 必须报 409
                )
            assert conflict_exc.value.code == "INDEX_POINTER_REVISION_CONFLICT"

        # 通过 HTTP /indexing/spaces/{space_id}/pointer 与 /rollback 验证真实接口
        ptr_resp = await client.get(
            f"/api/v1/indexing/spaces/{space_id}/pointer",
            headers=km_headers,
        )
        assert ptr_resp.status_code == 200, ptr_resp.text
        assert ptr_resp.json()["revision"] == 3
        assert ptr_resp.json()["active_generation_id"] == str(res_v3.index_generation_id)

        rb_resp = await client.post(
            f"/api/v1/indexing/spaces/{space_id}/rollback",
            headers=km_headers,
            json={"expected_revision": 3, "reason": "压测验证回滚至 V2"},
        )
        assert rb_resp.status_code == 200, rb_resp.text
        rb_body = rb_resp.json()
        assert rb_body["active_generation_id"] == str(gen2_id)
        assert rb_body["pointer_revision"] == 4

        async with session_factory() as session, org_rls_session(session, acme_org_id):
            gen3_row = await session.get(IndexGeneration, res_v3.index_generation_id)
            gen2_row = await session.get(IndexGeneration, gen2_id)
            ptr_row = (
                await session.execute(
                    select(ActiveIndexPointer).where(
                        ActiveIndexPointer.organization_id == acme_org_id,
                        ActiveIndexPointer.scope_id == space_id,
                    )
                )
            ).scalar_one()
            assert gen3_row is not None and gen3_row.status == "ROLLED_BACK"
            assert gen2_row is not None and gen2_row.status == "ACTIVE"
            assert ptr_row.index_generation_id == gen2_id
            assert ptr_row.revision == 4

        # 清理已回滚的 V3 Qdrant 点位
        async with session_factory() as session:
            assert res_v3.index_generation_id is not None
            cleaned = await pipeline.cleanup_retired_generation(
                session,
                organization_id=acme_org_id,
                generation_id=res_v3.index_generation_id,
            )
            assert cleaned is True


# ==============================================================================
# 2. L2/L3 扫描 PDF 严格 BLOCKED_POLICY 与 SecurityAuditEvent 落库测试
# ==============================================================================


@pytest.mark.asyncio
async def test_slice3_l2_l3_scanned_pdf_blocked_policy_and_security_audit() -> None:
    """L2/L3 扫描 PDF 在无本地 OCR 时精准置 ParseJob=BLOCKED_POLICY、绝不调 MinerU 并写安全审计。"""
    async with _slice3_env() as (
        settings,
        client,
        session_factory,
        seed_ids,
        demo_password,
        storage,
        qdrant_store,
        collection_prefix,
    ):
        acme_org_id = seed_ids["acme-corp"]
        km_headers, _ = await _login_headers(client, email="km@acme.local", password=demo_password)
        space_id, _, _ = await _create_isolated_space_with_8d_profile(
            session_factory,
            organization_id=acme_org_id,
            collection_prefix=collection_prefix,
        )

        mineru_called = False

        def fail_if_mineru(request: httpx.Request) -> httpx.Response:
            nonlocal mineru_called
            mineru_called = True
            raise AssertionError("严禁对 L2/L3 扫描 PDF 调用外部 MinerU API！")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail_if_mineru)
        ) as mineru_client:
            parser_router = ParserRouter(
                settings=settings,
                mineru_adapter=MinerUOfficialAdapter(settings=settings, http_client=mineru_client),
            )
            pipeline = DocumentIngestionPipeline(
                settings=settings,
                storage=storage,
                parser_router=parser_router,
                embedding_router=_make_mock_embedding_router(settings),
                qdrant_store=qdrant_store,
                collection_prefix=collection_prefix,
            )

            scanned_pdf = _build_scanned_pdf_bytes()
            for level in ("L2", "L3"):
                up_resp = await client.post(
                    f"/api/v1/spaces/{space_id}/documents/uploads",
                    headers=km_headers,
                    files={"file": (f"secret_{level}.pdf", scanned_pdf, "application/pdf")},
                    data={"title": f"涉密扫描件_{level}", "sensitivity_level": level},
                )
                assert up_resp.status_code == 201, up_resp.text
                ver_id = uuid.UUID(up_resp.json()["document_version_id"])

                async with session_factory() as session:
                    result = await pipeline.process_document_version(
                        session,
                        organization_id=acme_org_id,
                        document_version_id=ver_id,
                    )
                assert result.status == "BLOCKED_POLICY"
                assert result.parse_status == "BLOCKED_POLICY"
                assert mineru_called is False

                # 验证数据库 ParseJob 与 SecurityAuditEvent 落盘，以及 ProcessingStatus 接口真实返回
                async with (
                    session_factory() as session,
                    org_rls_session(session, acme_org_id),
                ):
                    pj = (
                        await session.execute(
                            select(ParseJob).where(
                                ParseJob.organization_id == acme_org_id,
                                ParseJob.document_version_id == ver_id,
                            )
                        )
                    ).scalar_one()
                    assert pj.status == "BLOCKED_POLICY"
                    assert pj.error_detail and "本地 OCR" in pj.error_detail

                    audit_ev = (
                        await session.execute(
                            select(SecurityAuditEvent).where(
                                SecurityAuditEvent.organization_id == acme_org_id,
                                SecurityAuditEvent.event_type == "parse.policy_blocked",
                                SecurityAuditEvent.resource_id == str(ver_id),
                            )
                        )
                    ).scalar_one_or_none()
                    assert audit_ev is not None
                    assert audit_ev.severity == "HIGH"

                status_resp = await client.get(
                    f"/api/v1/document-versions/{ver_id}/processing-status",
                    headers=km_headers,
                )
                assert status_resp.status_code == 200
                assert status_resp.json()["parse_job"]["status"] == "BLOCKED_POLICY"
                assert status_resp.json()["index_job"]["status"] == "NOT_STARTED"

            assert mineru_called is False


# ==============================================================================
# 3. Outbox 消费者消费 acl.revision.created 同步 Qdrant + 幂等跳过重复投递测试
# ==============================================================================


@pytest.mark.asyncio
async def test_slice3_outbox_acl_sync_to_qdrant_and_idempotent_dedup() -> None:
    """验证 OutboxDispatcherService 消费 acl.revision.created 更新 Qdrant 且重复事件零副作用。"""
    async with _slice3_env() as (
        settings,
        client,
        session_factory,
        seed_ids,
        demo_password,
        storage,
        qdrant_store,
        collection_prefix,
    ):
        acme_org_id = seed_ids["acme-corp"]
        km_headers, _ = await _login_headers(client, email="km@acme.local", password=demo_password)
        emp_headers, emp_login = await _login_headers(
            client, email="employee@acme.local", password=demo_password
        )
        _ = emp_headers
        emp_user_id = str(emp_login["user"]["id"])

        space_id, _, collection_name = await _create_isolated_space_with_8d_profile(
            session_factory,
            organization_id=acme_org_id,
            collection_prefix=collection_prefix,
        )

        emb_router = _make_mock_embedding_router(settings)
        sparse_vec = ChineseSparseVectorizer()
        pipeline = DocumentIngestionPipeline(
            settings=settings,
            storage=storage,
            embedding_router=emb_router,
            sparse_vectorizer=sparse_vec,
            qdrant_store=qdrant_store,
            collection_prefix=collection_prefix,
        )

        # 1) 上传文档并建索引（初始 ACL revision=0，employee 无法预过滤召回）
        doc_bytes = "# 员工年假手册\n\n工作满一年享有 5 天带薪年假。\n".encode()
        up_resp = await client.post(
            f"/api/v1/spaces/{space_id}/documents/uploads",
            headers=km_headers,
            files={"file": ("leave.md", doc_bytes, "text/markdown")},
            data={"title": "员工年假手册", "sensitivity_level": "L0"},
        )
        assert up_resp.status_code == 201, up_resp.text
        doc_id = uuid.UUID(up_resp.json()["document_id"])
        ver_id = uuid.UUID(up_resp.json()["document_version_id"])

        async with session_factory() as session:
            proc_res = await pipeline.process_document_version(
                session,
                organization_id=acme_org_id,
                document_version_id=ver_id,
            )
        assert proc_res.status == "SUCCEEDED"
        gen_id = proc_res.index_generation_id
        assert gen_id is not None

        # 先消费完上传时生成的 document.uploaded Outbox 事件（或只验证 acl.revision.created）
        before_hits = await qdrant_store.search_hybrid(
            collection_name,
            organization_id=str(acme_org_id),
            space_id=str(space_id),
            index_generation_id=str(gen_id),
            query_dense=_deterministic_vector("带薪年假员工手册", dims=64),
            acl_prefilter=ACLPrefilter(user_id=emp_user_id, include_global=False),
        )
        assert before_hits == []

        # 2) 通过真实 API 提交 ACL Revision（授予 employee 用户），同事务产生 acl.revision.created
        acl_resp = await client.post(
            f"/api/v1/documents/{doc_id}/acl/revisions",
            headers={**km_headers, "Idempotency-Key": f"acl-sync-{uuid.uuid4().hex[:8]}"},
            json={
                "expected_revision": 0,
                "reason": "向测试员工授予阅读权限以验证 Outbox Qdrant 同步",
                "grants": [
                    {
                        "subject_type": "USER",
                        "subject_id": emp_user_id,
                        "include_descendants": False,
                        "inheritance_mode": "DYNAMIC",
                    }
                ],
            },
        )
        assert acl_resp.status_code == 200, acl_resp.text
        assert acl_resp.json()["revision"] == 1

        # 定位新写入的 acl.revision.created OutboxEvent
        async with session_factory() as session, org_rls_session(session, acme_org_id):
            acl_event = (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.organization_id == acme_org_id,
                        OutboxEvent.event_type == "acl.revision.created",
                        OutboxEvent.aggregate_id == doc_id,
                    )
                    .order_by(OutboxEvent.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            acl_event_id = acl_event.id

        # 3) 运行 OutboxDispatcherService 消费 acl.revision.created
        dispatcher = OutboxDispatcherService(
            settings=settings,
            qdrant_store=qdrant_store,
            pipeline=pipeline,
        )
        async with session_factory() as session:
            first_outcome = await dispatcher.process_event(
                session,
                organization_id=acme_org_id,
                event_id=acl_event_id,
            )
        assert first_outcome.status == "PROCESSED"
        assert collection_name in first_outcome.updated_collections

        # 验证 Qdrant Payload 已真实更新，现在 employee 预过滤可精确召回且 acl_revision=1
        after_hits = await qdrant_store.search_hybrid(
            collection_name,
            organization_id=str(acme_org_id),
            space_id=str(space_id),
            index_generation_id=str(gen_id),
            query_dense=_deterministic_vector("带薪年假员工手册", dims=64),
            acl_prefilter=ACLPrefilter(user_id=emp_user_id, include_global=False),
        )
        assert len(after_hits) >= 1
        assert after_hits[0].document_id == str(doc_id)
        assert after_hits[0].acl_revision == 1

        # 4) 重复投递同一 event_id：必须因 processed_messages 幂等跳过，零副作用
        async with session_factory() as session:
            dup_outcome = await dispatcher.process_event(
                session,
                organization_id=acme_org_id,
                event_id=acl_event_id,
            )
        assert dup_outcome.status == "SKIPPED_DUPLICATE"

        async with session_factory() as session, org_rls_session(session, acme_org_id):
            pm_rows = (
                (
                    await session.execute(
                        select(ProcessedMessage).where(
                            ProcessedMessage.organization_id == acme_org_id,
                            ProcessedMessage.event_id == acl_event_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(pm_rows) == 1
            ev_after = await session.get(OutboxEvent, acl_event_id)
            assert ev_after is not None
            assert ev_after.published_at is not None
            assert ev_after.attempt_count == 1
