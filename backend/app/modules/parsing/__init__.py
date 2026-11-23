"""文档解析领域模块：统一解析协议、MinerU 官方适配器、本地 OCR 预留、7 类轻量解析器与路由守卫。"""

from app.modules.parsing.base import DocumentParser, SyncLightweightParser
from app.modules.parsing.lightweight import (
    CsvParser,
    DocxParser,
    ExcelParser,
    JsonParser,
    MarkdownParser,
    PlainPdfFallbackParser,
    TxtParser,
)
from app.modules.parsing.local_ocr_adapter import LocalOCRParserAdapter
from app.modules.parsing.mineru_adapter import MinerUOfficialAdapter
from app.modules.parsing.router import ParserRouteDecision, ParserRouter
from app.modules.parsing.schemas import (
    BlockType,
    CancelResult,
    ParseArtifactBundle,
    ParsedBlockData,
    ParseJobState,
    ParseRequest,
    ParseStatus,
    ParseSubmission,
    RenderedPageArtifact,
)

__all__ = [
    "BlockType",
    "CancelResult",
    "CsvParser",
    "DocumentParser",
    "DocxParser",
    "ExcelParser",
    "JsonParser",
    "LocalOCRParserAdapter",
    "MarkdownParser",
    "MinerUOfficialAdapter",
    "ParseArtifactBundle",
    "ParseJobState",
    "ParseRequest",
    "ParseStatus",
    "ParseSubmission",
    "ParsedBlockData",
    "ParserRouteDecision",
    "ParserRouter",
    "PlainPdfFallbackParser",
    "RenderedPageArtifact",
    "SyncLightweightParser",
    "TxtParser",
]
