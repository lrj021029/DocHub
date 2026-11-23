"""向量化领域模块：Qwen / 本地 BGE 双选 Embedding 路由与中文 BM25 稀疏向量生成器。"""

from app.modules.embeddings.providers import (
    BaseEmbeddingProvider,
    EmbeddingBatchResult,
    EmbeddingCapability,
    EmbeddingProfile,
    EmbeddingRouter,
    LocalBGEEmbeddingProvider,
    QwenEmbeddingProvider,
)
from app.modules.embeddings.sparse import DEFAULT_STOPWORDS, ChineseSparseVectorizer

__all__ = [
    "BaseEmbeddingProvider",
    "ChineseSparseVectorizer",
    "DEFAULT_STOPWORDS",
    "EmbeddingBatchResult",
    "EmbeddingCapability",
    "EmbeddingProfile",
    "EmbeddingRouter",
    "LocalBGEEmbeddingProvider",
    "QwenEmbeddingProvider",
]
