"""引用溯源契约（Schemas）：支持点击时实时二次 ACL 鉴权与页码/BBox 精准定位。"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class CitationDetailResponse(BaseModel):
    """引用详情与定位响应（严格对应前端 `CitationDetail` 与 TECHNICAL_SPEC §9.6）。"""

    citation_id: uuid.UUID = Field(..., description="引用记录唯一 ID")
    document_id: uuid.UUID = Field(..., description="来源文档 ID")
    document_version_id: uuid.UUID = Field(..., description="来源不可变文档版本 ID")
    document_title: str = Field(..., description="已授权文档标题")
    chunk_id: uuid.UUID = Field(..., description="来源切片 ID")
    block_id: uuid.UUID | None = Field(default=None, description="来源结构块 ID")
    page_number: int | None = Field(default=None, description="1-based 精确页码")
    page: int | None = Field(default=None, description="页码别名（兼容前端 SSE/卡片）")
    heading_path: str | None = Field(default=None, description="章节路径展示文本")
    quote_text: str = Field(default="", description="已授权证据正文摘录")
    quote: str = Field(default="", description="摘录别名")
    quote_excerpt: str = Field(default="", description="摘录别名（用于定位核验）")
    page_image_url: str | None = Field(
        default=None,
        description="页面渲染图预览地址（无渲染图时为 None，绝不伪造）",
    )
    bbox_json: list[float] | dict[str, Any] | None = Field(
        default=None,
        description="PDF 页内 Bounding Box 坐标（无精确坐标时为 None）",
    )
    bbox: list[float] | dict[str, Any] | None = Field(
        default=None,
        description="Bounding Box 别名",
    )
    acl_revision: int = Field(..., description="当前权威 ACL Revision")
    authorized: bool = Field(default=True, description="本次实时重鉴权是否通过")
    denied_reason: str | None = Field(default=None, description="拒绝原因（仅脱敏提示）")
