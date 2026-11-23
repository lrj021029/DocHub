"""混合检索领域模块：多 Space 权威 ACL 鉴权检索编排器与防注入上下文组装器。"""

from app.modules.retrieval.hybrid import (
    AuthorizedChunkBody,
    BuiltContextResult,
    ChunkAuthorizationResult,
    ContextBuilder,
    FusedEvidenceItem,
    MultiSpaceHybridRetriever,
    MultiSpaceRetrievalResult,
    RetrievalAuditTrace,
    SpaceRetrievalTarget,
)

__all__ = [
    "AuthorizedChunkBody",
    "BuiltContextResult",
    "ChunkAuthorizationResult",
    "ContextBuilder",
    "FusedEvidenceItem",
    "MultiSpaceHybridRetriever",
    "MultiSpaceRetrievalResult",
    "RetrievalAuditTrace",
    "SpaceRetrievalTarget",
]
