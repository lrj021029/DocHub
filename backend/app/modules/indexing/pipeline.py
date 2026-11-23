"""端到端文档处理流水线（DocumentIngestionPipeline）与蓝绿索引治理服务。

完整执行链路（严格遵循 PostgreSQL RLS + MinIO + ParserRouter + Chunker + Embedding + Qdrant）：
1. 在 `org_rls_session` 事务内回查 PostgreSQL 最新 `Document.sensitivity_level`、`DocumentVersion`、
   `KnowledgeSpace.default_embedding_profile_id` 与 `EmbeddingProfile`
   （绝不信任入队消息中的敏感度字段）；
2. 从真实 MinIO 下载原始文件字节并校验 SHA-256 摘要；
3. 调用 `ParserRouter`（支持 MinerU / 本地 OCR / 7 类轻量解析器）；若命中 `PolicyBlockedError`
   （如 L2/L3 扫描 PDF 且未开启本地 OCR），立即置 `ParseJob.status = 'BLOCKED_POLICY'`、写入
   `SecurityAuditEvent` 并安全中止；
4. 上传结构化 JSON / Markdown / 页面渲染图至 MinIO `dochub-artifacts` 桶，
   持久化 `ParsedArtifact` 与 `ParsedBlock`；
5. 运行 `StructureAwareChunker` 生成并持久化 `ChunkSet` 与 Parent-Child `Chunk`
   （写入权威 `content_text`）；
6. 读取 `AuthoritativeACLEngine.get_document_acl_projection`，生成 Dense + 中文 BM25 Sparse 向量；
7. 构建新 `IndexGeneration`（`BUILDING` -> `VALIDATING` -> `READY` -> `ACTIVE`），写入 Qdrant 共享
   Collection，并使用乐观锁 `revision` 原子切换 `ActiveIndexPointer`（支持蓝绿回滚与旧代次清理）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ConflictError, NotFoundError, PolicyBlockedError, ValidationError
from app.core.ids import new_uuid7, new_uuid7_str
from app.db.session import org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.audit.service import record_audit_event, record_security_audit_event
from app.modules.chunking.chunker import ChunkingConfig, StructureAwareChunker
from app.modules.documents.models import (
    Chunk,
    ChunkSet,
    Document,
    DocumentVersion,
    ParsedArtifact,
    ParsedBlock,
    ParseJob,
)
from app.modules.documents.storage import MinIOStorageService
from app.modules.embeddings.providers import EmbeddingProfile as RuntimeEmbeddingProfile
from app.modules.embeddings.providers import EmbeddingRouter
from app.modules.embeddings.sparse import ChineseSparseVectorizer
from app.modules.indexing.models import (
    ActiveIndexPointer,
    EmbeddingProfile,
    EmbeddingProvider,
    IndexGeneration,
    IndexJob,
)
from app.modules.indexing.qdrant_store import (
    ACLProjectionPayload,
    QdrantChunkPoint,
    QdrantIndexStore,
)
from app.modules.knowledge_spaces.models import KnowledgeSpace, SpacePolicy
from app.modules.parsing.base import SyncLightweightParser
from app.modules.parsing.router import ParserRouter
from app.modules.parsing.schemas import ParseArtifactBundle, ParseJobState, ParseRequest
from app.modules.providers.egress import SensitivityLevel

logger = logging.getLogger(__name__)

# 文件扩展名 -> ChunkingConfig doc_type 策略映射
_EXT_TO_DOC_TYPE: dict[str, str] = {
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".docx": "docx",
    ".xlsx": "spreadsheet",
    ".xls": "spreadsheet",
    ".xlsm": "spreadsheet",
    ".csv": "spreadsheet",
    ".tsv": "spreadsheet",
    ".json": "json",
    ".txt": "txt",
}


class PipelineExecutionResult(BaseModel):
    """单次文档处理流水线执行结果摘要。"""

    organization_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    space_id: uuid.UUID
    status: str = Field(..., description="整体处理结果（SUCCEEDED / BLOCKED_POLICY / FAILED）")
    parse_job_id: uuid.UUID | None = None
    parse_status: str
    chunk_set_id: uuid.UUID | None = None
    chunk_count: int = 0
    parent_chunk_count: int = 0
    child_chunk_count: int = 0
    index_generation_id: uuid.UUID | None = None
    index_generation_no: int | None = None
    active_pointer_revision: int | None = None
    collection_name: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


class ActiveIndexRollbackResult(BaseModel):
    """蓝绿索引回滚结果回执。"""

    organization_id: uuid.UUID
    space_id: uuid.UUID
    rolled_back_from_generation_id: uuid.UUID
    active_generation_id: uuid.UUID
    active_generation_no: int
    pointer_revision: int
    collection_name: str


def resolve_collection_name(
    profile: EmbeddingProfile,
    *,
    collection_prefix: str = "dochub",
) -> str:
    """根据 EmbeddingProfile 与可选前缀生成规范化 Qdrant 共享 Collection 名称。"""
    explicit = (
        profile.language_config.get("collection_name")
        if isinstance(profile.language_config, dict)
        else None
    )
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    safe_code = "".join(ch if ch.isalnum() else "_" for ch in profile.code.lower()).strip("_")
    safe_prefix = collection_prefix.strip().strip("_") or "dochub"
    return f"{safe_prefix}_{safe_code}_{profile.dimensions}d"


def _build_artifact_object_key(
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    version_no: int,
    artifact_name: str,
) -> str:
    safe_name = posixpath.basename(artifact_name.replace("\\", "/")) or "artifact.bin"
    return (
        f"org/{organization_id}/space/{space_id}/doc/{document_id}/"
        f"v{version_no}/artifacts/{new_uuid7_str()}_{safe_name}"
    )


class DocumentIngestionPipeline:
    """生产级端到端文档处理流水线（支持单阶段故障追踪、Fail-Closed 与蓝绿索引切换）。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        storage: MinIOStorageService | None = None,
        parser_router: ParserRouter | None = None,
        chunker: StructureAwareChunker | None = None,
        embedding_router: EmbeddingRouter | None = None,
        sparse_vectorizer: ChineseSparseVectorizer | None = None,
        qdrant_store: QdrantIndexStore | None = None,
        acl_engine: AuthoritativeACLEngine | None = None,
        collection_prefix: str = "dochub",
    ) -> None:
        self._settings = settings or get_settings()
        self._storage = storage or MinIOStorageService(settings=self._settings)
        self._parser_router = parser_router or ParserRouter(settings=self._settings)
        self._chunker = chunker or StructureAwareChunker(config=ChunkingConfig())
        self._embedding_router = embedding_router or EmbeddingRouter(settings=self._settings)
        self._sparse = sparse_vectorizer or ChineseSparseVectorizer()
        self._qdrant = qdrant_store or QdrantIndexStore(
            settings=self._settings,
            sparse_vectorizer=self._sparse,
        )
        self._acl_engine = acl_engine or AuthoritativeACLEngine()
        self._collection_prefix = collection_prefix

    async def _download_raw_object(self, object_key: str) -> bytes:
        """从 MinIO 原始存储桶下载文件完整字节。"""
        import asyncio

        def _read_sync() -> bytes:
            response = self._storage._client.get_object(  # noqa: SLF001
                self._storage.raw_bucket,
                object_key,
            )
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_read_sync)

    @staticmethod
    async def _resolve_space_profile_and_policy(
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        space: KnowledgeSpace,
    ) -> tuple[EmbeddingProfile, EmbeddingProvider, bool]:
        """加载 Space 绑定的 EmbeddingProfile 与外发策略配置。"""
        profile: EmbeddingProfile | None = None
        if space.default_embedding_profile_id is not None:
            profile = (
                await session.execute(
                    select(EmbeddingProfile).where(
                        EmbeddingProfile.organization_id == organization_id,
                        EmbeddingProfile.id == space.default_embedding_profile_id,
                        EmbeddingProfile.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()

        if profile is None:
            profile = (
                await session.execute(
                    select(EmbeddingProfile)
                    .where(
                        EmbeddingProfile.organization_id == organization_id,
                        EmbeddingProfile.status == "ACTIVE",
                    )
                    .order_by(EmbeddingProfile.created_at.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()

        if profile is None:
            raise ValidationError(
                code="EMBEDDING_PROFILE_MISSING",
                message="当前组织未配置激活的 EmbeddingProfile，无法构建索引",
            )

        provider = (
            await session.execute(
                select(EmbeddingProvider).where(
                    EmbeddingProvider.organization_id == organization_id,
                    EmbeddingProvider.id == profile.provider_id,
                )
            )
        ).scalar_one_or_none()
        if provider is None:
            raise ValidationError(
                code="EMBEDDING_PROVIDER_MISSING",
                message="EmbeddingProfile 关联的 Provider 记录不存在",
            )

        space_policy = (
            await session.execute(
                select(SpacePolicy).where(
                    SpacePolicy.organization_id == organization_id,
                    SpacePolicy.space_id == space.id,
                )
            )
        ).scalar_one_or_none()
        org_allow_external_l1 = False
        if space_policy and isinstance(space_policy.policy_json, dict):
            org_allow_external_l1 = bool(
                space_policy.policy_json.get("org_allow_external_l1", False)
            )
        elif isinstance(space.config_json, dict):
            org_allow_external_l1 = bool(space.config_json.get("org_allow_external_l1", False))

        return profile, provider, org_allow_external_l1

    @staticmethod
    def _build_runtime_embedding_profile(
        profile: EmbeddingProfile,
        provider: EmbeddingProvider,
    ) -> RuntimeEmbeddingProfile:
        preferred = (
            "local_bge"
            if provider.provider_type == "local_bge"
            else ("qwen" if provider.provider_type == "qwen" else None)
        )
        return RuntimeEmbeddingProfile(
            provider=preferred,
            model_id=profile.model_id,
            model_version=profile.model_version,
            dimensions=profile.dimensions,
        )

    async def process_document_version(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        document_version_id: uuid.UUID,
        actor_user_id: uuid.UUID | None = None,
        org_allow_external_l1_override: bool | None = None,
        include_space_documents: bool = True,
        raise_on_policy_blocked: bool = False,
    ) -> PipelineExecutionResult:
        """执行指定文档版本的完整导入解析、切片、向量化与蓝绿索引切换流水线。"""
        # --- 阶段 1：在受控 org_rls_session 下回查 PostgreSQL 权威元数据 ---
        async with org_rls_session(session, organization_id, user_id=actor_user_id):
            row = (
                await session.execute(
                    select(DocumentVersion, Document, KnowledgeSpace)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .join(KnowledgeSpace, KnowledgeSpace.id == Document.space_id)
                    .where(
                        DocumentVersion.id == document_version_id,
                        DocumentVersion.organization_id == organization_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise NotFoundError(
                    code="DOCUMENT_VERSION_NOT_FOUND",
                    message="待处理文档版本不存在或不属于当前组织",
                )
            version, doc, space = row

            profile, emb_provider, policy_allow_l1 = await self._resolve_space_profile_and_policy(
                session,
                organization_id=organization_id,
                space=space,
            )
            org_allow_external_l1 = (
                org_allow_external_l1_override
                if org_allow_external_l1_override is not None
                else policy_allow_l1
            )

            parse_job = (
                await session.execute(
                    select(ParseJob)
                    .where(
                        ParseJob.organization_id == organization_id,
                        ParseJob.document_version_id == version.id,
                    )
                    .order_by(ParseJob.created_at.desc())
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if parse_job is None:
                parse_job = ParseJob(
                    organization_id=organization_id,
                    document_version_id=version.id,
                    parser_provider="auto",
                    status="QUEUED",
                    idempotency_key=f"parse:{version.id}:{version.version_no}",
                )
                session.add(parse_job)
                await session.flush()

            parse_job.status = ParseJobState.SUBMITTING.value
            parse_job.error_detail = None

            # 锁定快照值供事务外 MinIO/解析阶段使用
            source_object_key = version.source_object_key
            declared_sha256 = version.file_sha256
            filename = version.filename
            mime_type = version.mime_type
            version_no = version.version_no
            sensitivity_str = doc.sensitivity_level
            doc_id = doc.id
            space_id = space.id
            space_config_json = dict(space.config_json or {})
            parse_job_id = parse_job.id
            parse_idem_key = parse_job.idempotency_key or f"parse:{version.id}:{version_no}"
            existing_external_task_id = parse_job.external_task_id
            profile_id = profile.id
            profile_dims = profile.dimensions
            collection_name = resolve_collection_name(
                profile,
                collection_prefix=self._collection_prefix,
            )
            runtime_profile = self._build_runtime_embedding_profile(profile, emb_provider)

        # --- 阶段 2：从真实 MinIO 下载原始字节并核验 SHA-256 ---
        try:
            raw_bytes = await self._download_raw_object(source_object_key)
        except Exception as exc:
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(ParseJob)
                    .where(
                        ParseJob.organization_id == organization_id,
                        ParseJob.id == parse_job_id,
                    )
                    .values(
                        status=ParseJobState.FAILED.value,
                        error_detail=f"从 MinIO 读取原始文件失败: {type(exc).__name__}",
                    )
                )
            raise

        actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if declared_sha256 and actual_sha256 != declared_sha256.strip().lower():
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(ParseJob)
                    .where(
                        ParseJob.organization_id == organization_id,
                        ParseJob.id == parse_job_id,
                    )
                    .values(
                        status=ParseJobState.FAILED.value,
                        error_detail="原始文件 SHA-256 完整性校验失败",
                    )
                )
            raise ValidationError(
                code="CHECKSUM_MISMATCH",
                message="流水线下载的原始文件 SHA-256 与登记摘要不一致",
            )

        # --- 阶段 3：调用 ParserRouter（严格执行 L0-L3 敏感外发门禁与 Fail-Closed） ---
        parse_request = ParseRequest(
            document_version_id=str(document_version_id),
            file_bytes=raw_bytes,
            filename=filename,
            mime_type=mime_type,
            sensitivity_level=SensitivityLevel(sensitivity_str),
            org_allow_external_l1=org_allow_external_l1,
            idempotency_key=parse_idem_key,
            existing_external_task_id=existing_external_task_id,
            organization_id=str(organization_id),
        )

        try:
            parser, route_decision = self._parser_router.select_parser(parse_request)
        except PolicyBlockedError as exc:
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(ParseJob)
                    .where(
                        ParseJob.organization_id == organization_id,
                        ParseJob.id == parse_job_id,
                    )
                    .values(
                        status=ParseJobState.BLOCKED_POLICY.value,
                        error_detail=exc.message[:512],
                    )
                )
                record_security_audit_event(
                    session,
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    event_type="parse.policy_blocked",
                    severity="HIGH",
                    resource_type="document_version",
                    resource_id=document_version_id,
                    details={
                        "document_id": str(doc_id),
                        "sensitivity_level": sensitivity_str,
                        "reason_code": exc.code,
                        "parse_job_id": str(parse_job_id),
                    },
                )
            if raise_on_policy_blocked:
                raise
            return PipelineExecutionResult(
                organization_id=organization_id,
                document_id=doc_id,
                document_version_id=document_version_id,
                space_id=space_id,
                status="BLOCKED_POLICY",
                parse_job_id=parse_job_id,
                parse_status=ParseJobState.BLOCKED_POLICY.value,
                error_code=exc.code,
                error_detail=exc.message,
            )

        bundle: ParseArtifactBundle
        try:
            if isinstance(parser, SyncLightweightParser):
                bundle = parser.parse(parse_request)
            else:
                submission = await parser.submit(parse_request)
                # 提交后先回写 external_task_id，满足崩溃恢复对账要求
                async with org_rls_session(session, organization_id, user_id=actor_user_id):
                    await session.execute(
                        update(ParseJob)
                        .where(
                            ParseJob.organization_id == organization_id,
                            ParseJob.id == parse_job_id,
                        )
                        .values(
                            parser_provider=submission.parser_provider,
                            parser_version=submission.parser_version,
                            external_task_id=submission.external_task_id,
                            status=submission.status.value,
                        )
                    )
                if submission.status not in {
                    ParseJobState.SUCCEEDED,
                    ParseJobState.FAILED,
                    ParseJobState.CANCELLED,
                }:
                    wait_fn = getattr(parser, "wait_for_completion", None)
                    if callable(wait_fn):
                        final_st = await wait_fn(submission.external_task_id)
                    else:
                        final_st = await parser.get_status(submission.external_task_id)
                    if final_st.state != ParseJobState.SUCCEEDED:
                        raise ValidationError(
                            code=final_st.error_code or "PARSE_FAILED",
                            message=final_st.error_message or "异步解析任务未成功完成",
                        )
                bundle = await parser.fetch_result(submission.external_task_id)
        except PolicyBlockedError as exc:
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(ParseJob)
                    .where(
                        ParseJob.organization_id == organization_id,
                        ParseJob.id == parse_job_id,
                    )
                    .values(
                        status=ParseJobState.BLOCKED_POLICY.value,
                        error_detail=exc.message[:512],
                    )
                )
                record_security_audit_event(
                    session,
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    event_type="parse.policy_blocked",
                    severity="HIGH",
                    resource_type="document_version",
                    resource_id=document_version_id,
                    details={
                        "document_id": str(doc_id),
                        "sensitivity_level": sensitivity_str,
                        "reason_code": exc.code,
                    },
                )
            if raise_on_policy_blocked:
                raise
            return PipelineExecutionResult(
                organization_id=organization_id,
                document_id=doc_id,
                document_version_id=document_version_id,
                space_id=space_id,
                status="BLOCKED_POLICY",
                parse_job_id=parse_job_id,
                parse_status=ParseJobState.BLOCKED_POLICY.value,
                error_code=exc.code,
                error_detail=exc.message,
            )
        except Exception as exc:
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(ParseJob)
                    .where(
                        ParseJob.organization_id == organization_id,
                        ParseJob.id == parse_job_id,
                    )
                    .values(
                        status=ParseJobState.FAILED.value,
                        parser_provider=route_decision.parser_provider,
                        error_detail=str(exc)[:512],
                    )
                )
            raise

        # --- 阶段 4：上传解析产物至 MinIO 并落库 ParsedArtifact / ParsedBlock ---
        md_bytes = bundle.markdown.encode("utf-8")
        json_bytes = json.dumps(bundle.raw_json, ensure_ascii=False, indent=2).encode("utf-8")

        md_key = _build_artifact_object_key(
            organization_id=organization_id,
            space_id=space_id,
            document_id=doc_id,
            version_no=version_no,
            artifact_name="parsed.md",
        )
        json_key = _build_artifact_object_key(
            organization_id=organization_id,
            space_id=space_id,
            document_id=doc_id,
            version_no=version_no,
            artifact_name="structured.json",
        )
        md_stored = await self._storage.put_bytes(
            bucket_name=self._storage.artifacts_bucket,
            object_key=md_key,
            data=md_bytes,
            content_type="text/markdown; charset=utf-8",
        )
        json_stored = await self._storage.put_bytes(
            bucket_name=self._storage.artifacts_bucket,
            object_key=json_key,
            data=json_bytes,
            content_type="application/json; charset=utf-8",
        )

        page_image_records: list[tuple[str, str, dict[str, Any]]] = []
        for page_img in bundle.rendered_pages:
            if not page_img.content_bytes:
                continue
            img_key = _build_artifact_object_key(
                organization_id=organization_id,
                space_id=space_id,
                document_id=doc_id,
                version_no=version_no,
                artifact_name=page_img.filename,
            )
            img_stored = await self._storage.put_bytes(
                bucket_name=self._storage.artifacts_bucket,
                object_key=img_key,
                data=page_img.content_bytes,
                content_type=page_img.mime_type,
            )
            page_image_records.append(
                (
                    img_stored.object_key,
                    img_stored.sha256,
                    {
                        "page_number": page_img.page_number,
                        "filename": page_img.filename,
                        "size_bytes": img_stored.size_bytes,
                    },
                )
            )

        async with org_rls_session(session, organization_id, user_id=actor_user_id):
            await session.execute(
                update(ParseJob)
                .where(
                    ParseJob.organization_id == organization_id,
                    ParseJob.id == parse_job_id,
                )
                .values(
                    parser_provider=bundle.parser_provider,
                    parser_version=bundle.parser_version,
                    status=ParseJobState.SUCCEEDED.value,
                    error_detail=None,
                )
            )

            # 清理同版本旧产物与旧块以保持重试幂等性
            await session.execute(
                delete(ParsedArtifact).where(
                    ParsedArtifact.organization_id == organization_id,
                    ParsedArtifact.document_version_id == document_version_id,
                )
            )
            await session.execute(
                delete(ParsedBlock).where(
                    ParsedBlock.organization_id == organization_id,
                    ParsedBlock.document_version_id == document_version_id,
                )
            )

            session.add(
                ParsedArtifact(
                    organization_id=organization_id,
                    document_version_id=document_version_id,
                    artifact_type="markdown",
                    object_key=md_stored.object_key,
                    sha256=md_stored.sha256,
                    metadata_json={
                        "bucket": self._storage.artifacts_bucket,
                        "size_bytes": md_stored.size_bytes,
                        "parser_provider": bundle.parser_provider,
                    },
                )
            )
            session.add(
                ParsedArtifact(
                    organization_id=organization_id,
                    document_version_id=document_version_id,
                    artifact_type="structured_json",
                    object_key=json_stored.object_key,
                    sha256=json_stored.sha256,
                    metadata_json={
                        "bucket": self._storage.artifacts_bucket,
                        "size_bytes": json_stored.size_bytes,
                        "block_count": len(bundle.blocks),
                    },
                )
            )
            for img_obj_key, img_sha, img_meta in page_image_records:
                session.add(
                    ParsedArtifact(
                        organization_id=organization_id,
                        document_version_id=document_version_id,
                        artifact_type="page_image",
                        object_key=img_obj_key,
                        sha256=img_sha,
                        metadata_json={
                            "bucket": self._storage.artifacts_bucket,
                            **img_meta,
                        },
                    )
                )

            persisted_blocks_for_chunking = []
            for seq_no, blk in enumerate(bundle.blocks):
                db_block_id = new_uuid7()
                bbox_list = blk.bounding_box if isinstance(blk.bounding_box, list) else None
                meta_json = dict(blk.metadata)
                if isinstance(blk.bounding_box, dict):
                    meta_json["bounding_box_dict"] = blk.bounding_box
                session.add(
                    ParsedBlock(
                        id=db_block_id,
                        organization_id=organization_id,
                        document_version_id=document_version_id,
                        sequence=seq_no,
                        block_type=blk.block_type.value,
                        page_number=blk.page_number,
                        heading_path=list(blk.heading_path),
                        content_text=(blk.markdown or blk.text or "").strip(),
                        bounding_box=bbox_list,
                        metadata_json=meta_json,
                    )
                )
                persisted_blocks_for_chunking.append(
                    blk.model_copy(update={"block_id": str(db_block_id)})
                )

        # --- 阶段 5：执行 StructureAwareChunker 并持久化 ChunkSet + Parent-Child Chunk ---
        ext = Path(filename).suffix.lower()
        doc_type = _EXT_TO_DOC_TYPE.get(ext)
        space_chunk_override = (
            space_config_json.get("chunking")
            if isinstance(space_config_json.get("chunking"), dict)
            else None
        )
        chunk_output = self._chunker.chunk_blocks(
            persisted_blocks_for_chunking,
            document_version_id=str(document_version_id),
            doc_type=doc_type,
            space_id=str(space_id),
            space_override=space_chunk_override,
        )

        chunk_set_id = new_uuid7()
        child_records_for_indexing: list[tuple[uuid.UUID, uuid.UUID | None, str, int | None]] = []

        async with org_rls_session(session, organization_id, user_id=actor_user_id):
            chunk_set = ChunkSet(
                id=chunk_set_id,
                organization_id=organization_id,
                document_version_id=document_version_id,
                chunker_version=chunk_output.chunker_version,
                config_hash=chunk_output.config_hash,
                status="RUNNING",
            )
            session.add(chunk_set)
            await session.flush()

            parent_id_map: dict[str, uuid.UUID] = {}
            for p_chunk in chunk_output.parents:
                db_pid = new_uuid7()
                parent_id_map[p_chunk.chunk_id] = db_pid
                session.add(
                    Chunk(
                        id=db_pid,
                        organization_id=organization_id,
                        chunk_set_id=chunk_set_id,
                        document_version_id=document_version_id,
                        parent_chunk_id=None,
                        sequence=p_chunk.sequence_no,
                        content_text=p_chunk.markdown,
                        content_hash=p_chunk.content_hash,
                        token_count=p_chunk.token_count,
                        source_block_ids=list(p_chunk.source_block_ids),
                        page_range=p_chunk.page_range.model_dump() if p_chunk.page_range else {},
                        metadata_json={
                            "chunk_role": "parent",
                            "heading_path": p_chunk.heading_path,
                            **p_chunk.metadata,
                        },
                    )
                )

            await session.flush()

            for c_chunk in chunk_output.children:
                db_cid = new_uuid7()
                db_parent_uuid = (
                    parent_id_map.get(c_chunk.parent_chunk_id) if c_chunk.parent_chunk_id else None
                )
                session.add(
                    Chunk(
                        id=db_cid,
                        organization_id=organization_id,
                        chunk_set_id=chunk_set_id,
                        document_version_id=document_version_id,
                        parent_chunk_id=db_parent_uuid,
                        sequence=c_chunk.sequence_no,
                        content_text=c_chunk.markdown,
                        content_hash=c_chunk.content_hash,
                        token_count=c_chunk.token_count,
                        source_block_ids=list(c_chunk.source_block_ids),
                        page_range=c_chunk.page_range.model_dump() if c_chunk.page_range else {},
                        metadata_json={
                            "chunk_role": "child",
                            "heading_path": c_chunk.heading_path,
                            **c_chunk.metadata,
                        },
                    )
                )
                child_records_for_indexing.append(
                    (db_cid, db_parent_uuid, c_chunk.markdown, c_chunk.sequence_no)
                )

            chunk_set.status = "SUCCEEDED"

        # --- 阶段 6：准备索引语料（本版本 + 同 Space 其他生效版本，确保蓝绿切换零丢召回） ---
        acl_proj = await self._acl_engine.get_document_acl_projection(
            session,
            organization_id=organization_id,
            document_id=doc_id,
        )
        doc_acl_proj = ACLProjectionPayload(
            acl_global=acl_proj.acl_global,
            acl_dept_ids=acl_proj.acl_dept_ids,
            acl_role_ids=acl_proj.acl_role_ids,
            acl_user_ids=acl_proj.acl_user_ids,
        )

        # 收集需要纳入本次新 IndexGeneration 的全部切片条目
        # 条目格式: (chunk_id, document_id, document_version_id, parent_chunk_id,
        #          sensitivity_level, acl_revision, acl_proj, content_text)
        index_items: list[
            tuple[
                uuid.UUID,
                uuid.UUID,
                uuid.UUID,
                uuid.UUID | None,
                str,
                int,
                ACLProjectionPayload,
                str,
            ]
        ] = [
            (
                cid,
                doc_id,
                document_version_id,
                pid,
                sensitivity_str,
                acl_proj.acl_revision,
                doc_acl_proj,
                text,
            )
            for cid, pid, text, _ in child_records_for_indexing
        ]

        if include_space_documents:
            extra_items = await self._collect_other_space_chunks(
                session,
                organization_id=organization_id,
                space_id=space_id,
                exclude_document_id=doc_id,
            )
            index_items.extend(extra_items)

        # --- 阶段 7：创建 IndexGeneration (BUILDING->VALIDATING->READY->ACTIVE) 并原子切换 ---
        async with org_rls_session(session, organization_id, user_id=actor_user_id):
            max_gen_no = int(
                (
                    await session.execute(
                        select(func.coalesce(func.max(IndexGeneration.generation_no), 0)).where(
                            IndexGeneration.organization_id == organization_id,
                            IndexGeneration.embedding_profile_id == profile_id,
                        )
                    )
                ).scalar_one()
            )
            next_gen_no = max_gen_no + 1

            pointer_before = (
                await session.execute(
                    select(ActiveIndexPointer).where(
                        ActiveIndexPointer.organization_id == organization_id,
                        ActiveIndexPointer.scope_type == "KNOWLEDGE_SPACE",
                        ActiveIndexPointer.scope_id == space_id,
                    )
                )
            ).scalar_one_or_none()
            prev_gen_id = pointer_before.index_generation_id if pointer_before else None

            new_gen = IndexGeneration(
                id=new_uuid7(),
                organization_id=organization_id,
                embedding_profile_id=profile_id,
                collection_name=collection_name,
                generation_no=next_gen_no,
                chunker_version=chunk_output.chunker_version,
                status="BUILDING",
                metrics_json={
                    "document_version_id": str(document_version_id),
                    "previous_generation_id": str(prev_gen_id) if prev_gen_id else None,
                    "chunk_count": len(index_items),
                },
            )
            session.add(new_gen)
            await session.flush()

            index_job = IndexJob(
                id=new_uuid7(),
                organization_id=organization_id,
                generation_id=new_gen.id,
                document_version_id=document_version_id,
                status="BUILDING",
                progress=20,
            )
            session.add(index_job)
            await session.flush()
            new_gen_id = new_gen.id
            index_job_id = index_job.id

        # 在事务外执行 Embedding 计算与 Qdrant 写入，避免长时间占用数据库行锁
        try:
            qdrant_points: list[QdrantChunkPoint] = []
            if index_items:
                # 按敏感等级分组向量化，确保每条切片均经严格 DataEgressPolicyEngine 校验
                texts_by_level: dict[str, list[int]] = {}
                for idx, item in enumerate(index_items):
                    texts_by_level.setdefault(item[4], []).append(idx)

                dense_vectors: list[list[float]] = [[] for _ in index_items]
                for lvl, indices in texts_by_level.items():
                    batch_texts = [index_items[i][7] for i in indices]
                    emb_res = await self._embedding_router.embed_documents(
                        batch_texts,
                        sensitivity_level=SensitivityLevel(lvl),
                        org_allow_external_l1=org_allow_external_l1,
                        profile=runtime_profile,
                        organization_id=str(organization_id),
                        resource_id=str(document_version_id),
                    )
                    for pos, vec in zip(indices, emb_res.vectors, strict=True):
                        dense_vectors[pos] = vec

                sparse_vectors = self._sparse.encode_documents([item[7] for item in index_items])
                for idx, item in enumerate(index_items):
                    (
                        cid,
                        item_doc_id,
                        item_ver_id,
                        item_pid,
                        item_lvl,
                        item_acl_rev,
                        item_acl_proj,
                        item_text,
                    ) = item
                    sp_vec = sparse_vectors[idx]
                    qdrant_points.append(
                        QdrantChunkPoint(
                            chunk_id=str(cid),
                            organization_id=str(organization_id),
                            space_id=str(space_id),
                            document_id=str(item_doc_id),
                            document_version_id=str(item_ver_id),
                            parent_chunk_id=str(item_pid) if item_pid else None,
                            index_generation_id=str(new_gen_id),
                            acl_revision=item_acl_rev,
                            sensitivity_level=item_lvl,
                            dense_vector=dense_vectors[idx],
                            sparse_indices=list(sp_vec.indices),
                            sparse_values=list(sp_vec.values),
                            text_for_sparse=item_text,
                            acl_projection=item_acl_proj,
                        )
                    )

            await self._qdrant.ensure_collection(collection_name, profile_dims)
            if qdrant_points:
                await self._qdrant.upsert_chunks(collection_name, qdrant_points)

            # 进入 VALIDATING -> READY 状态并执行真实 Qdrant 探活校验
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == new_gen_id,
                    )
                    .values(status="VALIDATING")
                )
                await session.execute(
                    update(IndexJob)
                    .where(
                        IndexJob.organization_id == organization_id,
                        IndexJob.id == index_job_id,
                    )
                    .values(status="VALIDATING", progress=85)
                )

            if qdrant_points:
                probe_hits = await self._qdrant.search_hybrid(
                    collection_name,
                    organization_id=str(organization_id),
                    space_id=str(space_id),
                    index_generation_id=str(new_gen_id),
                    query_dense=qdrant_points[0].dense_vector,
                    top_k=1,
                )
                if not probe_hits:
                    raise ValidationError(
                        code="QDRANT_INDEX_VALIDATION_FAILED",
                        message="Qdrant 蓝绿代次写入校验未召回任何点位",
                    )

            # READY -> 原子乐观锁切换 ActiveIndexPointer -> ACTIVE
            now = datetime.now(UTC)
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == new_gen_id,
                    )
                    .values(status="READY")
                )

                pointer = (
                    await session.execute(
                        select(ActiveIndexPointer)
                        .where(
                            ActiveIndexPointer.organization_id == organization_id,
                            ActiveIndexPointer.scope_type == "KNOWLEDGE_SPACE",
                            ActiveIndexPointer.scope_id == space_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()

                if pointer is None:
                    pointer = ActiveIndexPointer(
                        organization_id=organization_id,
                        scope_type="KNOWLEDGE_SPACE",
                        scope_id=space_id,
                        embedding_profile_id=profile_id,
                        index_generation_id=new_gen_id,
                        revision=1,
                    )
                    session.add(pointer)
                    await session.flush()
                    active_rev = 1
                else:
                    old_active_gen_id = pointer.index_generation_id
                    if old_active_gen_id != new_gen_id:
                        await session.execute(
                            update(IndexGeneration)
                            .where(
                                IndexGeneration.organization_id == organization_id,
                                IndexGeneration.id == old_active_gen_id,
                                IndexGeneration.status == "ACTIVE",
                            )
                            .values(status="RETIRED")
                        )
                        # 更新 metrics_json 精确记录切换前的真实活跃代次，保证并发场景回滚精确
                        await session.execute(
                            update(IndexGeneration)
                            .where(
                                IndexGeneration.organization_id == organization_id,
                                IndexGeneration.id == new_gen_id,
                            )
                            .values(
                                metrics_json={
                                    "document_version_id": str(document_version_id),
                                    "previous_generation_id": str(old_active_gen_id),
                                    "chunk_count": len(index_items),
                                }
                            )
                        )
                    pointer.embedding_profile_id = profile_id
                    pointer.index_generation_id = new_gen_id
                    pointer.revision += 1
                    active_rev = pointer.revision

                await session.execute(
                    update(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == new_gen_id,
                    )
                    .values(status="ACTIVE")
                )
                await session.execute(
                    update(IndexJob)
                    .where(
                        IndexJob.organization_id == organization_id,
                        IndexJob.id == index_job_id,
                    )
                    .values(status="ACTIVE", progress=100)
                )
                if child_records_for_indexing:
                    indexed_chunk_ids = [cid for cid, _, _, _ in child_records_for_indexing]
                    await session.execute(
                        update(Chunk)
                        .where(
                            Chunk.organization_id == organization_id,
                            Chunk.id.in_(indexed_chunk_ids),
                        )
                        .values(indexed_at=now)
                    )

                record_audit_event(
                    session,
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    action="index.generation.activated",
                    resource_type="index_generation",
                    resource_id=new_gen_id,
                    details={
                        "space_id": str(space_id),
                        "document_version_id": str(document_version_id),
                        "generation_no": next_gen_no,
                        "pointer_revision": active_rev,
                        "chunk_count": len(index_items),
                    },
                )

        except PolicyBlockedError as exc:
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == new_gen_id,
                    )
                    .values(status="FAILED")
                )
                await session.execute(
                    update(IndexJob)
                    .where(
                        IndexJob.organization_id == organization_id,
                        IndexJob.id == index_job_id,
                    )
                    .values(
                        status="FAILED",
                        error_code=exc.code,
                        error_message=exc.message[:512],
                    )
                )
                record_security_audit_event(
                    session,
                    organization_id=organization_id,
                    actor_user_id=actor_user_id,
                    event_type="embedding.policy_blocked",
                    severity="HIGH",
                    resource_type="document_version",
                    resource_id=document_version_id,
                    details={"reason_code": exc.code, "sensitivity_level": sensitivity_str},
                )
            if raise_on_policy_blocked:
                raise
            return PipelineExecutionResult(
                organization_id=organization_id,
                document_id=doc_id,
                document_version_id=document_version_id,
                space_id=space_id,
                status="BLOCKED_POLICY",
                parse_job_id=parse_job_id,
                parse_status=ParseJobState.SUCCEEDED.value,
                chunk_set_id=chunk_set_id,
                chunk_count=len(chunk_output.parents) + len(chunk_output.children),
                parent_chunk_count=len(chunk_output.parents),
                child_chunk_count=len(chunk_output.children),
                index_generation_id=new_gen_id,
                index_generation_no=next_gen_no,
                collection_name=collection_name,
                error_code=exc.code,
                error_detail=exc.message,
            )
        except Exception as exc:
            async with org_rls_session(session, organization_id, user_id=actor_user_id):
                await session.execute(
                    update(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == new_gen_id,
                    )
                    .values(status="FAILED")
                )
                await session.execute(
                    update(IndexJob)
                    .where(
                        IndexJob.organization_id == organization_id,
                        IndexJob.id == index_job_id,
                    )
                    .values(
                        status="FAILED",
                        error_code="INDEX_BUILD_FAILED",
                        error_message=str(exc)[:512],
                    )
                )
            raise

        return PipelineExecutionResult(
            organization_id=organization_id,
            document_id=doc_id,
            document_version_id=document_version_id,
            space_id=space_id,
            status="SUCCEEDED",
            parse_job_id=parse_job_id,
            parse_status=ParseJobState.SUCCEEDED.value,
            chunk_set_id=chunk_set_id,
            chunk_count=len(chunk_output.parents) + len(chunk_output.children),
            parent_chunk_count=len(chunk_output.parents),
            child_chunk_count=len(chunk_output.children),
            index_generation_id=new_gen_id,
            index_generation_no=next_gen_no,
            active_pointer_revision=active_rev,
            collection_name=collection_name,
        )

    async def _collect_other_space_chunks(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        space_id: uuid.UUID,
        exclude_document_id: uuid.UUID,
    ) -> list[
        tuple[
            uuid.UUID,
            uuid.UUID,
            uuid.UUID,
            uuid.UUID | None,
            str,
            int,
            ACLProjectionPayload,
            str,
        ]
    ]:
        """收集同 Space 内其他非归档文档的最新已切片 ChildChunk，保证蓝绿新代次完整无损。"""
        async with org_rls_session(session, organization_id):
            other_docs = (
                (
                    await session.execute(
                        select(Document).where(
                            Document.organization_id == organization_id,
                            Document.space_id == space_id,
                            Document.id != exclude_document_id,
                            Document.status != "ARCHIVED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            if not other_docs:
                return []

            collected: list[
                tuple[
                    uuid.UUID,
                    uuid.UUID,
                    uuid.UUID,
                    uuid.UUID | None,
                    str,
                    int,
                    ACLProjectionPayload,
                    str,
                ]
            ] = []
            for o_doc in other_docs:
                target_ver_id = o_doc.current_version_id
                if target_ver_id is None:
                    target_ver_id = (
                        await session.execute(
                            select(DocumentVersion.id)
                            .where(
                                DocumentVersion.organization_id == organization_id,
                                DocumentVersion.document_id == o_doc.id,
                            )
                            .order_by(DocumentVersion.version_no.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                if target_ver_id is None:
                    continue

                latest_cs = (
                    await session.execute(
                        select(ChunkSet)
                        .where(
                            ChunkSet.organization_id == organization_id,
                            ChunkSet.document_version_id == target_ver_id,
                            ChunkSet.status == "SUCCEEDED",
                        )
                        .order_by(ChunkSet.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if latest_cs is None:
                    continue

                o_proj = await self._acl_engine.get_document_acl_projection(
                    session,
                    organization_id=organization_id,
                    document_id=o_doc.id,
                )
                o_acl_payload = ACLProjectionPayload(
                    acl_global=o_proj.acl_global,
                    acl_dept_ids=o_proj.acl_dept_ids,
                    acl_role_ids=o_proj.acl_role_ids,
                    acl_user_ids=o_proj.acl_user_ids,
                )

                chunks = (
                    (
                        await session.execute(
                            select(Chunk)
                            .where(
                                Chunk.organization_id == organization_id,
                                Chunk.chunk_set_id == latest_cs.id,
                                Chunk.parent_chunk_id.is_not(None),
                            )
                            .order_by(Chunk.sequence.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
                for ch in chunks:
                    collected.append(
                        (
                            ch.id,
                            o_doc.id,
                            target_ver_id,
                            ch.parent_chunk_id,
                            o_doc.sensitivity_level,
                            o_proj.acl_revision,
                            o_acl_payload,
                            ch.content_text,
                        )
                    )

            return collected

    async def rollback_active_index(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        space_id: uuid.UUID,
        expected_revision: int,
        target_generation_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        reason: str = "manual_blue_green_rollback",
    ) -> ActiveIndexRollbackResult:
        """带乐观锁校验的蓝绿索引回滚：将 ActiveIndexPointer 原子切回上一代次或指定目标代次。"""
        async with org_rls_session(session, organization_id, user_id=actor_user_id):
            pointer = (
                await session.execute(
                    select(ActiveIndexPointer)
                    .where(
                        ActiveIndexPointer.organization_id == organization_id,
                        ActiveIndexPointer.scope_type == "KNOWLEDGE_SPACE",
                        ActiveIndexPointer.scope_id == space_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if pointer is None:
                raise NotFoundError(
                    code="ACTIVE_INDEX_POINTER_NOT_FOUND",
                    message="当前知识空间尚未初始化激活索引指针",
                )
            if pointer.revision != expected_revision:
                raise ConflictError(
                    code="INDEX_POINTER_REVISION_CONFLICT",
                    message=(
                        f"索引指针乐观锁冲突（期望 revision={expected_revision}，"
                        f"当前 revision={pointer.revision}）"
                    ),
                    detail={
                        "expected_revision": expected_revision,
                        "current_revision": pointer.revision,
                    },
                )

            current_gen = (
                await session.execute(
                    select(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == pointer.index_generation_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if current_gen is None:
                raise NotFoundError(
                    code="CURRENT_GENERATION_NOT_FOUND",
                    message="当前激活的索引代次记录不存在",
                )

            resolved_target_id = target_generation_id
            if resolved_target_id is None:
                prev_raw = (
                    current_gen.metrics_json.get("previous_generation_id")
                    if isinstance(current_gen.metrics_json, dict)
                    else None
                )
                if not prev_raw:
                    raise ConflictError(
                        code="NO_PREVIOUS_GENERATION",
                        message="当前索引代次无历史上一代次可回滚",
                    )
                resolved_target_id = uuid.UUID(str(prev_raw))

            if resolved_target_id == current_gen.id:
                raise ConflictError(
                    code="ROLLBACK_SAME_GENERATION",
                    message="目标回滚代次与当前激活代次相同",
                )

            target_gen = (
                await session.execute(
                    select(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == resolved_target_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if target_gen is None:
                raise NotFoundError(
                    code="TARGET_GENERATION_NOT_FOUND",
                    message="指定的回滚目标索引代次不存在",
                )
            if target_gen.status == "FAILED":
                raise ConflictError(
                    code="TARGET_GENERATION_FAILED",
                    message="目标索引代次构建失败，禁止回滚至失败代次",
                )

            current_gen.status = "ROLLED_BACK"
            target_gen.status = "ACTIVE"
            pointer.index_generation_id = target_gen.id
            pointer.embedding_profile_id = target_gen.embedding_profile_id
            pointer.revision += 1

            record_audit_event(
                session,
                organization_id=organization_id,
                actor_user_id=actor_user_id,
                action="index.generation.rolled_back",
                resource_type="active_index_pointer",
                resource_id=pointer.id,
                details={
                    "space_id": str(space_id),
                    "from_generation_id": str(current_gen.id),
                    "to_generation_id": str(target_gen.id),
                    "new_revision": pointer.revision,
                    "reason": reason,
                },
            )

            return ActiveIndexRollbackResult(
                organization_id=organization_id,
                space_id=space_id,
                rolled_back_from_generation_id=current_gen.id,
                active_generation_id=target_gen.id,
                active_generation_no=target_gen.generation_no,
                pointer_revision=pointer.revision,
                collection_name=target_gen.collection_name,
            )

    async def cleanup_retired_generation(
        self,
        session: AsyncSession,
        *,
        organization_id: uuid.UUID,
        generation_id: uuid.UUID,
    ) -> bool:
        """清理已退休/回滚且不再被任何 ActiveIndexPointer 指向的 Qdrant 历史代次点位。"""
        async with org_rls_session(session, organization_id):
            gen = (
                await session.execute(
                    select(IndexGeneration).where(
                        IndexGeneration.organization_id == organization_id,
                        IndexGeneration.id == generation_id,
                    )
                )
            ).scalar_one_or_none()
            if gen is None:
                raise NotFoundError(
                    code="GENERATION_NOT_FOUND",
                    message="目标索引代次不存在",
                )
            active_ptr = (
                await session.execute(
                    select(ActiveIndexPointer.id).where(
                        ActiveIndexPointer.organization_id == organization_id,
                        ActiveIndexPointer.index_generation_id == generation_id,
                    )
                )
            ).scalar_one_or_none()
            if active_ptr is not None or gen.status == "ACTIVE":
                raise ConflictError(
                    code="CANNOT_CLEANUP_ACTIVE_GENERATION",
                    message="禁止清理当前仍处于激活状态的索引代次",
                )
            collection_name = gen.collection_name

        await self._qdrant.delete_generation_points(
            collection_name,
            organization_id=str(organization_id),
            index_generation_id=str(generation_id),
        )
        return True
