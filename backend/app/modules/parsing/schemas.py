"""文档解析层核心契约与数据模型（Schemas）。

契约基线（TECHNICAL_SPEC §7.2 & §7.6）：
- 覆盖解析请求、异步提交回执、状态轮询、结构化块（ParsedBlockData）、
  产物包（ParseArtifactBundle）与任务取消回执（CancelResult）。
- 状态枚举严格对齐 ParseJob 状态机（包含 `BLOCKED_POLICY` 策略阻断态）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.ids import new_uuid7_str
from app.modules.providers.egress import SensitivityLevel


class ParseJobState(StrEnum):
    """解析任务标准状态枚举（TECHNICAL_SPEC §7.6）。"""

    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    BLOCKED_POLICY = "BLOCKED_POLICY"


class BlockType(StrEnum):
    """标准化结构块类型枚举。"""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    FORMULA = "formula"
    IMAGE = "image"
    LIST = "list"
    CODE = "code"


class ParseRequest(BaseModel):
    """文档解析统一请求契约。"""

    document_version_id: str = Field(..., min_length=1, description="文档版本唯一 ID")
    file_bytes: bytes | None = Field(default=None, description="待解析文件原始字节（可选）")
    source_ref: str | None = Field(
        default=None,
        description="文件源引用（如对象存储预签名 URL 或内部路径，可选）",
    )
    filename: str = Field(..., min_length=1, description="原始文件名（用于扩展名与格式判定）")
    mime_type: str = Field(default="", description="文件 MIME 类型")
    sensitivity_level: SensitivityLevel = Field(
        default=SensitivityLevel.L0,
        description="文档敏感等级（L0/L1/L2/L3）",
    )
    org_allow_external_l1: bool = Field(
        default=False,
        description="当前组织是否允许 L1 文档外发至外部解析服务",
    )
    idempotency_key: str = Field(..., min_length=1, description="解析幂等键（防重复提交）")
    existing_external_task_id: str | None = Field(
        default=None,
        description="崩溃恢复或重试时已存在的外部任务 ID（用于对账而非重提）",
    )
    force_ocr: bool = Field(
        default=False,
        description="是否强制走 OCR/复杂版面解析路径",
    )
    organization_id: str | None = Field(default=None, description="所属组织 ID（用于外发审计）")
    extra_options: dict[str, Any] = Field(
        default_factory=dict,
        description="解析器可选扩展参数",
    )

    @model_validator(mode="after")
    def _validate_input_source(self) -> ParseRequest:
        """确保至少提供文件字节、源引用或用于对账的既有外部任务 ID 之一。"""
        if (
            self.file_bytes is None
            and not (self.source_ref and self.source_ref.strip())
            and not (self.existing_external_task_id and self.existing_external_task_id.strip())
        ):
            raise ValueError(
                "必须提供 file_bytes、source_ref 或 existing_external_task_id 至少一项"
            )
        return self


class ParseSubmission(BaseModel):
    """解析任务提交回执。"""

    external_task_id: str = Field(..., min_length=1, description="外部或本地解析任务 ID")
    parser_provider: str = Field(..., description="解析器 Provider 标识")
    parser_version: str = Field(..., description="解析器版本号")
    status: ParseJobState = Field(..., description="提交后的初始任务状态")
    idempotency_key: str = Field(..., description="关联幂等键")
    is_reconciled: bool = Field(
        default=False,
        description="本次提交是否命中已有任务对账恢复（未重复提交外部任务）",
    )
    submitted_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="任务提交 UTC 时间",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="脱敏后的扩展回执信息")


class ParseStatus(BaseModel):
    """解析任务轮询状态。"""

    external_task_id: str = Field(..., description="外部或本地解析任务 ID")
    state: ParseJobState = Field(..., description="归一化后的内部解析状态")
    provider_state: str = Field(
        ...,
        description="Provider 原始状态（如 pending/running/done/failed/cancelled）",
    )
    progress: float | None = Field(default=None, ge=0.0, le=100.0, description="可选百分比进度")
    result_url: str | None = Field(default=None, description="解析结果下载链接（可能有时效）")
    error_code: str | None = Field(default=None, description="失败时的错误码")
    error_message: str | None = Field(default=None, description="失败时的安全错误描述")
    metadata: dict[str, Any] = Field(default_factory=dict, description="额外状态元数据")
    checked_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="状态检查 UTC 时间",
    )


class ParsedBlockData(BaseModel):
    """统一结构化解析块（对应数据模型 ParsedBlock）。"""

    block_id: str = Field(default_factory=new_uuid7_str, description="结构块唯一 ID")
    block_type: BlockType = Field(
        ..., description="块类型（heading/paragraph/table/formula/image/list/code）"
    )
    page_number: int | None = Field(default=None, ge=1, description="1-based 原始页码（若适用）")
    heading_path: list[str] = Field(
        default_factory=list,
        description="当前块所属的层级标题路径（从顶层标题到最近标题）",
    )
    text: str = Field(default="", description="纯文本表示（用于检索与字数统计）")
    markdown: str = Field(default="", description="保真 Markdown 表示（保留表头、公式、代码块）")
    bounding_box: list[float] | dict[str, Any] | None = Field(
        default=None,
        description="可选页面坐标框（通常为 [x0, y0, x1, y1] 或结构化坐标字典）",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="块级扩展元数据（如表头、图注、Sheet 名）"
    )

    @field_validator("bounding_box")
    @classmethod
    def _validate_bbox(
        cls, value: list[float] | dict[str, Any] | None
    ) -> list[float] | dict[str, Any] | None:
        if isinstance(value, list) and len(value) != 4:
            raise ValueError("列表形式的 bounding_box 必须精确包含 4 个数值 [x0, y0, x1, y1]")
        return value


class RenderedPageArtifact(BaseModel):
    """解析生成的页面渲染图或插图制品（用于前端引用高亮定位）。"""

    page_number: int | None = Field(default=None, ge=1, description="关联 1-based 页码")
    filename: str = Field(..., description="压缩包或产物内的文件名")
    mime_type: str = Field(default="image/png", description="图片 MIME 类型")
    sha256: str = Field(..., description="图片字节 SHA-256 校验值")
    size_bytes: int = Field(..., ge=0, description="图片字节长度")
    content_bytes: bytes | None = Field(default=None, description="可选内存图片字节")


class ParseArtifactBundle(BaseModel):
    """完整解析产物集合（用于落盘 MinIO、入库 ParsedBlock 与触发后续 Chunking）。"""

    blocks: list[ParsedBlockData] = Field(default_factory=list, description="归一化结构块序列")
    markdown: str = Field(default="", description="完整文档 Markdown 文本")
    raw_json: dict[str, Any] | list[Any] = Field(
        default_factory=dict,
        description="原始结构化解析 JSON（如 MinerU content_list/middle.json 或本地元数据）",
    )
    rendered_pages: list[RenderedPageArtifact] = Field(
        default_factory=list,
        description="提取出的页面渲染图/图表图片制品",
    )
    parser_provider: str = Field(..., description="实际执行解析的 Provider 名称")
    parser_version: str = Field(..., description="解析器版本号")
    sha256: str = Field(..., description="核心产物（如 ZIP 包或规范化内容）的 SHA-256 哈希")
    metadata: dict[str, Any] = Field(default_factory=dict, description="解析级统计与诊断信息")


class CancelResult(BaseModel):
    """解析任务取消结果回执。"""

    external_task_id: str = Field(..., description="目标任务 ID")
    cancelled: bool = Field(..., description="是否成功取消或已处于终止态")
    state: ParseJobState = Field(..., description="取消后的任务状态")
    message: str = Field(default="", description="中文状态说明")
