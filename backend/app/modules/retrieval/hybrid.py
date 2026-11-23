"""多 Space 权威鉴权混合检索编排器（MultiSpaceHybridRetriever）与上下文组装器（ContextBuilder）。

不可绕过的安全执行顺序（PRD §5.1 & TECHNICAL_SPEC §8.3 / §9.2）：
1. 按 Space 绑定的 `EmbeddingProfile` 分组，经 `EmbeddingRouter` 与 `ChineseSparseVectorizer`
   分别生成对应维度的 Query 稠密向量与中文稀疏向量；
2. 每 Space 在对应 Collection 独立执行 Qdrant Dense + Sparse Prefetch 与 Space 内 RRF，
   仅产出候选 `chunk_id` 与安全元数据；
3. 调用权威授权回调 `authorize_chunks_fn(candidate_chunk_ids)`（对应 PostgreSQL 最终 ACL 复核），
   立即丢弃全部未授权候选，并记录脱敏的 `RetrievalAuditTrace`（绝不记录未授权正文或标题）；
4. 仅对已授权 `chunk_id` 调用正文加载回调 `load_authorized_chunks_fn(allowed_chunk_ids)`；
5. 仅对已授权正文按 Space 调用 `RerankerRouter`（严格校验本批最高敏感等级外发策略）；
6. 跨 Space 仅采用基于排名的 RRF 融合（`1 / (k + rank)`），严禁直接比较异构模型的原始分值；
7. `ContextBuilder` 按 Token 预算与来源多样性组装带引用编号与 `<untrusted_evidence>`
   防提示注入隔离标签的最终上下文。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import ValidationError
from app.modules.chunking.chunker import estimate_token_count
from app.modules.embeddings.providers import EmbeddingProfile, EmbeddingRouter
from app.modules.embeddings.sparse import ChineseSparseVectorizer
from app.modules.indexing.qdrant_store import (
    ACLPrefilter,
    QdrantCandidateHit,
    QdrantIndexStore,
)
from app.modules.providers.egress import SensitivityLevel
from app.modules.providers.llm import (
    AnswerPolicyMode,
    LLMMessage,
    UntrustedContextDocument,
    build_untrusted_rag_messages,
)
from app.modules.providers.reranker import RerankCandidate, RerankerRouter

logger = logging.getLogger(__name__)


class SpaceRetrievalTarget(BaseModel):
    """单个待检索 Knowledge Space 的索引与向量配置目标。"""

    space_id: str = Field(..., min_length=1, description="知识空间 ID")
    collection_name: str = Field(..., min_length=1, description="对应的 Qdrant 共享 Collection 名")
    index_generation_id: str = Field(..., min_length=1, description="当前激活索引代次 ID")
    embedding_profile_id: str = Field(
        ..., min_length=1, description="该 Space 绑定的 Embedding Profile ID"
    )
    embedding_profile: EmbeddingProfile | None = Field(
        default=None,
        description="可选覆盖 Embedding 参数（如模型与维度）",
    )
    query_sensitivity_level: SensitivityLevel = Field(
        default=SensitivityLevel.L0,
        description="查询向量化时的敏感等级基线",
    )
    acl_prefilter: ACLPrefilter | None = Field(
        default=None,
        description="可选 Qdrant ACL 预过滤（仅优化召回，绝不替代 PG 权威复核）",
    )
    top_k: int = Field(default=20, ge=1, le=200, description="单 Space 召回候选上限")


class ChunkAuthorizationResult(BaseModel):
    """权威 PostgreSQL ACL 批量复核结果契约。"""

    allowed_chunk_ids: set[str] = Field(
        default_factory=set, description="通过权威鉴权的切片 ID 集合"
    )
    denied_chunk_ids: set[str] = Field(default_factory=set, description="被拒绝的切片 ID 集合")
    doc_acl_revisions: dict[str, int] = Field(
        default_factory=dict,
        description="已授权文档对应的当前权威 ACL Revision 快照",
    )
    reason_codes: dict[str, str] = Field(
        default_factory=dict,
        description="按 chunk_id 记录的机器可读判定码（仅用于安全审计，不面向普通用户暴露）",
    )


class AuthorizedChunkBody(BaseModel):
    """仅在通过 PostgreSQL 权威 ACL 复核后才允许加载的切片正文对象。"""

    chunk_id: str = Field(..., description="切片 ID")
    document_id: str = Field(..., description="文档 ID")
    document_version_id: str = Field(..., description="文档版本 ID")
    space_id: str = Field(..., description="所属知识空间 ID")
    document_title: str = Field(..., description="已授权文档标题")
    content: str = Field(..., description="已授权切片正文（仍视作不可信数据）")
    parent_chunk_id: str | None = Field(default=None, description="可选父切片 ID")
    parent_content: str | None = Field(default=None, description="可选父切片扩展上下文")
    page_number: int | None = Field(default=None, ge=1, description="可选 1-based 页码")
    heading_path: list[str] = Field(default_factory=list, description="章节路径")
    block_id: str | None = Field(default=None, description="来源结构块 ID")
    bounding_box: list[float] | dict[str, Any] | None = Field(
        default=None, description="坐标定位框"
    )
    sensitivity_level: SensitivityLevel = Field(
        default=SensitivityLevel.L0,
        description="来源文档敏感等级",
    )
    acl_revision: int = Field(default=1, ge=0, description="当前权威 ACL Revision")
    metadata: dict[str, Any] = Field(default_factory=dict, description="扩展安全元数据")


class FusedEvidenceItem(BaseModel):
    """跨 Space 排名 RRF 融合后的已授权证据条目。"""

    citation_id: str = Field(..., description="面向回答引用的稳定编号（如 C1）")
    chunk: AuthorizedChunkBody = Field(..., description="已授权切片正文与溯源信息")
    space_id: str = Field(..., description="来源 Space ID")
    per_space_rank: int = Field(..., ge=1, description="单 Space 重排后排名（1-based）")
    rrf_score: float = Field(..., description="跨 Space 排名 RRF 融合分")
    rerank_score: float | None = Field(default=None, description="单 Space 内重排相关性分")


class RetrievalAuditTrace(BaseModel):
    """脱敏后的检索审计记录（严禁包含任何未授权文档正文、标题或路径）。"""

    query_hash: str = Field(..., description="原始 Query 的 SHA-256 哈希")
    organization_id: str = Field(..., description="所属组织 ID")
    space_ids: list[str] = Field(default_factory=list, description="本次检索的 Space 列表")
    candidate_chunk_ids: list[str] = Field(default_factory=list, description="Qdrant 召回候选 ID")
    authorized_chunk_ids: list[str] = Field(
        default_factory=list, description="通过 PG ACL 复核的 ID"
    )
    denied_count: int = Field(default=0, ge=0, description="被权威 ACL 拦截丢弃的候选总数")
    per_space_candidate_counts: dict[str, int] = Field(
        default_factory=dict,
        description="各 Space 初始召回数量",
    )
    per_space_authorized_counts: dict[str, int] = Field(
        default_factory=dict,
        description="各 Space 授权通过数量",
    )
    rerank_providers: dict[str, str] = Field(
        default_factory=dict,
        description="各 Space 实际执行的 Reranker Provider",
    )
    latency_ms: float = Field(..., ge=0.0, description="检索编排总耗时（毫秒）")


class BuiltContextResult(BaseModel):
    """ContextBuilder 输出的防注入隔离上下文与预算统计。"""

    evidences: list[UntrustedContextDocument] = Field(
        default_factory=list,
        description="进入 Prompt 的不可信证据文档列表",
    )
    messages: list[LLMMessage] = Field(
        default_factory=list,
        description="已包裹 `<untrusted_evidence>` 隔离标签的完整对话消息",
    )
    selected_items: list[FusedEvidenceItem] = Field(
        default_factory=list,
        description="最终入选的融合证据条目",
    )
    total_tokens: int = Field(default=0, ge=0, description="入选证据累计估算 Token 数")
    dropped_count: int = Field(default=0, ge=0, description="因 Token 预算或多样性上限被截断的数量")


class MultiSpaceRetrievalResult(BaseModel):
    """多 Space 鉴权混合检索完整返回结构。"""

    items: list[FusedEvidenceItem] = Field(
        default_factory=list, description="融合排序后的全部已授权证据"
    )
    context: BuiltContextResult = Field(..., description="已按预算与防注入隔离组装的上下文")
    audit_trace: RetrievalAuditTrace = Field(..., description="脱敏检索审计记录")


# 权威 ACL 复核回调与已授权正文加载回调类型签名（支持同步与异步）
AuthorizeChunksFn = Callable[
    [list[str]],
    ChunkAuthorizationResult | set[str] | Awaitable[ChunkAuthorizationResult | set[str]],
]
LoadAuthorizedChunksFn = Callable[
    [list[str]],
    Sequence[AuthorizedChunkBody]
    | dict[str, AuthorizedChunkBody]
    | Awaitable[Sequence[AuthorizedChunkBody] | dict[str, AuthorizedChunkBody]],
]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ContextBuilder:
    """按 Token 预算与来源多样性组装 `<untrusted_evidence>` 防注入 RAG 上下文。"""

    def __init__(
        self,
        *,
        max_context_tokens: int = 2400,
        max_chunks_per_document: int = 3,
        max_chunks_per_space: int | None = None,
        prefer_parent_content: bool = False,
    ) -> None:
        if max_context_tokens < 64:
            raise ValidationError("max_context_tokens 不能小于 64")
        self.max_context_tokens = max_context_tokens
        self.max_chunks_per_document = max(1, max_chunks_per_document)
        self.max_chunks_per_space = max_chunks_per_space
        self.prefer_parent_content = prefer_parent_content

    def build(
        self,
        *,
        user_query: str,
        items: Sequence[FusedEvidenceItem],
        answer_policy: AnswerPolicyMode = AnswerPolicyMode.STRICT_EVIDENCE,
        conversation_history: Sequence[LLMMessage] | None = None,
        system_instructions: str | None = None,
    ) -> BuiltContextResult:
        """筛选满足 Token 预算与来源多样性的证据，并生成带 `<untrusted_evidence>` 标签的消息。"""
        selected: list[FusedEvidenceItem] = []
        evidences: list[UntrustedContextDocument] = []
        doc_counts: dict[str, int] = defaultdict(int)
        space_counts: dict[str, int] = defaultdict(int)
        used_tokens = 0
        dropped = 0

        for item in items:
            chunk = item.chunk
            if doc_counts[chunk.document_id] >= self.max_chunks_per_document:
                dropped += 1
                continue
            if (
                self.max_chunks_per_space is not None
                and space_counts[chunk.space_id] >= self.max_chunks_per_space
            ):
                dropped += 1
                continue

            body_text = (
                chunk.parent_content
                if (self.prefer_parent_content and chunk.parent_content)
                else chunk.content
            )
            item_tokens = estimate_token_count(body_text) + estimate_token_count(
                chunk.document_title
            )
            if selected and used_tokens + item_tokens > self.max_context_tokens:
                dropped += 1
                continue

            doc_counts[chunk.document_id] += 1
            space_counts[chunk.space_id] += 1
            used_tokens += item_tokens
            selected.append(item)
            evidences.append(
                UntrustedContextDocument(
                    citation_id=item.citation_id,
                    document_title=chunk.document_title,
                    content=body_text,
                    page_number=chunk.page_number,
                    heading_path=list(chunk.heading_path),
                    sensitivity_level=chunk.sensitivity_level,
                )
            )

        messages = build_untrusted_rag_messages(
            user_query=user_query,
            evidences=evidences,
            answer_policy=answer_policy,
            conversation_history=conversation_history,
            system_instructions=system_instructions,
        )
        return BuiltContextResult(
            evidences=evidences,
            messages=messages,
            selected_items=selected,
            total_tokens=used_tokens,
            dropped_count=dropped,
        )


class MultiSpaceHybridRetriever:
    """多 Space 鉴权混合检索编排器（严格保证未授权正文零进入 Reranker 与 ContextBuilder）。"""

    def __init__(
        self,
        *,
        qdrant_store: QdrantIndexStore,
        embedding_router: EmbeddingRouter,
        reranker_router: RerankerRouter,
        sparse_vectorizer: ChineseSparseVectorizer | None = None,
        context_builder: ContextBuilder | None = None,
        rrf_k: int = 60,
    ) -> None:
        if rrf_k <= 0:
            raise ValidationError("跨 Space RRF 常数 rrf_k 必须大于 0")
        self._qdrant = qdrant_store
        self._embedding_router = embedding_router
        self._reranker_router = reranker_router
        self._sparse = sparse_vectorizer or ChineseSparseVectorizer()
        self._context_builder = context_builder or ContextBuilder()
        self._rrf_k = rrf_k

    @staticmethod
    def _normalize_auth_result(
        raw_result: ChunkAuthorizationResult | set[str],
        candidate_ids: Sequence[str],
    ) -> ChunkAuthorizationResult:
        if isinstance(raw_result, ChunkAuthorizationResult):
            return raw_result
        allowed = set(raw_result) & set(candidate_ids)
        denied = set(candidate_ids) - allowed
        return ChunkAuthorizationResult(
            allowed_chunk_ids=allowed,
            denied_chunk_ids=denied,
        )

    @staticmethod
    def _normalize_loaded_chunks(
        raw_loaded: Sequence[AuthorizedChunkBody] | dict[str, AuthorizedChunkBody],
        allowed_ids: set[str],
    ) -> dict[str, AuthorizedChunkBody]:
        """将加载器返回值统一转为字典，并防御性剔除任何不在 `allowed_ids` 中的越权条目。"""
        result: dict[str, AuthorizedChunkBody] = {}
        if isinstance(raw_loaded, dict):
            for cid, body in raw_loaded.items():
                if cid in allowed_ids and body.chunk_id in allowed_ids:
                    result[body.chunk_id] = body
        else:
            for body in raw_loaded:
                if body.chunk_id in allowed_ids:
                    result[body.chunk_id] = body
        return result

    async def retrieve(
        self,
        *,
        query: str,
        organization_id: str,
        spaces: Sequence[SpaceRetrievalTarget],
        authorize_chunks_fn: AuthorizeChunksFn,
        load_authorized_chunks_fn: LoadAuthorizedChunksFn,
        org_allow_external_l1: bool = False,
        per_space_rerank_top_n: int = 10,
        final_top_k: int = 12,
        allow_rerank_rrf_fallback: bool = True,
        answer_policy: AnswerPolicyMode = AnswerPolicyMode.STRICT_EVIDENCE,
        conversation_history: Sequence[LLMMessage] | None = None,
        system_instructions: str | None = None,
    ) -> MultiSpaceRetrievalResult:
        """执行端到端 7 步安全混合检索与防注入上下文组装。"""
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValidationError("检索问题 query 不能为空")
        cleaned_org = organization_id.strip()
        if not cleaned_org:
            raise ValidationError("检索必须绑定有效的 organization_id")
        if not spaces:
            raise ValidationError("至少需要指定一个待检索的 Knowledge Space")

        start_ts = time.perf_counter()
        query_hash = hashlib.sha256(cleaned_query.encode("utf-8")).hexdigest()

        # --- 步骤 1：按 EmbeddingProfile 分组生成各 Profile 的 Dense 向量与中文 Sparse 向量 ---
        profile_groups: dict[str, list[SpaceRetrievalTarget]] = defaultdict(list)
        for target in spaces:
            profile_groups[target.embedding_profile_id].append(target)

        query_sparse = self._sparse.encode_query(cleaned_query)

        async def _embed_profile_group(
            profile_id: str, group_targets: list[SpaceRetrievalTarget]
        ) -> tuple[str, list[float]]:
            rep = group_targets[0]
            emb_result = await self._embedding_router.embed_query(
                cleaned_query,
                sensitivity_level=rep.query_sensitivity_level,
                org_allow_external_l1=org_allow_external_l1,
                profile=rep.embedding_profile,
                organization_id=cleaned_org,
            )
            if not emb_result.vectors:
                raise ValidationError(f"Embedding Profile {profile_id} 未返回有效查询向量")
            return profile_id, emb_result.vectors[0]

        embed_pairs = await asyncio.gather(
            *[_embed_profile_group(pid, grp) for pid, grp in profile_groups.items()]
        )
        profile_dense_map = dict(embed_pairs)

        # --- 步骤 2：各 Space 并行在对应 Collection 执行 Qdrant Dense + Sparse + Space 内 RRF ---
        async def _search_single_space(
            target: SpaceRetrievalTarget,
        ) -> tuple[str, list[QdrantCandidateHit]]:
            hits = await self._qdrant.search_hybrid(
                target.collection_name,
                organization_id=cleaned_org,
                space_id=target.space_id,
                index_generation_id=target.index_generation_id,
                query_dense=profile_dense_map[target.embedding_profile_id],
                query_sparse=query_sparse,
                top_k=target.top_k,
                acl_prefilter=target.acl_prefilter,
            )
            return target.space_id, hits

        space_hit_pairs = await asyncio.gather(*[_search_single_space(t) for t in spaces])
        per_space_hits: dict[str, list[QdrantCandidateHit]] = dict(space_hit_pairs)
        per_space_candidate_counts = {sid: len(hits) for sid, hits in per_space_hits.items()}

        # 汇总去重候选 ID（仅 ID，不含任何正文）
        ordered_candidate_ids: list[str] = []
        seen_candidates: set[str] = set()
        for target in spaces:
            for hit in per_space_hits.get(target.space_id, []):
                if hit.chunk_id not in seen_candidates:
                    seen_candidates.add(hit.chunk_id)
                    ordered_candidate_ids.append(hit.chunk_id)

        # --- 步骤 3：调用 PostgreSQL 权威 ACL 复核回调，丢弃全部未授权候选 ---
        if ordered_candidate_ids:
            raw_auth = await _maybe_await(authorize_chunks_fn(ordered_candidate_ids))
            auth_decision = self._normalize_auth_result(raw_auth, ordered_candidate_ids)
        else:
            auth_decision = ChunkAuthorizationResult()

        allowed_ids = auth_decision.allowed_chunk_ids & seen_candidates
        denied_count = len(seen_candidates - allowed_ids)
        ordered_allowed_ids = [cid for cid in ordered_candidate_ids if cid in allowed_ids]

        # 过滤各 Space 的授权候选列表
        per_space_authorized_hits: dict[str, list[QdrantCandidateHit]] = {}
        for target in spaces:
            per_space_authorized_hits[target.space_id] = [
                h for h in per_space_hits.get(target.space_id, []) if h.chunk_id in allowed_ids
            ]
        per_space_authorized_counts = {
            sid: len(hits) for sid, hits in per_space_authorized_hits.items()
        }

        # --- 步骤 4：仅对已授权 ID 加载正文（无授权结果直接短路，绝不碰正文/Reranker） ---
        authorized_bodies: dict[str, AuthorizedChunkBody] = {}
        if ordered_allowed_ids:
            raw_loaded = await _maybe_await(load_authorized_chunks_fn(ordered_allowed_ids))
            authorized_bodies = self._normalize_loaded_chunks(raw_loaded, allowed_ids)

        # --- 步骤 5：仅对已授权文本按 Space 调用 RerankerRouter（校验本批最高敏感等级） ---
        per_space_ranked: dict[str, list[tuple[AuthorizedChunkBody, float | None]]] = {}
        rerank_providers: dict[str, str] = {}

        async def _rerank_space(
            space_id: str,
            auth_hits: list[QdrantCandidateHit],
        ) -> tuple[str, list[tuple[AuthorizedChunkBody, float | None]], str]:
            candidates: list[RerankCandidate] = []
            body_by_id: dict[str, AuthorizedChunkBody] = {}
            for hit in auth_hits:
                body = authorized_bodies.get(hit.chunk_id)
                if body is None:
                    continue
                body_by_id[body.chunk_id] = body
                candidates.append(
                    RerankCandidate(
                        chunk_id=body.chunk_id,
                        text=body.content,
                        sensitivity_level=body.sensitivity_level,
                        initial_score=hit.score,
                        metadata={"space_id": space_id, "document_id": body.document_id},
                    )
                )
            if not candidates:
                return space_id, [], "none"

            rerank_res = await self._reranker_router.rerank(
                cleaned_query,
                candidates,
                top_n=min(per_space_rerank_top_n, len(candidates)),
                org_allow_external_l1=org_allow_external_l1,
                allow_rrf_fallback=allow_rerank_rrf_fallback,
                organization_id=cleaned_org,
            )
            ranked_list = [
                (body_by_id[item.chunk_id], item.relevance_score)
                for item in rerank_res.items
                if item.chunk_id in body_by_id
            ]
            return space_id, ranked_list, rerank_res.provider

        rerank_tasks = [
            _rerank_space(target.space_id, per_space_authorized_hits.get(target.space_id, []))
            for target in spaces
        ]
        for sid, ranked_items, provider_name in await asyncio.gather(*rerank_tasks):
            per_space_ranked[sid] = ranked_items
            rerank_providers[sid] = provider_name

        # --- 步骤 6：跨 Space 仅基于排名执行 RRF 融合（1 / (k + rank)），不比较异构模型原始分 ---
        rrf_scores: dict[str, float] = defaultdict(float)
        best_rank_meta: dict[str, tuple[str, int, AuthorizedChunkBody, float | None]] = {}

        for target in spaces:
            ranked_items = per_space_ranked.get(target.space_id, [])
            for rank, (body, rerank_score) in enumerate(ranked_items, start=1):
                cid = body.chunk_id
                rrf_scores[cid] += 1.0 / (self._rrf_k + rank)
                if cid not in best_rank_meta or rank < best_rank_meta[cid][1]:
                    best_rank_meta[cid] = (target.space_id, rank, body, rerank_score)

        sorted_chunk_ids = sorted(
            rrf_scores.keys(),
            key=lambda cid: (-rrf_scores[cid], best_rank_meta[cid][1], cid),
        )[: max(1, final_top_k)]

        fused_items: list[FusedEvidenceItem] = []
        for idx, cid in enumerate(sorted_chunk_ids, start=1):
            sp_id, sp_rank, body, r_score = best_rank_meta[cid]
            # 若权威 ACL 返回了最新 Revision，同步刷新切片上的 acl_revision
            if body.document_id in auth_decision.doc_acl_revisions:
                body = body.model_copy(
                    update={"acl_revision": auth_decision.doc_acl_revisions[body.document_id]}
                )
            fused_items.append(
                FusedEvidenceItem(
                    citation_id=f"C{idx}",
                    chunk=body,
                    space_id=sp_id,
                    per_space_rank=sp_rank,
                    rrf_score=round(rrf_scores[cid], 6),
                    rerank_score=r_score,
                )
            )

        # --- 步骤 7：ContextBuilder 按 Token 预算与来源多样性组装防注入隔离上下文 ---
        built_context = self._context_builder.build(
            user_query=cleaned_query,
            items=fused_items,
            answer_policy=answer_policy,
            conversation_history=conversation_history,
            system_instructions=system_instructions,
        )

        latency_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        audit_trace = RetrievalAuditTrace(
            query_hash=query_hash,
            organization_id=cleaned_org,
            space_ids=[t.space_id for t in spaces],
            candidate_chunk_ids=ordered_candidate_ids,
            authorized_chunk_ids=ordered_allowed_ids,
            denied_count=denied_count,
            per_space_candidate_counts=per_space_candidate_counts,
            per_space_authorized_counts=per_space_authorized_counts,
            rerank_providers=rerank_providers,
            latency_ms=latency_ms,
        )

        return MultiSpaceRetrievalResult(
            items=fused_items,
            context=built_context,
            audit_trace=audit_trace,
        )
