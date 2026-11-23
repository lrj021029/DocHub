"""引用溯源领域模块：AnswerCitation 页码/Block/BBox 绑定与点击时实时二次鉴权。"""

from app.modules.citations.router import router as citations_router
from app.modules.citations.schemas import CitationDetailResponse
from app.modules.citations.service import get_authorized_citation_detail

__all__ = [
    "CitationDetailResponse",
    "citations_router",
    "get_authorized_citation_detail",
]
