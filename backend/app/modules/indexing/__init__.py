"""向量索引领域模块：Qdrant 混合检索存储引擎、端到端处理流水线与蓝绿索引治理路由。"""

from app.modules.indexing.pipeline import (
    ActiveIndexRollbackResult,
    DocumentIngestionPipeline,
    PipelineExecutionResult,
    resolve_collection_name,
)
from app.modules.indexing.qdrant_store import (
    DENSE_VECTOR_NAME,
    SAFE_CANDIDATE_PAYLOAD_KEYS,
    SPARSE_VECTOR_NAME,
    ACLPrefilter,
    ACLProjectionPayload,
    QdrantCandidateHit,
    QdrantChunkPoint,
    QdrantIndexStore,
)
from app.modules.indexing.router import router as indexing_router

__all__ = [
    "ACLPrefilter",
    "ACLProjectionPayload",
    "ActiveIndexRollbackResult",
    "DENSE_VECTOR_NAME",
    "DocumentIngestionPipeline",
    "PipelineExecutionResult",
    "QdrantCandidateHit",
    "QdrantChunkPoint",
    "QdrantIndexStore",
    "SAFE_CANDIDATE_PAYLOAD_KEYS",
    "SPARSE_VECTOR_NAME",
    "indexing_router",
    "resolve_collection_name",
]
