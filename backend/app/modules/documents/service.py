"""文档安全导入、MinIO 完整性核验、台账查询、分阶段状态与 Quarantine 发布服务。

安全架构约束：
1. 新导入文档默认处于 `QUARANTINED` 状态，任何未经配置非空 ACL 并显式发布的操作，
   都无法解除 Quarantine，也不会被 `AuthoritativeACLEngine.batch_authorize_chunks` 放行。
2. 服务端强制执行路径穿越检查、格式白名单、字节与 SHA-256 校验，并同事务写入 `OutboxEvent`。
3. `GET /api/v1/documents/{document_id}` 对无权用户返回统一 404/403（不泄露未授权文档标题）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.ids import new_uuid7
from app.core.pagination import OffsetPage, SortOrder, SortWhitelistValidator
from app.db.session import org_rls_session
from app.modules.acl.engine import AuthoritativeACLEngine
from app.modules.acl.models import ACLGrant, DocumentACL
from app.modules.audit.service import record_audit_event
from app.modules.documents.models import (
    ChunkSet,
    Document,
    DocumentVersion,
    ImportJob,
    ParseJob,
)
from app.modules.documents.schemas import (
    DocumentDetailResponse,
    DocumentListItem,
    DocumentProcessingStatusResponse,
    DocumentUploadResponse,
    DocumentVersionActionResponse,
    DocumentVersionItem,
    ImportBatchCreateRequest,
    ImportBatchCreateResponse,
    StageJobStatus,
    UploadCompleteRequest,
    UploadSessionCreateRequest,
)
from app.modules.documents.storage import (
    MinIOStorageService,
    build_safe_object_key,
    sanitize_and_validate_filename,
)
from app.modules.identity.deps import OrgContext
from app.modules.indexing.models import IndexJob
from app.modules.knowledge_spaces.models import KnowledgeSpace
from app.modules.outbox.service import (
    enqueue_outbox_event,
    lookup_idempotency_record,
    save_idempotency_record,
)

_DOC_SORT_VALIDATOR = SortWhitelistValidator(
    allowed_fields={"created_at", "updated_at", "title", "status", "sensitivity_level"},
    default_field="created_at",
    default_order=SortOrder.DESC,
)

_acl_engine = AuthoritativeACLEngine()


async def _ensure_space_and_upload_permission(
    session: AsyncSession,
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
) -> KnowledgeSpace:
    """在 org_rls_session 内校验 Space 存在、有效且允许当前用户进入。"""
    space = (
        await session.execute(
            select(KnowledgeSpace).where(
                KnowledgeSpace.id == space_id,
                KnowledgeSpace.organization_id == org_ctx.organization_id,
                KnowledgeSpace.status == "ACTIVE",
            )
        )
    ).scalar_one_or_none()
    if space is None:
        raise NotFoundError(code="SPACE_NOT_FOUND", message="目标知识空间不存在或已归档")

    if space.visibility == "PRIVATE" and not org_ctx.has_permission("space:manage"):
        decision = await _acl_engine.check_space_access(
            session,
            org_ctx,
            space_id,
            organization_id=org_ctx.organization_id,
        )
        if not decision.allowed:
            raise ForbiddenError(code="SPACE_ACCESS_DENIED", message="无权向该私有知识空间上传文档")

    return space


async def _create_document_and_version_records_in_tx(
    session: AsyncSession,
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
    safe_filename: str,
    mime_type: str,
    title: str | None,
    sensitivity_level: str,
    source_type: str,
    document_id: uuid.UUID | None,
    initial_import_status: str,
    file_sha256: str | None,
    size_bytes: int,
    idempotency_key: str | None,
    storage: MinIOStorageService,
) -> tuple[Document, DocumentVersion, ImportJob, ParseJob]:
    """在已开启的 org_rls_session 事务内创建或追加不可变 DocumentVersion 与初始任务状态。"""
    if document_id is not None:
        doc = (
            await session.execute(
                select(Document)
                .where(
                    Document.id == document_id,
                    Document.organization_id == org_ctx.organization_id,
                    Document.space_id == space_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="目标文档不存在或不属于该空间")
        max_ver = int(
            (
                await session.execute(
                    select(func.coalesce(func.max(DocumentVersion.version_no), 0)).where(
                        DocumentVersion.organization_id == org_ctx.organization_id,
                        DocumentVersion.document_id == doc.id,
                    )
                )
            ).scalar_one()
        )
        version_no = max_ver + 1
    else:
        doc_id = new_uuid7()
        doc = Document(
            id=doc_id,
            organization_id=org_ctx.organization_id,
            space_id=space_id,
            title=(title or safe_filename).strip()[:255],
            source_type=source_type,
            status="QUARANTINED",
            sensitivity_level=sensitivity_level,
            created_by=org_ctx.principal.user_id,
        )
        session.add(doc)
        await session.flush()

        # 初始化 revision=0 的空 DocumentACL（默认拒绝所有普通检索，待显式配置后发布）
        session.add(
            DocumentACL(
                organization_id=org_ctx.organization_id,
                document_id=doc.id,
                revision=0,
                status="ACTIVE",
            )
        )
        version_no = 1

    object_key = build_safe_object_key(
        organization_id=org_ctx.organization_id,
        space_id=space_id,
        document_id=doc.id,
        version_no=version_no,
        safe_filename=safe_filename,
    )

    version = DocumentVersion(
        organization_id=org_ctx.organization_id,
        document_id=doc.id,
        version_no=version_no,
        filename=safe_filename,
        source_object_key=object_key,
        file_sha256=file_sha256,
        mime_type=mime_type,
        size_bytes=size_bytes,
        created_by=org_ctx.principal.user_id,
    )
    session.add(version)
    await session.flush()

    import_job = ImportJob(
        organization_id=org_ctx.organization_id,
        document_version_id=version.id,
        status=initial_import_status,
        idempotency_key=idempotency_key,
    )
    parse_job = ParseJob(
        organization_id=org_ctx.organization_id,
        document_version_id=version.id,
        parser_provider="auto",
        status="QUEUED",
        idempotency_key=idempotency_key,
    )
    session.add(import_job)
    session.add(parse_job)
    await session.flush()

    if initial_import_status == "QUARANTINED":
        enqueue_outbox_event(
            session,
            organization_id=org_ctx.organization_id,
            aggregate_type="document_version",
            aggregate_id=version.id,
            aggregate_revision=version.version_no,
            event_type="document.uploaded",
            payload={
                "document_id": str(doc.id),
                "document_version_id": str(version.id),
                "space_id": str(space_id),
                "sensitivity_level": doc.sensitivity_level,
                "source_object_key": object_key,
                "bucket": storage.raw_bucket,
            },
        )

    record_audit_event(
        session,
        organization_id=org_ctx.organization_id,
        actor_user_id=org_ctx.principal.user_id,
        action="document.version.created",
        resource_type="document_version",
        resource_id=version.id,
        details={
            "document_id": str(doc.id),
            "version_no": version_no,
            "sensitivity_level": doc.sensitivity_level,
            "import_status": initial_import_status,
        },
    )

    return doc, version, import_job, parse_job


async def create_presigned_upload_session(
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
    payload: UploadSessionCreateRequest,
    idempotency_key: str | None,
    session: AsyncSession,
    storage: MinIOStorageService,
) -> DocumentUploadResponse:
    """创建 MinIO 预签名直传会话（新文档默认进入 QUARANTINED）。"""
    safe_filename, _, resolved_mime = sanitize_and_validate_filename(
        payload.filename,
        mime_type=payload.mime_type,
    )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        await _ensure_space_and_upload_permission(session, org_ctx=org_ctx, space_id=space_id)

        if idempotency_key:
            fp, req_hash, existing = await lookup_idempotency_record(
                session,
                organization_id=org_ctx.organization_id,
                principal_id=org_ctx.principal.user_id,
                operation=f"document.upload.session:{space_id}",
                idempotency_key=idempotency_key,
                request_payload=payload.model_dump(mode="json"),
            )
            if existing is not None:
                return DocumentUploadResponse.model_validate(existing.response_body_json)
        else:
            fp, req_hash = "", ""

        doc, version, import_job, parse_job = await _create_document_and_version_records_in_tx(
            session,
            org_ctx=org_ctx,
            space_id=space_id,
            safe_filename=safe_filename,
            mime_type=resolved_mime,
            title=payload.title,
            sensitivity_level=payload.sensitivity_level,
            source_type="UPLOAD",
            document_id=payload.document_id,
            initial_import_status="UPLOADING",
            file_sha256=None,
            size_bytes=payload.size_bytes or 0,
            idempotency_key=idempotency_key,
            storage=storage,
        )

        presigned_url = await storage.create_presigned_put_url(
            bucket_name=storage.raw_bucket,
            object_key=version.source_object_key,
        )

        resp = DocumentUploadResponse(
            upload_id=import_job.id,
            document_id=doc.id,
            document_version_id=version.id,
            version_no=version.version_no,
            space_id=doc.space_id,
            title=doc.title,
            filename=version.filename,
            document_status=doc.status,
            import_status=import_job.status,
            parse_status=parse_job.status,
            sensitivity_level=doc.sensitivity_level,
            source_object_key=version.source_object_key,
            file_sha256=version.file_sha256,
            size_bytes=version.size_bytes,
            presigned_put_url=presigned_url,
        )

        if idempotency_key:
            save_idempotency_record(
                session,
                organization_id=org_ctx.organization_id,
                principal_id=org_ctx.principal.user_id,
                operation=f"document.upload.session:{space_id}",
                idempotency_key=idempotency_key,
                fingerprint=fp,
                request_hash=req_hash,
                response_status=201,
                response_body=resp.model_dump(mode="json"),
            )

    return resp


async def upload_document_direct(
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
    raw_filename: str,
    mime_type: str | None,
    file_bytes: bytes,
    title: str | None,
    sensitivity_level: str,
    document_id: uuid.UUID | None,
    idempotency_key: str | None,
    session: AsyncSession,
    storage: MinIOStorageService,
) -> DocumentUploadResponse:
    """API 代理直传文件到 MinIO 并立即完成 SHA-256 核验与 Quarantine 建档。"""
    if sensitivity_level not in {"L0", "L1", "L2", "L3"}:
        raise ValidationError(
            code="INVALID_SENSITIVITY_LEVEL", message="敏感等级必须为 L0/L1/L2/L3"
        )

    safe_filename, _, resolved_mime = sanitize_and_validate_filename(
        raw_filename,
        mime_type=mime_type,
    )
    if not file_bytes:
        raise ValidationError(code="EMPTY_FILE", message="禁止上传空文件（0 字节）")

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        await _ensure_space_and_upload_permission(session, org_ctx=org_ctx, space_id=space_id)

        doc, version, import_job, parse_job = await _create_document_and_version_records_in_tx(
            session,
            org_ctx=org_ctx,
            space_id=space_id,
            safe_filename=safe_filename,
            mime_type=resolved_mime,
            title=title,
            sensitivity_level=sensitivity_level,
            source_type="UPLOAD",
            document_id=document_id,
            initial_import_status="VERIFYING",
            file_sha256=None,
            size_bytes=len(file_bytes),
            idempotency_key=idempotency_key,
            storage=storage,
        )

        # 上传真实字节到 MinIO 并核验返回摘要
        verified = await storage.put_bytes(
            bucket_name=storage.raw_bucket,
            object_key=version.source_object_key,
            data=file_bytes,
            content_type=resolved_mime,
        )
        version.file_sha256 = verified.sha256
        version.size_bytes = verified.size_bytes
        import_job.status = "QUARANTINED"

        enqueue_outbox_event(
            session,
            organization_id=org_ctx.organization_id,
            aggregate_type="document_version",
            aggregate_id=version.id,
            aggregate_revision=version.version_no,
            event_type="document.uploaded",
            payload={
                "document_id": str(doc.id),
                "document_version_id": str(version.id),
                "space_id": str(space_id),
                "sensitivity_level": doc.sensitivity_level,
                "source_object_key": version.source_object_key,
                "file_sha256": verified.sha256,
                "size_bytes": verified.size_bytes,
                "bucket": storage.raw_bucket,
            },
        )

    return DocumentUploadResponse(
        upload_id=import_job.id,
        document_id=doc.id,
        document_version_id=version.id,
        version_no=version.version_no,
        space_id=doc.space_id,
        title=doc.title,
        filename=version.filename,
        document_status=doc.status,
        import_status=import_job.status,
        parse_status=parse_job.status,
        sensitivity_level=doc.sensitivity_level,
        source_object_key=version.source_object_key,
        file_sha256=version.file_sha256,
        size_bytes=version.size_bytes,
        presigned_put_url=None,
    )


async def complete_presigned_upload(
    *,
    org_ctx: OrgContext,
    upload_id: uuid.UUID,
    payload: UploadCompleteRequest,
    idempotency_key: str,
    session: AsyncSession,
    storage: MinIOStorageService,
) -> DocumentUploadResponse:
    """校验预签名上传对象在 MinIO 中的真实大小与 SHA-256，并将 ImportJob 推进至 QUARANTINED。"""
    operation = f"document.upload.complete:{upload_id}"

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        fp, req_hash, existing_idem = await lookup_idempotency_record(
            session,
            organization_id=org_ctx.organization_id,
            principal_id=org_ctx.principal.user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_payload=payload.model_dump(mode="json"),
        )
        if existing_idem is not None:
            return DocumentUploadResponse.model_validate(existing_idem.response_body_json)

        import_job = (
            await session.execute(
                select(ImportJob)
                .where(
                    ImportJob.id == upload_id,
                    ImportJob.organization_id == org_ctx.organization_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if import_job is None:
            raise NotFoundError(code="UPLOAD_JOB_NOT_FOUND", message="上传会话不存在或无权访问")

        version = (
            await session.execute(
                select(DocumentVersion).where(
                    DocumentVersion.id == import_job.document_version_id,
                    DocumentVersion.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if version is None:
            raise NotFoundError(code="DOCUMENT_VERSION_NOT_FOUND", message="关联文档版本不存在")

        doc = (
            await session.execute(
                select(Document).where(
                    Document.id == version.document_id,
                    Document.organization_id == org_ctx.organization_id,
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="关联文档不存在")

        parse_job = (
            await session.execute(
                select(ParseJob)
                .where(
                    ParseJob.organization_id == org_ctx.organization_id,
                    ParseJob.document_version_id == version.id,
                )
                .order_by(ParseJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if import_job.status != "QUARANTINED":
            verified = await storage.verify_and_hash_object(
                bucket_name=storage.raw_bucket,
                object_key=version.source_object_key,
                expected_sha256=payload.expected_sha256,
            )
            version.file_sha256 = verified.sha256
            version.size_bytes = verified.size_bytes
            import_job.status = "QUARANTINED"

            enqueue_outbox_event(
                session,
                organization_id=org_ctx.organization_id,
                aggregate_type="document_version",
                aggregate_id=version.id,
                aggregate_revision=version.version_no,
                event_type="document.uploaded",
                payload={
                    "document_id": str(doc.id),
                    "document_version_id": str(version.id),
                    "space_id": str(doc.space_id),
                    "sensitivity_level": doc.sensitivity_level,
                    "source_object_key": version.source_object_key,
                    "file_sha256": verified.sha256,
                    "size_bytes": verified.size_bytes,
                    "bucket": storage.raw_bucket,
                },
            )

        resp = DocumentUploadResponse(
            upload_id=import_job.id,
            document_id=doc.id,
            document_version_id=version.id,
            version_no=version.version_no,
            space_id=doc.space_id,
            title=doc.title,
            filename=version.filename,
            document_status=doc.status,
            import_status=import_job.status,
            parse_status=parse_job.status if parse_job else "QUEUED",
            sensitivity_level=doc.sensitivity_level,
            source_object_key=version.source_object_key,
            file_sha256=version.file_sha256,
            size_bytes=version.size_bytes,
            presigned_put_url=None,
        )

        save_idempotency_record(
            session,
            organization_id=org_ctx.organization_id,
            principal_id=org_ctx.principal.user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            fingerprint=fp,
            request_hash=req_hash,
            response_status=200,
            response_body=resp.model_dump(mode="json"),
        )

    return resp


async def create_import_batch(
    *,
    org_ctx: OrgContext,
    space_id: uuid.UUID,
    payload: ImportBatchCreateRequest,
    idempotency_key: str,
    session: AsyncSession,
    storage: MinIOStorageService,
) -> ImportBatchCreateResponse:
    """批量创建多文件预签名上传会话（支持文件夹批量导入前端抽屉）。"""
    operation = f"document.import.batch:{space_id}"
    validated_files = [
        (spec, *sanitize_and_validate_filename(spec.filename, mime_type=spec.mime_type))
        for spec in payload.files
    ]

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        await _ensure_space_and_upload_permission(session, org_ctx=org_ctx, space_id=space_id)

        fp, req_hash, existing_idem = await lookup_idempotency_record(
            session,
            organization_id=org_ctx.organization_id,
            principal_id=org_ctx.principal.user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_payload=payload.model_dump(mode="json"),
        )
        if existing_idem is not None:
            return ImportBatchCreateResponse.model_validate(existing_idem.response_body_json)

        items: list[DocumentUploadResponse] = []
        for spec, safe_name, _, resolved_mime in validated_files:
            doc, version, import_job, parse_job = await _create_document_and_version_records_in_tx(
                session,
                org_ctx=org_ctx,
                space_id=space_id,
                safe_filename=safe_name,
                mime_type=resolved_mime,
                title=spec.title,
                sensitivity_level=spec.sensitivity_level,
                source_type="BATCH_IMPORT",
                document_id=None,
                initial_import_status="UPLOADING",
                file_sha256=None,
                size_bytes=spec.size_bytes or 0,
                idempotency_key=idempotency_key,
                storage=storage,
            )
            presigned_url = await storage.create_presigned_put_url(
                bucket_name=storage.raw_bucket,
                object_key=version.source_object_key,
            )
            items.append(
                DocumentUploadResponse(
                    upload_id=import_job.id,
                    document_id=doc.id,
                    document_version_id=version.id,
                    version_no=version.version_no,
                    space_id=doc.space_id,
                    title=doc.title,
                    filename=version.filename,
                    document_status=doc.status,
                    import_status=import_job.status,
                    parse_status=parse_job.status,
                    sensitivity_level=doc.sensitivity_level,
                    source_object_key=version.source_object_key,
                    file_sha256=version.file_sha256,
                    size_bytes=version.size_bytes,
                    presigned_put_url=presigned_url,
                )
            )

        resp = ImportBatchCreateResponse(
            space_id=space_id,
            created_count=len(items),
            items=items,
        )
        save_idempotency_record(
            session,
            organization_id=org_ctx.organization_id,
            principal_id=org_ctx.principal.user_id,
            operation=operation,
            idempotency_key=idempotency_key,
            fingerprint=fp,
            request_hash=req_hash,
            response_status=201,
            response_body=resp.model_dump(mode="json"),
        )

    return resp


async def list_documents(
    *,
    org_ctx: OrgContext,
    session: AsyncSession,
    space_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    sensitivity_level: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> OffsetPage[DocumentListItem]:
    """查询当前组织文档台账（管理员可见台账管理，普通阅读者仅可见本人已获授权文档）。"""
    sort_col, sort_order = _DOC_SORT_VALIDATOR.resolve_column(
        sort,
        {
            "created_at": Document.created_at,
            "updated_at": Document.updated_at,
            "title": Document.title,
            "status": Document.status,
            "sensitivity_level": Document.sensitivity_level,
        },
    )
    can_manage_docs = org_ctx.has_permission("document:write") or org_ctx.has_permission(
        "document:acl:manage"
    )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        stmt = (
            select(Document, KnowledgeSpace.name, DocumentACL.revision)
            .join(KnowledgeSpace, KnowledgeSpace.id == Document.space_id)
            .outerjoin(DocumentACL, DocumentACL.document_id == Document.id)
            .where(Document.organization_id == org_ctx.organization_id)
        )
        if space_id is not None:
            stmt = stmt.where(Document.space_id == space_id)
        if status_filter:
            stmt = stmt.where(Document.status == status_filter.strip().upper())
        if sensitivity_level:
            stmt = stmt.where(Document.sensitivity_level == sensitivity_level.strip().upper())
        if q and q.strip():
            stmt = stmt.where(Document.title.ilike(f"%{q.strip()}%"))

        order_expr = sort_col.asc() if sort_order == SortOrder.ASC else sort_col.desc()
        rows = (await session.execute(stmt.order_by(order_expr))).all()

    # 普通阅读者必须逐篇过 AuthoritativeACLEngine 校验，绝不在列表中泄漏无权文档标题
    filtered_rows: list[tuple[Document, str, int | None]] = []
    if can_manage_docs:
        filtered_rows = [(doc, sp_name, acl_rev) for doc, sp_name, acl_rev in rows]
    else:
        for doc, sp_name, acl_rev in rows:
            decision = await _acl_engine.check_document_access(
                session,
                org_ctx,
                doc.id,
                organization_id=org_ctx.organization_id,
            )
            if decision.allowed:
                filtered_rows.append((doc, sp_name, acl_rev))

    total = len(filtered_rows)
    paged_rows = filtered_rows[offset : offset + limit]
    paged_doc_ids = [doc.id for doc, _, _ in paged_rows]

    latest_ver_map: dict[uuid.UUID, DocumentVersion] = {}
    import_status_map: dict[uuid.UUID, str] = {}
    parse_status_map: dict[uuid.UUID, str] = {}

    if paged_doc_ids:
        async with org_rls_session(
            session, org_ctx.organization_id, user_id=org_ctx.principal.user_id
        ):
            ver_rows = (
                (
                    await session.execute(
                        select(DocumentVersion)
                        .where(
                            DocumentVersion.organization_id == org_ctx.organization_id,
                            DocumentVersion.document_id.in_(paged_doc_ids),
                        )
                        .order_by(DocumentVersion.version_no.desc())
                    )
                )
                .scalars()
                .all()
            )
            for v in ver_rows:
                if v.document_id not in latest_ver_map:
                    latest_ver_map[v.document_id] = v

            ver_ids = [v.id for v in latest_ver_map.values()]
            if ver_ids:
                for imp in (
                    (
                        await session.execute(
                            select(ImportJob)
                            .where(
                                ImportJob.organization_id == org_ctx.organization_id,
                                ImportJob.document_version_id.in_(ver_ids),
                            )
                            .order_by(ImportJob.created_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                ):
                    if imp.document_version_id not in import_status_map:
                        import_status_map[imp.document_version_id] = imp.status

                for pj in (
                    (
                        await session.execute(
                            select(ParseJob)
                            .where(
                                ParseJob.organization_id == org_ctx.organization_id,
                                ParseJob.document_version_id.in_(ver_ids),
                            )
                            .order_by(ParseJob.created_at.desc())
                        )
                    )
                    .scalars()
                    .all()
                ):
                    if pj.document_version_id not in parse_status_map:
                        parse_status_map[pj.document_version_id] = pj.status

    items: list[DocumentListItem] = []
    for doc, sp_name, acl_rev in paged_rows:
        latest_ver = latest_ver_map.get(doc.id)
        items.append(
            DocumentListItem(
                id=doc.id,
                organization_id=doc.organization_id,
                space_id=doc.space_id,
                space_name=sp_name,
                title=doc.title,
                source_type=doc.source_type,
                status=doc.status,
                current_version_id=doc.current_version_id,
                latest_version_id=latest_ver.id if latest_ver else None,
                latest_version_no=latest_ver.version_no if latest_ver else None,
                latest_import_status=import_status_map.get(latest_ver.id) if latest_ver else None,
                latest_parse_status=parse_status_map.get(latest_ver.id) if latest_ver else None,
                sensitivity_level=doc.sensitivity_level,
                acl_revision=acl_rev or 0,
                created_by=doc.created_by,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
            )
        )

    return OffsetPage.create(items, total=total, offset=offset, limit=limit)


async def get_document_detail(
    *,
    org_ctx: OrgContext,
    document_id: uuid.UUID,
    session: AsyncSession,
) -> DocumentDetailResponse:
    """获取文档详情（强校验授权，未授权返回 404 不泄露标题或存在性）。"""
    can_manage_docs = org_ctx.has_permission("document:write") or org_ctx.has_permission(
        "document:acl:manage"
    )
    if not can_manage_docs:
        decision = await _acl_engine.check_document_access(
            session,
            org_ctx,
            document_id,
            organization_id=org_ctx.organization_id,
        )
        if not decision.allowed:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="目标文档不存在或无权访问")

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        row = (
            await session.execute(
                select(Document, KnowledgeSpace.name, DocumentACL.id, DocumentACL.revision)
                .join(KnowledgeSpace, KnowledgeSpace.id == Document.space_id)
                .outerjoin(DocumentACL, DocumentACL.document_id == Document.id)
                .where(
                    Document.id == document_id,
                    Document.organization_id == org_ctx.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(code="DOCUMENT_NOT_FOUND", message="目标文档不存在或无权访问")
        doc, sp_name, acl_id, acl_rev = row

        grant_count = 0
        if acl_id is not None:
            grant_count = int(
                (
                    await session.execute(
                        select(func.count(ACLGrant.id)).where(
                            ACLGrant.organization_id == org_ctx.organization_id,
                            ACLGrant.acl_id == acl_id,
                        )
                    )
                ).scalar_one()
            )

        latest_ver = (
            await session.execute(
                select(DocumentVersion)
                .where(
                    DocumentVersion.organization_id == org_ctx.organization_id,
                    DocumentVersion.document_id == doc.id,
                )
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    return DocumentDetailResponse(
        id=doc.id,
        organization_id=doc.organization_id,
        space_id=doc.space_id,
        space_name=sp_name,
        title=doc.title,
        source_type=doc.source_type,
        status=doc.status,
        current_version_id=doc.current_version_id,
        latest_version_id=latest_ver.id if latest_ver else None,
        latest_version_no=latest_ver.version_no if latest_ver else None,
        sensitivity_level=doc.sensitivity_level,
        acl_revision=acl_rev or 0,
        acl_grant_count=grant_count,
        created_by=doc.created_by,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def list_document_versions(
    *,
    org_ctx: OrgContext,
    document_id: uuid.UUID,
    session: AsyncSession,
) -> list[DocumentVersionItem]:
    """查询文档的不可变版本列表。"""
    await get_document_detail(org_ctx=org_ctx, document_id=document_id, session=session)
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        versions = (
            (
                await session.execute(
                    select(DocumentVersion)
                    .where(
                        DocumentVersion.organization_id == org_ctx.organization_id,
                        DocumentVersion.document_id == document_id,
                    )
                    .order_by(DocumentVersion.version_no.desc())
                )
            )
            .scalars()
            .all()
        )

    return [
        DocumentVersionItem(
            id=v.id,
            document_id=v.document_id,
            version_no=v.version_no,
            filename=v.filename,
            source_object_key=v.source_object_key,
            file_sha256=v.file_sha256,
            mime_type=v.mime_type,
            size_bytes=v.size_bytes,
            created_by=v.created_by,
            created_at=v.created_at,
        )
        for v in versions
    ]


async def get_document_version_processing_status(
    *,
    org_ctx: OrgContext,
    version_id: uuid.UUID,
    session: AsyncSession,
) -> DocumentProcessingStatusResponse:
    """读取文档版本的各独立阶段任务状态（Import -> Parse -> Chunking -> Index）。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        row = (
            await session.execute(
                select(DocumentVersion, Document)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.organization_id == org_ctx.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(
                code="DOCUMENT_VERSION_NOT_FOUND", message="目标文档版本不存在或无权访问"
            )
        version, doc = row

        imp = (
            await session.execute(
                select(ImportJob)
                .where(
                    ImportJob.organization_id == org_ctx.organization_id,
                    ImportJob.document_version_id == version_id,
                )
                .order_by(ImportJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        pj = (
            await session.execute(
                select(ParseJob)
                .where(
                    ParseJob.organization_id == org_ctx.organization_id,
                    ParseJob.document_version_id == version_id,
                )
                .order_by(ParseJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        cs = (
            await session.execute(
                select(ChunkSet)
                .where(
                    ChunkSet.organization_id == org_ctx.organization_id,
                    ChunkSet.document_version_id == version_id,
                )
                .order_by(ChunkSet.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        ij = (
            await session.execute(
                select(IndexJob)
                .where(
                    IndexJob.organization_id == org_ctx.organization_id,
                    IndexJob.document_version_id == version_id,
                )
                .order_by(IndexJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    return DocumentProcessingStatusResponse(
        document_id=doc.id,
        document_version_id=version.id,
        document_status=doc.status,
        sensitivity_level=doc.sensitivity_level,
        import_job=StageJobStatus(
            job_id=imp.id if imp else None,
            status=imp.status if imp else "NOT_STARTED",
            error_code=imp.error_code if imp else None,
            error_detail=imp.error_message if imp else None,
            updated_at=imp.updated_at if imp else None,
        ),
        parse_job=StageJobStatus(
            job_id=pj.id if pj else None,
            status=pj.status if pj else "NOT_STARTED",
            provider=pj.parser_provider if pj else None,
            external_task_id=pj.external_task_id if pj else None,
            error_detail=pj.error_detail if pj else None,
            updated_at=pj.updated_at if pj else None,
        ),
        chunking_job=StageJobStatus(
            job_id=cs.id if cs else None,
            status=cs.status if cs else "NOT_STARTED",
            updated_at=cs.updated_at if cs else None,
        ),
        index_job=StageJobStatus(
            job_id=ij.id if ij else None,
            status=ij.status if ij else "NOT_STARTED",
            progress=ij.progress if ij else 0,
            error_code=ij.error_code if ij else None,
            error_detail=ij.error_message if ij else None,
            updated_at=ij.updated_at if ij else None,
        ),
    )


async def reprocess_document_version(
    *,
    org_ctx: OrgContext,
    version_id: uuid.UUID,
    session: AsyncSession,
) -> DocumentVersionActionResponse:
    """触发文档版本重新解析/切片/索引，同事务写 `document.parse.requested` Outbox 事件。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        row = (
            await session.execute(
                select(DocumentVersion, Document)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.organization_id == org_ctx.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(
                code="DOCUMENT_VERSION_NOT_FOUND", message="目标文档版本不存在或无权访问"
            )
        version, doc = row

        parse_job = ParseJob(
            organization_id=org_ctx.organization_id,
            document_version_id=version.id,
            parser_provider="auto",
            status="QUEUED",
        )
        session.add(parse_job)
        await session.flush()

        enqueue_outbox_event(
            session,
            organization_id=org_ctx.organization_id,
            aggregate_type="document_version",
            aggregate_id=version.id,
            aggregate_revision=version.version_no,
            event_type="document.parse.requested",
            payload={
                "document_id": str(doc.id),
                "document_version_id": str(version.id),
                "parse_job_id": str(parse_job.id),
                "sensitivity_level": doc.sensitivity_level,
            },
        )
        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="document.version.reprocess_requested",
            resource_type="document_version",
            resource_id=version.id,
            details={"parse_job_id": str(parse_job.id)},
        )

    return DocumentVersionActionResponse(
        document_id=doc.id,
        document_version_id=version.id,
        document_status=doc.status,
        parse_status=parse_job.status,
    )


async def publish_document_version(
    *,
    org_ctx: OrgContext,
    version_id: uuid.UUID,
    idempotency_key: str | None,
    session: AsyncSession,
) -> DocumentVersionActionResponse:
    """发布文档版本并解除 Quarantine；强约束：必须已配置非空且生效的 ACL 方允许发布！"""
    now = datetime.now(UTC)
    operation = f"document.version.publish:{version_id}"

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        if idempotency_key:
            fp, req_hash, existing_idem = await lookup_idempotency_record(
                session,
                organization_id=org_ctx.organization_id,
                principal_id=org_ctx.principal.user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload={"version_id": str(version_id)},
            )
            if existing_idem is not None:
                return DocumentVersionActionResponse.model_validate(
                    existing_idem.response_body_json
                )
        else:
            fp, req_hash = "", ""

        row = (
            await session.execute(
                select(DocumentVersion, Document)
                .join(Document, Document.id == DocumentVersion.document_id)
                .where(
                    DocumentVersion.id == version_id,
                    DocumentVersion.organization_id == org_ctx.organization_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError(
                code="DOCUMENT_VERSION_NOT_FOUND", message="目标文档版本不存在或无权访问"
            )
        version, doc = row

        acl = (
            await session.execute(
                select(DocumentACL).where(
                    DocumentACL.organization_id == org_ctx.organization_id,
                    DocumentACL.document_id == doc.id,
                )
            )
        ).scalar_one_or_none()
        if acl is None or acl.revision <= 0:
            raise ConflictError(
                code="ACL_REQUIRED_BEFORE_PUBLISH",
                message="文档尚未配置四维 ACL，禁止解除 Quarantine 发布上线",
            )

        grant_count = int(
            (
                await session.execute(
                    select(func.count(ACLGrant.id)).where(
                        ACLGrant.organization_id == org_ctx.organization_id,
                        ACLGrant.acl_id == acl.id,
                        ACLGrant.valid_from <= now,
                    )
                )
            ).scalar_one()
        )
        if grant_count <= 0:
            raise ConflictError(
                code="EMPTY_ACL_GRANTS_BEFORE_PUBLISH",
                message="文档 ACL 授权列表为空，请至少配置一项有效授权后再发布",
            )

        doc.current_version_id = version.id
        doc.status = "ACTIVE"

        enqueue_outbox_event(
            session,
            organization_id=org_ctx.organization_id,
            aggregate_type="document",
            aggregate_id=doc.id,
            aggregate_revision=acl.revision,
            event_type="document.published",
            payload={
                "document_id": str(doc.id),
                "document_version_id": str(version.id),
                "space_id": str(doc.space_id),
                "acl_revision": acl.revision,
            },
        )
        record_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            action="document.version.published",
            resource_type="document",
            resource_id=doc.id,
            details={"version_id": str(version.id), "acl_revision": acl.revision},
        )

        resp = DocumentVersionActionResponse(
            document_id=doc.id,
            document_version_id=version.id,
            document_status=doc.status,
            acl_revision=acl.revision,
        )
        if idempotency_key:
            save_idempotency_record(
                session,
                organization_id=org_ctx.organization_id,
                principal_id=org_ctx.principal.user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                fingerprint=fp,
                request_hash=req_hash,
                response_status=200,
                response_body=resp.model_dump(mode="json"),
            )

    return resp
