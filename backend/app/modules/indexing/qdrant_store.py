"""Qdrant 混合检索与索引存储引擎（QdrantIndexStore）。

安全与架构红线（TECHNICAL_SPEC §6.5 / §8.4 / §8.5 & ADR-001）：
1. 按 `EmbeddingProfile` 创建共享 Collection，配置：
   - Named Dense Vector：`dense`（Cosine 距离）
   - Named Sparse Vector：`bm25_sparse`（IDF 修饰器，点积评分）
   并为租户、空间、文档、索引代次与 ACL 预过滤标签建立 Payload 索引。
2. `search_hybrid` 强制无条件注入 `must: [organization_id, space_id, index_generation_id]`；
   任意一项缺失直接抛出 `ValidationError`，严禁无过滤裸查。
3. 防越权与防正文泄漏强约束：
   - Qdrant 检索仅返回安全候选元数据（`chunk_id`、`document_id`、`document_version_id`、
     `parent_chunk_id`、`space_id`、`acl_revision`、`rank`、`score`）；
   - 严禁在候选检索阶段透传任何文档正文文本给 Reranker 或 LLM；
     所有正文必须先经过 PostgreSQL 权威 ACL 复核后再按需加载。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient, models

from app.core.config import Settings, get_settings
from app.core.errors import ValidationError
from app.modules.embeddings.sparse import ChineseSparseVectorizer

logger = logging.getLogger(__name__)

# Named Vector 规范名称（全系统统一契约）
DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "bm25_sparse"

# Qdrant 检索阶段仅允许返回的安全候选元数据键（绝不包含任何文档正文或敏感标题）
SAFE_CANDIDATE_PAYLOAD_KEYS: tuple[str, ...] = (
    "chunk_id",
    "organization_id",
    "space_id",
    "document_id",
    "document_version_id",
    "parent_chunk_id",
    "index_generation_id",
    "acl_revision",
    "sensitivity_level",
    "language",
)


class ACLProjectionPayload(BaseModel):
    """同步至 Qdrant Payload 的轻量 ACL 预过滤投影（仅用于预过滤，绝不替代 PG 权威鉴权）。"""

    acl_global: bool = Field(default=False, description="是否对当前 Space 全体有效成员开放")
    acl_dept_ids: list[str] = Field(default_factory=list, description="授权部门 ID 列表")
    acl_role_ids: list[str] = Field(default_factory=list, description="授权组织角色 ID 列表")
    acl_user_ids: list[str] = Field(default_factory=list, description="显式授权用户 ID 列表")


class ACLPrefilter(BaseModel):
    """检索端可选 ACL 预过滤条件（用于减少明显无权候选，最终仍由 PostgreSQL 权威复核）。"""

    user_id: str | None = Field(default=None, description="当前用户 ID")
    department_ids: list[str] = Field(default_factory=list, description="当前用户有效部门 ID 集合")
    role_ids: list[str] = Field(default_factory=list, description="当前用户有效组织角色 ID 集合")
    include_global: bool = Field(default=True, description="是否匹配 Space 内 GLOBAL 授权")


class QdrantChunkPoint(BaseModel):
    """写入 Qdrant 的单条 Chunk 向量与受控 Payload 契约。"""

    chunk_id: str = Field(..., min_length=1, description="切片唯一 ID（PostgreSQL chunks.id）")
    point_id: str | None = Field(
        default=None,
        description="可选 Qdrant Point UUID；缺省时由 chunk_id 与 index_generation_id 确定性派生",
    )
    organization_id: str = Field(..., min_length=1, description="所属组织 ID（租户隔离必选）")
    space_id: str = Field(..., min_length=1, description="所属知识空间 ID（与 ADR-001 命名一致）")
    document_id: str = Field(..., min_length=1, description="所属文档 ID")
    document_version_id: str = Field(..., min_length=1, description="所属文档版本 ID")
    parent_chunk_id: str | None = Field(default=None, description="父切片 ID")
    index_generation_id: str = Field(..., min_length=1, description="蓝绿索引代次 ID")
    acl_revision: int = Field(default=1, ge=0, description="写入时的 ACL 修订号")
    sensitivity_level: str = Field(default="L0", description="敏感等级（L0/L1/L2/L3）")
    language: str = Field(default="zh-CN", description="内容语言标识")
    dense_vector: list[float] = Field(..., min_length=1, description="稠密语义向量")
    sparse_indices: list[int] | None = Field(default=None, description="可选稀疏向量词槽索引")
    sparse_values: list[float] | None = Field(default=None, description="可选稀疏向量权重值")
    text_for_sparse: str | None = Field(
        default=None,
        description="可选用于自动生成中文 BM25 稀疏向量的文本（绝不从检索结果透传）",
    )
    acl_projection: ACLProjectionPayload = Field(
        default_factory=ACLProjectionPayload,
        description="可选 ACL 预过滤投影标签",
    )

    def resolve_point_id(self) -> str:
        """生成稳定合法的 Qdrant UUID Point ID，保证蓝绿代次间互不覆盖且重试幂等。"""
        if self.point_id:
            return self.point_id
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.index_generation_id}:{self.chunk_id}"))


class QdrantCandidateHit(BaseModel):
    """Qdrant 混合检索返回的安全候选条目（严格不含正文文本）。"""

    chunk_id: str = Field(..., description="候选切片 ID")
    document_id: str = Field(..., description="所属文档 ID")
    document_version_id: str = Field(..., description="所属文档版本 ID")
    parent_chunk_id: str | None = Field(default=None, description="父切片 ID")
    space_id: str = Field(..., description="所属知识空间 ID")
    acl_revision: int = Field(default=0, description="索引中记录的 ACL 修订号")
    rank: int = Field(..., ge=1, description="当前 Space 内 RRF 排名（1-based）")
    score: float = Field(..., description="当前 Space 内 RRF 融合得分")
    retrieval_source: str = Field(default="hybrid_rrf", description="召回来源标识")


def _require_non_empty(value: str, field_name: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValidationError(f"Qdrant 安全检索/写入缺少必填隔离字段：{field_name}")
    return cleaned


class QdrantIndexStore:
    """面向多租户知识库的 Qdrant 混合检索与蓝绿索引存储客户端。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: AsyncQdrantClient | None = None,
        sparse_vectorizer: ChineseSparseVectorizer | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._sparse = sparse_vectorizer or ChineseSparseVectorizer()
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            api_key = (
                self._settings.qdrant_api_key.get_secret_value().strip()
                if self._settings.qdrant_api_key is not None
                else None
            )
            self._client = AsyncQdrantClient(
                url=self._settings.qdrant_http_url,
                api_key=api_key or None,
                timeout=timeout_seconds,
            )

    async def close(self) -> None:
        """释放底层 Qdrant 异步连接池。"""
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> QdrantIndexStore:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def ensure_collection(
        self,
        collection_name: str,
        dense_dimensions: int,
    ) -> bool:
        """按 EmbeddingProfile 幂等创建共享 Collection 与全部必需 Payload 索引。

        Returns:
            True 表示新建了 Collection，False 表示已存在并完成索引检查。
        """
        _require_non_empty(collection_name, "collection_name")
        if dense_dimensions <= 0:
            raise ValidationError("dense_dimensions 必须为正整数")

        exists = await self._client.collection_exists(collection_name=collection_name)
        created = False
        if not exists:
            await self._client.create_collection(
                collection_name=collection_name,
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=dense_dimensions,
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    SPARSE_VECTOR_NAME: models.SparseVectorParams(
                        modifier=models.Modifier.IDF,
                    )
                },
            )
            created = True

        # 为租户、空间、文档、索引代次与四维 ACL 预过滤标签建立 Payload 索引
        payload_indexes: tuple[tuple[str, models.PayloadSchemaType], ...] = (
            ("organization_id", models.PayloadSchemaType.KEYWORD),
            ("space_id", models.PayloadSchemaType.KEYWORD),
            ("document_id", models.PayloadSchemaType.KEYWORD),
            ("document_version_id", models.PayloadSchemaType.KEYWORD),
            ("index_generation_id", models.PayloadSchemaType.KEYWORD),
            ("acl_revision", models.PayloadSchemaType.INTEGER),
            ("acl_global", models.PayloadSchemaType.BOOL),
            ("acl_dept_ids", models.PayloadSchemaType.KEYWORD),
            ("acl_role_ids", models.PayloadSchemaType.KEYWORD),
            ("acl_user_ids", models.PayloadSchemaType.KEYWORD),
        )
        for field_name, schema_type in payload_indexes:
            await self._client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=schema_type,
                wait=True,
            )

        return created

    async def upsert_chunks(
        self,
        collection_name: str,
        points: Sequence[QdrantChunkPoint],
        *,
        batch_size: int = 100,
        wait: bool = True,
    ) -> int:
        """批量写入 Chunk 的 Dense + 中文 Sparse 向量与受控 Payload。"""
        _require_non_empty(collection_name, "collection_name")
        if not points:
            return 0

        qdrant_points: list[models.PointStruct] = []
        for item in points:
            org_id = _require_non_empty(item.organization_id, "organization_id")
            space_id = _require_non_empty(item.space_id, "space_id")
            doc_id = _require_non_empty(item.document_id, "document_id")
            ver_id = _require_non_empty(item.document_version_id, "document_version_id")
            gen_id = _require_non_empty(item.index_generation_id, "index_generation_id")
            chunk_id = _require_non_empty(item.chunk_id, "chunk_id")

            # 若调用方未直接传入稀疏向量且提供了 text_for_sparse，则通过中文 BM25 编码器生成
            if item.sparse_indices is not None and item.sparse_values is not None:
                sparse_vec = models.SparseVector(
                    indices=list(item.sparse_indices),
                    values=list(item.sparse_values),
                )
            elif item.text_for_sparse:
                sparse_vec = self._sparse.encode_document(item.text_for_sparse)
            else:
                sparse_vec = models.SparseVector(indices=[], values=[])

            vectors: dict[str, Any] = {
                DENSE_VECTOR_NAME: list(item.dense_vector),
            }
            if sparse_vec.indices:
                vectors[SPARSE_VECTOR_NAME] = sparse_vec

            payload: dict[str, Any] = {
                "organization_id": org_id,
                "space_id": space_id,
                "document_id": doc_id,
                "document_version_id": ver_id,
                "chunk_id": chunk_id,
                "parent_chunk_id": item.parent_chunk_id,
                "index_generation_id": gen_id,
                "acl_revision": item.acl_revision,
                "sensitivity_level": item.sensitivity_level,
                "language": item.language,
                "acl_global": item.acl_projection.acl_global,
                "acl_dept_ids": list(item.acl_projection.acl_dept_ids),
                "acl_role_ids": list(item.acl_projection.acl_role_ids),
                "acl_user_ids": list(item.acl_projection.acl_user_ids),
            }
            qdrant_points.append(
                models.PointStruct(
                    id=item.resolve_point_id(),
                    vector=vectors,
                    payload=payload,
                )
            )

        effective_batch = max(1, batch_size)
        for offset in range(0, len(qdrant_points), effective_batch):
            batch = qdrant_points[offset : offset + effective_batch]
            await self._client.upsert(
                collection_name=collection_name,
                points=batch,
                wait=wait,
            )

        return len(qdrant_points)

    @staticmethod
    def _build_mandatory_filter(
        *,
        organization_id: str,
        space_id: str,
        index_generation_id: str,
        acl_prefilter: ACLPrefilter | None = None,
    ) -> models.Filter:
        """构造强制租户/空间/代次硬过滤条件，并可选叠加 ACL 预过滤 `should` 子句。"""
        org_id = _require_non_empty(organization_id, "organization_id")
        sp_id = _require_non_empty(space_id, "space_id")
        gen_id = _require_non_empty(index_generation_id, "index_generation_id")

        must_conditions: list[models.Condition] = [
            models.FieldCondition(
                key="organization_id",
                match=models.MatchValue(value=org_id),
            ),
            models.FieldCondition(
                key="space_id",
                match=models.MatchValue(value=sp_id),
            ),
            models.FieldCondition(
                key="index_generation_id",
                match=models.MatchValue(value=gen_id),
            ),
        ]

        if acl_prefilter is not None:
            should_clauses: list[models.Condition] = []
            if acl_prefilter.include_global:
                should_clauses.append(
                    models.FieldCondition(
                        key="acl_global",
                        match=models.MatchValue(value=True),
                    )
                )
            if acl_prefilter.user_id:
                should_clauses.append(
                    models.FieldCondition(
                        key="acl_user_ids",
                        match=models.MatchValue(value=acl_prefilter.user_id),
                    )
                )
            if acl_prefilter.department_ids:
                should_clauses.append(
                    models.FieldCondition(
                        key="acl_dept_ids",
                        match=models.MatchAny(any=list(acl_prefilter.department_ids)),
                    )
                )
            if acl_prefilter.role_ids:
                should_clauses.append(
                    models.FieldCondition(
                        key="acl_role_ids",
                        match=models.MatchAny(any=list(acl_prefilter.role_ids)),
                    )
                )
            if should_clauses:
                must_conditions.append(models.Filter(should=should_clauses))

        return models.Filter(must=must_conditions)

    async def search_hybrid(
        self,
        collection_name: str,
        *,
        organization_id: str,
        space_id: str,
        index_generation_id: str,
        query_dense: Sequence[float],
        query_sparse: models.SparseVector | None = None,
        top_k: int = 20,
        prefetch_limit: int | None = None,
        acl_prefilter: ACLPrefilter | None = None,
    ) -> list[QdrantCandidateHit]:
        """执行单 Space 内 Dense + Sparse Prefetch 与 RRF 融合检索，仅返回安全候选元数据。"""
        _require_non_empty(collection_name, "collection_name")
        if not query_dense:
            raise ValidationError("query_dense 不能为空")
        if top_k <= 0:
            raise ValidationError("top_k 必须大于 0")

        q_filter = self._build_mandatory_filter(
            organization_id=organization_id,
            space_id=space_id,
            index_generation_id=index_generation_id,
            acl_prefilter=acl_prefilter,
        )
        pf_limit = max(top_k, prefetch_limit or (top_k * 2))

        prefetches: list[models.Prefetch] = [
            models.Prefetch(
                query=list(query_dense),
                using=DENSE_VECTOR_NAME,
                filter=q_filter,
                limit=pf_limit,
            )
        ]
        has_sparse = bool(query_sparse is not None and query_sparse.indices)
        if has_sparse and query_sparse is not None:
            prefetches.append(
                models.Prefetch(
                    query=query_sparse,
                    using=SPARSE_VECTOR_NAME,
                    filter=q_filter,
                    limit=pf_limit,
                )
            )

        # 显式限定 with_payload 仅返回安全元数据白名单，绝不读取或透传任何正文文本
        payload_selector = models.PayloadSelectorInclude(include=list(SAFE_CANDIDATE_PAYLOAD_KEYS))
        response = await self._client.query_points(
            collection_name=collection_name,
            prefetch=prefetches,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=top_k,
            with_payload=payload_selector,
            with_vectors=False,
        )

        hits: list[QdrantCandidateHit] = []
        for rank, point in enumerate(response.points, start=1):
            payload = point.payload or {}
            # 再次做租户与空间防御性核验，杜绝任何异常 Payload 越界
            if (
                payload.get("organization_id") != organization_id
                or payload.get("space_id") != space_id
                or payload.get("index_generation_id") != index_generation_id
            ):
                logger.warning("Qdrant 返回点位与查询租户/空间/代次不符，已丢弃")
                continue

            chunk_id = str(payload.get("chunk_id") or "")
            doc_id = str(payload.get("document_id") or "")
            ver_id = str(payload.get("document_version_id") or "")
            if not chunk_id or not doc_id or not ver_id:
                continue

            hits.append(
                QdrantCandidateHit(
                    chunk_id=chunk_id,
                    document_id=doc_id,
                    document_version_id=ver_id,
                    parent_chunk_id=(
                        str(payload["parent_chunk_id"])
                        if payload.get("parent_chunk_id") is not None
                        else None
                    ),
                    space_id=space_id,
                    acl_revision=int(payload.get("acl_revision") or 0),
                    rank=rank,
                    score=float(point.score),
                    retrieval_source="hybrid_rrf" if has_sparse else "dense_rrf",
                )
            )
        return hits

    async def update_document_acl_payload(
        self,
        collection_name: str,
        *,
        organization_id: str,
        document_id: str,
        acl_revision: int,
        acl_projection: ACLProjectionPayload,
        wait: bool = True,
    ) -> None:
        """响应 Outbox ACL 变更事件，异步更新目标文档全部 Chunk 的 Qdrant 预过滤 Payload。"""
        _require_non_empty(collection_name, "collection_name")
        org_id = _require_non_empty(organization_id, "organization_id")
        doc_id = _require_non_empty(document_id, "document_id")

        doc_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="organization_id",
                    match=models.MatchValue(value=org_id),
                ),
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=doc_id),
                ),
            ]
        )
        await self._client.set_payload(
            collection_name=collection_name,
            payload={
                "acl_revision": int(acl_revision),
                "acl_global": acl_projection.acl_global,
                "acl_dept_ids": list(acl_projection.acl_dept_ids),
                "acl_role_ids": list(acl_projection.acl_role_ids),
                "acl_user_ids": list(acl_projection.acl_user_ids),
            },
            points=models.FilterSelector(filter=doc_filter),
            wait=wait,
        )

    async def delete_generation_points(
        self,
        collection_name: str,
        *,
        organization_id: str,
        index_generation_id: str,
        wait: bool = True,
    ) -> None:
        """按组织与索引代次精准删除退休蓝绿代次的全部向量点位。"""
        _require_non_empty(collection_name, "collection_name")
        org_id = _require_non_empty(organization_id, "organization_id")
        gen_id = _require_non_empty(index_generation_id, "index_generation_id")

        gen_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="organization_id",
                    match=models.MatchValue(value=org_id),
                ),
                models.FieldCondition(
                    key="index_generation_id",
                    match=models.MatchValue(value=gen_id),
                ),
            ]
        )
        await self._client.delete(
            collection_name=collection_name,
            points_selector=models.FilterSelector(filter=gen_filter),
            wait=wait,
        )

    async def delete_collection(self, collection_name: str) -> bool:
        """删除指定 Collection（用于运维清理与集成测试回收）。"""
        _require_non_empty(collection_name, "collection_name")
        if not await self._client.collection_exists(collection_name=collection_name):
            return False
        await self._client.delete_collection(collection_name=collection_name)
        return True
