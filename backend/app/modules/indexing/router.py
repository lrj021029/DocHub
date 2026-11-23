"""索引构建触发、蓝绿指针预览与回滚辅助 HTTP 路由（`/api/v1/indexing/*`）。

说明：
- 本路由独立定义于 `app.modules.indexing.router`，不修改 `backend-core` 管理的
  `app/api/v1/router.py`，可由主控合并时统一挂载或在集成测试中按需挂载。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from functools import lru_cache

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.db.session import get_db_session, org_rls_session
from app.modules.identity.deps import OrgContext, require_permission
from app.modules.indexing.models import ActiveIndexPointer, IndexGeneration
from app.modules.indexing.pipeline import (
    ActiveIndexRollbackResult,
    DocumentIngestionPipeline,
    PipelineExecutionResult,
)

router = APIRouter(prefix="/indexing", tags=["Indexing"])


@lru_cache(maxsize=1)
def get_ingestion_pipeline() -> DocumentIngestionPipeline:
    """获取进程级缓存的文档处理流水线服务。"""
    return DocumentIngestionPipeline()


class IndexGenerationSummary(BaseModel):
    """蓝绿索引代次摘要视图。"""

    id: uuid.UUID
    embedding_profile_id: uuid.UUID
    collection_name: str
    generation_no: int
    chunker_version: str
    status: str
    metrics_json: dict[str, object]
    created_at: datetime


class SpaceIndexPointerResponse(BaseModel):
    """知识空间当前激活索引指针与历史代次列表。"""

    organization_id: uuid.UUID
    space_id: uuid.UUID
    pointer_id: uuid.UUID | None = None
    active_generation_id: uuid.UUID | None = None
    embedding_profile_id: uuid.UUID | None = None
    revision: int = 0
    generations: list[IndexGenerationSummary] = Field(default_factory=list)


class IndexRollbackRequest(BaseModel):
    """蓝绿索引回滚请求体（携带乐观锁 expected_revision）。"""

    expected_revision: int = Field(..., ge=1, description="当前激活指针乐观锁修订号")
    target_generation_id: uuid.UUID | None = Field(
        default=None,
        description="可选指定回滚目标代次 ID；缺省时自动回退至上一代次",
    )
    reason: str = Field(
        default="manual_blue_green_rollback",
        min_length=1,
        max_length=255,
        description="回滚操作原因",
    )


@router.post(
    "/document-versions/{version_id}/process",
    response_model=PipelineExecutionResult,
    summary="触发文档版本解析、切片、向量化与蓝绿建索引流水线",
)
async def post_process_document_version(
    version_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("document:write")),
    session: AsyncSession = Depends(get_db_session),
    pipeline: DocumentIngestionPipeline = Depends(get_ingestion_pipeline),
) -> PipelineExecutionResult:
    """按文档最新敏感等级执行端到端解析、Parent-Child 切片与 Qdrant 蓝绿建索引。"""
    return await pipeline.process_document_version(
        session,
        organization_id=org_ctx.organization_id,
        document_version_id=version_id,
        actor_user_id=org_ctx.principal.user_id,
    )


@router.get(
    "/spaces/{space_id}/pointer",
    response_model=SpaceIndexPointerResponse,
    summary="查询知识空间当前激活索引指针与蓝绿代次列表",
)
async def get_space_index_pointer(
    space_id: uuid.UUID,
    org_ctx: OrgContext = Depends(require_permission("space:read")),
    session: AsyncSession = Depends(get_db_session),
) -> SpaceIndexPointerResponse:
    """读取知识空间当前激活的 ActiveIndexPointer 及相关 IndexGeneration 历史。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        pointer = (
            await session.execute(
                select(ActiveIndexPointer).where(
                    ActiveIndexPointer.organization_id == org_ctx.organization_id,
                    ActiveIndexPointer.scope_type == "KNOWLEDGE_SPACE",
                    ActiveIndexPointer.scope_id == space_id,
                )
            )
        ).scalar_one_or_none()
        if pointer is None:
            raise NotFoundError(
                code="ACTIVE_INDEX_POINTER_NOT_FOUND",
                message="该知识空间尚未构建激活索引指针",
            )

        gens = (
            (
                await session.execute(
                    select(IndexGeneration)
                    .where(
                        IndexGeneration.organization_id == org_ctx.organization_id,
                        IndexGeneration.embedding_profile_id == pointer.embedding_profile_id,
                    )
                    .order_by(IndexGeneration.generation_no.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )

    return SpaceIndexPointerResponse(
        organization_id=org_ctx.organization_id,
        space_id=space_id,
        pointer_id=pointer.id,
        active_generation_id=pointer.index_generation_id,
        embedding_profile_id=pointer.embedding_profile_id,
        revision=pointer.revision,
        generations=[
            IndexGenerationSummary(
                id=g.id,
                embedding_profile_id=g.embedding_profile_id,
                collection_name=g.collection_name,
                generation_no=g.generation_no,
                chunker_version=g.chunker_version,
                status=g.status,
                metrics_json=dict(g.metrics_json or {}),
                created_at=g.created_at,
            )
            for g in gens
        ],
    )


@router.post(
    "/spaces/{space_id}/rollback",
    response_model=ActiveIndexRollbackResult,
    summary="执行蓝绿索引指针原子回滚（带乐观锁校验）",
)
async def post_rollback_space_index(
    space_id: uuid.UUID,
    payload: IndexRollbackRequest,
    org_ctx: OrgContext = Depends(require_permission("space:manage")),
    session: AsyncSession = Depends(get_db_session),
    pipeline: DocumentIngestionPipeline = Depends(get_ingestion_pipeline),
) -> ActiveIndexRollbackResult:
    """在乐观锁保护下将知识空间索引指针回滚至上一代次或指定目标代次。"""
    return await pipeline.rollback_active_index(
        session,
        organization_id=org_ctx.organization_id,
        space_id=space_id,
        expected_revision=payload.expected_revision,
        target_generation_id=payload.target_generation_id,
        actor_user_id=org_ctx.principal.user_id,
        reason=payload.reason,
    )
