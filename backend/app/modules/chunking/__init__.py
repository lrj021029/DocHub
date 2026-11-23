"""文档切片领域模块：结构感知 Parent-Child Chunking、多级配置覆盖与页码/表头保留。"""

from app.modules.chunking.chunker import (
    CHUNKER_VERSION,
    DEFAULT_DOC_TYPE_OVERRIDES,
    ChunkData,
    ChunkingConfig,
    ChunkSetOutput,
    PageRange,
    StructureAwareChunker,
    estimate_token_count,
)

__all__ = [
    "CHUNKER_VERSION",
    "DEFAULT_DOC_TYPE_OVERRIDES",
    "ChunkData",
    "ChunkSetOutput",
    "ChunkingConfig",
    "PageRange",
    "StructureAwareChunker",
    "estimate_token_count",
]
