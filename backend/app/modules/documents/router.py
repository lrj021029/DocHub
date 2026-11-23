"""文档导入、台账、分阶段状态与发布 HTTP 路由。"""

from __future__ import annotations

import uuid
from functools import lru_cache

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.core.idempotency import validate_idempotency_key
from app.core.pagination import OffsetPage
from app.db.session import get_db_session
from app.modules.documents.schemas import (
    DocumentDetailResponse,
    DocumentListItem,
    DocumentProcessingStatusResponse,
    DocumentUploadResponse,
    DocumentVersionActionResponse,
    DocumentVersionItem,
    ImportBatchCreateRequest,
    ImportBatchCreateResponse,
    UploadCompleteRequest,
    UploadSessionCreateRequest,
)
from app.modules.documents.service import (
    complete_presigned_upload,
    create_import_batch,
    create_presigned_upload_session,
    get_document_detail,
    get_document_version_processing_status,
    list_document_versions,
    list_documents,
    publish_document_version,
    reprocess_document_version,
    upload_document_direct,
)
from app.modules.documents.storage import MinIOStorageService
from app.modules.identity.deps import OrgContext, require_permission

router = APIRouter(tags=["Documents"])


@lru_cache(maxsize=1)
def get_storage_service() -> MinIOStorageService:
    """获取进程级缓存的 MinIO 存储服务实例。"""
    return MinIOStorageService()


@router.post(
    "/spaces/{space_id}/documents/uploads",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建文档上传会话或直传文档到 MinIO",
    description="同时支持 multipart/form-data 小文件直传与 application/json 预签名 PUT 会话创建。",
)
async def post_document_upload(
    space_id: uuid.UUID,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    org_ctx: OrgContext = Depends(require_permission("document:write")),
    session: AsyncSession = Depends(get_db_session),
    storage: MinIOStorageService = Depends(get_storage_service),
) -> DocumentUploadResponse:
    """多模态上传入口：自动识别 multipart 直传与 JSON 预签名模式。"""
    content_type = (request.headers.get("content-type") or "").lower()
    validated_key = validate_idempotency_key(idempotency_key, required=False)

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        uploaded_file = form.get("file")
        if uploaded_file is None or not hasattr(uploaded_file, "read"):
            raise ValidationError(
                code="MISSING_UPLOAD_FILE", message="multipart 请求缺少 file 字段"
            )

        raw_bytes = await uploaded_file.read()  # type: ignore[union-attr]
        raw_filename = str(getattr(uploaded_file, "filename", "") or "")
        mime_type = getattr(uploaded_file, "content_type", None)
        title_val = form.get("title")
        sensitivity_val = str(form.get("sensitivity_level") or "L1").strip().upper()
        doc_id_raw = form.get("document_id")
        doc_id: uuid.UUID | None = None
        if doc_id_raw:
            try:
                doc_id = uuid.UUID(str(doc_id_raw).strip())
            except ValueError as exc:
                raise ValidationError(
                    code="INVALID_DOCUMENT_ID", message="document_id 格式无效"
                ) from exc

        return await upload_document_direct(
            org_ctx=org_ctx,
            space_id=space_id,
            raw_filename=raw_filename,
            mime_type=mime_type,
            file_bytes=raw_bytes,
            title=str(title_val) if title_val else None,
            sensitivity_level=sensitivity_val,
            document_id=doc_id,
            idempotency_key=validated_key,
            session=session,
            storage=storage,
        )

    try:
        raw_json = await request.json()
    except Exception as exc:
        raise ValidationError(
            code="INVALID_UPLOAD_BODY",
            message="请使用 multipart/form-data 上传文件或提交合法 JSON 元数据",
        ) from exc

    payload = UploadSessionCreateRequest.model_validate(raw_json)
    return await create_presigned_upload_session(
        org_ctx=org_ctx,
        space_id=space_id,
        payload=payload,
        idempotency_key=validated_key,
        session=session,
        storage=storage,
    )


@router.post(
    "/documents/uploads/{upload_id}/complete",
    response_model=DocumentUploadResponse,
    summary="校验预签名上传完成并进入 Quarantine",
)
async def post_upload_complete(
    upload_id: uuid.UUID,
    payload: UploadCompleteRequest = UploadCompleteRequest(),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    org_ctx: OrgContext = Depends(require_permission("document:write")),
    session: AsyncSession = Depends(get_db_session),
    storage: MinIOStorageService = Depends(get_storage_service),
) -> DocumentUploadResponse:
    """服务端对 MinIO 对象执行 SHA-256 与字节大小核验，推进状态并写入 Outbox。"""
    validated_key = validate_idempotency_key(idempotency_key, required=True)
    assert validated_key is not None
    return await complete_presigned_upload(
        org_ctx=org_ctx,
        upload_id=upload_id,
        payload=payload,
        idempotency_key=validated_key,
        session=session,
        storage=storage,
    )


@router.post(
    "/spaces/{space_id}/documents/import-batches",
    response_model=ImportBatchCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="批量导入文档（返回多文件预签名上传会话）",
)
async def post_import_batch(
    space_id: uuid.UUID,
    payload: ImportBatchCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    org_ctx: OrgContext = Depends(require_permission("document:write")),
    session: AsyncSession = Depends(get_db_session),
    storage: MinIOStorageService = Depends(get_storage_service),
) -> ImportBatchCreateResponse:
    """批量创建多个文件的隔离导入任务与 MinIO 预签名地址。"""
    validated_key = validate_idempotency_key(idempotency_key, required=True)
    assert validated_key is not None
    return await create_import_batch(
        org_ctx=org_ctx,
        space_id=space_id,
        payload=payload,
        idempotency_key=validated_key,
        session=session,
        storage=storage,
    )


@router.get(
    "/documents",
    response_model=OffsetPage[DocumentListItem],
    summary="查询文档台账列表",
)
async def get_documents(
    space_id: uuid.UUID | None = Query(default=None),
    doc_status: str | None = Query(default=None, alias="status"),
    sensitivity_level: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=120),
    sort: str | None = Query(default="-created_at"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    org_ctx: OrgContext = Depends(require_permission("document:read")),
    session: AsyncSession = Depends(get_db_session),
) -> OffsetPage[DocumentListItem]:
    """查询文档台账（普通读者仅见已获四维 ACL 授权的文档）。"""
    return await list_documents(
        org_ctx=org_ctx,
        session=session,
        space_id=space_id,
        status_filter=doc_status,
        sensitivity_level=sensitivity_level,
        q=q,
        sort=sort,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetailResponse,
    summary="查询文档详情（强制 ACL 校验）",
)
async def get_document(
    document_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("document:read")),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentDetailResponse:
    """获取单篇授权文档的元数据详情。"""
    return await get_document_detail(
        org_ctx=org_ctx,
        document_id=document_id,
        session=session,
    )


@router.get(
    "/documents/{document_id}/versions",
    response_model=list[DocumentVersionItem],
    summary="查询文档不可变版本历史",
)
async def get_doc_versions(
    document_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("document:read")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DocumentVersionItem]:
    """获取文档所有不可变历史版本记录。"""
    return await list_document_versions(
        org_ctx=org_ctx,
        document_id=document_id,
        session=session,
    )


@router.get(
    "/document-versions/{version_id}/processing-status",
    response_model=DocumentProcessingStatusResponse,
    summary="查询文档版本分阶段真实处理状态",
)
async def get_version_processing_status(
    version_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("document:write")),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentProcessingStatusResponse:
    """查看 ImportJob / ParseJob / ChunkingJob / IndexJob 各自独立真实状态。"""
    return await get_document_version_processing_status(
        org_ctx=org_ctx,
        version_id=version_id,
        session=session,
    )


@router.post(
    "/document-versions/{version_id}/reprocess",
    response_model=DocumentVersionActionResponse,
    summary="重新触发文档版本解析/切片/索引",
)
async def post_reprocess_version(
    version_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("document:write")),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentVersionActionResponse:
    """重新排入解析队列并写入 Outbox 事件。"""
    return await reprocess_document_version(
        org_ctx=org_ctx,
        version_id=version_id,
        session=session,
    )


@router.post(
    "/document-versions/{version_id}/publish",
    response_model=DocumentVersionActionResponse,
    summary="发布文档版本并解除 Quarantine（强制校验已配置非空 ACL）",
)
async def post_publish_version(
    version_id: uuid.UUID,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    org_ctx: OrgContext = Depends(require_permission("document:write")),
    session: AsyncSession = Depends(get_db_session),
) -> DocumentVersionActionResponse:
    """校验已配置有效 ACL 后解除 Quarantine 并激活当前版本。"""
    validated_key = validate_idempotency_key(idempotency_key, required=False)
    return await publish_document_version(
        org_ctx=org_ctx,
        version_id=version_id,
        idempotency_key=validated_key,
        session=session,
    )
