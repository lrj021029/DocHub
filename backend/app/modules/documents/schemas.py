"""文档导入、台账查询、分阶段处理状态与发布（解除 Quarantine）Pydantic Schema 定义。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SensitivityLevel = Literal["L0", "L1", "L2", "L3"]


class UploadSessionCreateRequest(BaseModel):
    """创建预签名上传会话请求体（JSON 模式）。"""

    filename: str = Field(..., min_length=1, max_length=255, description="原始文件名（禁止含路径）")
    title: str | None = Field(
        default=None, min_length=1, max_length=255, description="可选文档标题"
    )
    mime_type: str | None = Field(default=None, max_length=128)
    size_bytes: int | None = Field(default=None, ge=1, description="声明文件字节数")
    sensitivity_level: SensitivityLevel = Field(default="L1", description="敏感等级（L0~L3）")
    document_id: uuid.UUID | None = Field(
        default=None,
        description="若传入已有文档 ID，则为其新增不可变版本",
    )


class UploadCompleteRequest(BaseModel):
    """上传完成校验请求体。"""

    expected_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="可选客户端计算的 SHA-256 用于比对完整性",
    )


class DocumentUploadResponse(BaseModel):
    """创建上传会话或直传完成后的统一响应结构。"""

    upload_id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    version_no: int
    space_id: uuid.UUID
    title: str
    filename: str
    document_status: str
    import_status: str
    parse_status: str
    sensitivity_level: str
    source_object_key: str
    file_sha256: str | None = None
    size_bytes: int = 0
    presigned_put_url: str | None = None


class ImportBatchFileSpec(BaseModel):
    """批量导入单文件元数据声明。"""

    filename: str = Field(..., min_length=1, max_length=255)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    mime_type: str | None = Field(default=None, max_length=128)
    size_bytes: int | None = Field(default=None, ge=1)
    sensitivity_level: SensitivityLevel = Field(default="L1")


class ImportBatchCreateRequest(BaseModel):
    """批量导入文档会话创建请求体。"""

    files: list[ImportBatchFileSpec] = Field(..., min_length=1, max_length=100)


class ImportBatchCreateResponse(BaseModel):
    """批量导入创建结果。"""

    space_id: uuid.UUID
    created_count: int
    items: list[DocumentUploadResponse]


class DocumentListItem(BaseModel):
    """文档台账列表项。"""

    id: uuid.UUID
    organization_id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    title: str
    source_type: str
    status: str
    current_version_id: uuid.UUID | None = None
    latest_version_id: uuid.UUID | None = None
    latest_version_no: int | None = None
    latest_import_status: str | None = None
    latest_parse_status: str | None = None
    sensitivity_level: str
    acl_revision: int = 0
    created_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class DocumentDetailResponse(DocumentListItem):
    """文档详情响应（仅授权可见，附带当前版本基础元数据）。"""

    acl_grant_count: int = 0


class DocumentVersionItem(BaseModel):
    """不可变文档版本列表项。"""

    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    filename: str
    source_object_key: str
    file_sha256: str | None = None
    mime_type: str
    size_bytes: int
    created_by: uuid.UUID | None = None
    created_at: datetime


class StageJobStatus(BaseModel):
    """单个独立阶段任务状态摘要（严禁虚构假进度）。"""

    job_id: uuid.UUID | None = None
    status: str
    provider: str | None = None
    external_task_id: str | None = None
    progress: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    updated_at: datetime | None = None


class DocumentProcessingStatusResponse(BaseModel):
    """文档版本分阶段真实处理状态视图（Import -> Parse -> Chunking -> Embedding/Index）。"""

    document_id: uuid.UUID
    document_version_id: uuid.UUID
    document_status: str
    sensitivity_level: str
    import_job: StageJobStatus
    parse_job: StageJobStatus
    chunking_job: StageJobStatus
    index_job: StageJobStatus


class DocumentVersionActionResponse(BaseModel):
    """重处理或发布操作响应。"""

    document_id: uuid.UUID
    document_version_id: uuid.UUID
    document_status: str
    parse_status: str | None = None
    acl_revision: int | None = None
