"""Provider 与外发策略脱敏配置路由（`/api/v1/providers/config`）。

安全红线（TECHNICAL_SPEC §3.3 / §14.3 & ADR-003）：
1. 任何 API Key（`LLM_API_KEY`、`QWEN_API_KEY`、`MINERU_API_KEY`）严禁在响应中返回明文，
   仅允许返回 `has_api_key_configured: bool` 与脱敏尾号（如 `****1234`）。
2. 所有修改操作必须通过 `require_permission("system:provider:manage")` 授权，并写入安全审计事件。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ForbiddenError
from app.db.session import get_db_session, org_rls_session
from app.modules.analytics.models import OrganizationProviderPolicy
from app.modules.audit.service import record_security_audit_event
from app.modules.identity.deps import OrgContext, require_org_context, require_permission

router = APIRouter(prefix="/providers", tags=["Providers & Egress Policy"])


def _mask_secret_status(secret: SecretStr | None) -> tuple[bool, str | None]:
    """安全检测密钥是否已配置，仅返回布尔值与末 4 位脱敏提示，绝不暴露明文或前缀。"""
    if secret is None:
        return False, None
    raw = secret.get_secret_value().strip()
    if not raw or raw.startswith("YOUR_") or raw.startswith("CHANGE_ME"):
        return False, None
    if len(raw) <= 6:
        return True, "****"
    return True, f"****{raw[-4:]}"


def _normalize_provider_label(raw_value: str) -> str:
    """将配置值统一映射为前端展示规范标识（QWEN_API / LOCAL_BGE）。"""
    cleaned = raw_value.strip().lower()
    if cleaned in {"local_bge", "local-bge"}:
        return "LOCAL_BGE"
    return "QWEN_API"


def _normalize_provider_storage(raw_value: str) -> str:
    """将请求参数规范化为内部存储枚举（qwen / local_bge）。"""
    cleaned = raw_value.strip().lower()
    if cleaned in {"local_bge", "local-bge"}:
        return "local_bge"
    return "qwen"


class LLMProviderInfo(BaseModel):
    """LLM Provider 脱敏配置视图。"""

    provider_type: str
    base_url: str
    model: str
    has_api_key_configured: bool
    masked_key_hint: str | None = None


class EmbeddingProviderInfo(BaseModel):
    """Embedding 双选路由配置视图。"""

    primary_provider: str
    qwen_model: str
    has_qwen_key_configured: bool
    masked_qwen_key_hint: str | None = None
    local_bge_enabled: bool
    local_bge_endpoint: str


class RerankerProviderInfo(BaseModel):
    """Reranker 双选路由配置视图。"""

    primary_provider: str
    model: str
    local_bge_enabled: bool


class ParserProviderInfo(BaseModel):
    """文档解析（MinerU + Local OCR）脱敏配置视图。"""

    mineru_base_url: str
    mineru_api_version: str
    has_mineru_key_configured: bool
    masked_mineru_key_hint: str | None = None
    local_ocr_enabled: bool


class EgressPolicyMatrixRow(BaseModel):
    """L0~L3 敏感等级外发策略矩阵条目。"""

    level: Literal["L0", "L1", "L2", "L3"]
    label: str
    allow_external_parse: bool
    allow_external_llm: bool
    note: str


class ProviderConfigResponse(BaseModel):
    """`GET/PATCH /api/v1/providers/config` 统一脱敏响应体。"""

    llm: LLMProviderInfo
    embedding: EmbeddingProviderInfo
    reranker: RerankerProviderInfo
    parser: ParserProviderInfo
    allow_l1_external_egress: bool
    egress_matrix: list[EgressPolicyMatrixRow]


class ProviderConfigUpdateRequest(BaseModel):
    """更新组织级 Provider 路由偏好与本地开关（绝不接收明文密钥）。"""

    allow_l1_external_egress: bool | None = None
    embedding_primary_provider: str | None = Field(default=None, max_length=32)
    reranker_primary_provider: str | None = Field(default=None, max_length=32)
    local_bge_enabled: bool | None = None
    local_ocr_enabled: bool | None = None
    llm_base_url: str | None = Field(default=None, max_length=255)
    llm_model: str | None = Field(default=None, max_length=128)


def _build_response(
    *,
    policy: OrganizationProviderPolicy | None,
    settings: Settings,
) -> ProviderConfigResponse:
    """组装绝不含明文密钥的脱敏配置响应。"""
    llm_ok, llm_hint = _mask_secret_status(settings.llm_api_key)
    qwen_ok, qwen_hint = _mask_secret_status(settings.qwen_api_key)
    mineru_ok, mineru_hint = _mask_secret_status(settings.mineru_api_key)

    allow_l1 = policy.allow_l1_external_egress if policy else True
    emb_primary = _normalize_provider_label(
        policy.embedding_primary_provider if policy else settings.embedding_primary_provider
    )
    rerank_primary = _normalize_provider_label(
        policy.reranker_primary_provider if policy else settings.reranker_primary_provider
    )
    local_bge = policy.local_bge_enabled if policy else settings.local_bge_embedding_enabled
    local_ocr = policy.local_ocr_enabled if policy else settings.local_ocr_enabled
    llm_base_url = (
        policy.llm_base_url_override
        if policy and policy.llm_base_url_override
        else settings.llm_base_url
    )
    llm_model = (
        policy.llm_model_override
        if policy and policy.llm_model_override
        else settings.llm_default_model
    )

    egress_matrix = [
        EgressPolicyMatrixRow(
            level="L0",
            label="Public 公开",
            allow_external_parse=True,
            allow_external_llm=True,
            note="允许外部 MinerU / Qwen / LLM Provider",
        ),
        EgressPolicyMatrixRow(
            level="L1",
            label="Internal 内部",
            allow_external_parse=allow_l1,
            allow_external_llm=allow_l1,
            note="需当前 Organization 明确授权后允许外发"
            if allow_l1
            else "当前组织已收紧 L1 策略，仅限本地处理",
        ),
        EgressPolicyMatrixRow(
            level="L2",
            label="Confidential 机密",
            allow_external_parse=False,
            allow_external_llm=False,
            note="默认本地处理（Local BGE / Local OCR），未启用则 Fail-Closed 阻断",
        ),
        EgressPolicyMatrixRow(
            level="L3",
            label="Restricted 受限",
            allow_external_parse=False,
            allow_external_llm=False,
            note="严格禁止任何外部模型与解析服务外发",
        ),
    ]

    return ProviderConfigResponse(
        llm=LLMProviderInfo(
            provider_type=settings.llm_provider,
            base_url=llm_base_url,
            model=llm_model,
            has_api_key_configured=llm_ok,
            masked_key_hint=llm_hint,
        ),
        embedding=EmbeddingProviderInfo(
            primary_provider=emb_primary,
            qwen_model=settings.qwen_embedding_model,
            has_qwen_key_configured=qwen_ok,
            masked_qwen_key_hint=qwen_hint,
            local_bge_enabled=local_bge,
            local_bge_endpoint=settings.local_bge_embedding_url if local_bge else "未启用",
        ),
        reranker=RerankerProviderInfo(
            primary_provider=rerank_primary,
            model=(
                settings.local_bge_rerank_model
                if rerank_primary == "LOCAL_BGE"
                else settings.qwen_rerank_model
            ),
            local_bge_enabled=local_bge,
        ),
        parser=ParserProviderInfo(
            mineru_base_url=settings.mineru_api_base_url,
            mineru_api_version=settings.mineru_api_version,
            has_mineru_key_configured=mineru_ok,
            masked_mineru_key_hint=mineru_hint,
            local_ocr_enabled=local_ocr,
        ),
        allow_l1_external_egress=allow_l1,
        egress_matrix=egress_matrix,
    )


@router.get(
    "/config",
    response_model=ProviderConfigResponse,
    summary="查询脱敏后的 Provider 与 L0-L3 外发策略配置",
)
async def get_providers_config(
    org_ctx: OrgContext = Depends(require_org_context),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ProviderConfigResponse:
    """读取当前组织 Provider 路由与脱敏状态（严禁普通成员以外人员篡改，绝不返回明文密钥）。"""
    if not (
        org_ctx.has_permission("system:provider:manage") or org_ctx.has_permission("analytics:read")
    ):
        raise ForbiddenError(
            code="PERMISSION_DENIED",
            message="当前角色无权查看系统 Provider 与外发策略配置",
        )

    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        policy = (
            await session.execute(
                select(OrganizationProviderPolicy).where(
                    OrganizationProviderPolicy.organization_id == org_ctx.organization_id
                )
            )
        ).scalar_one_or_none()

    return _build_response(policy=policy, settings=settings)


@router.patch(
    "/config",
    response_model=ProviderConfigResponse,
    summary="更新组织级 Provider 路由偏好与外发开关",
)
async def patch_providers_config(
    payload: ProviderConfigUpdateRequest,
    org_ctx: OrgContext = Depends(require_permission("system:provider:manage")),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ProviderConfigResponse:
    """更新当前组织的外发授权与模型路由偏好（严禁传入或存储明文密钥）。"""
    async with org_rls_session(session, org_ctx.organization_id, user_id=org_ctx.principal.user_id):
        policy = (
            await session.execute(
                select(OrganizationProviderPolicy).where(
                    OrganizationProviderPolicy.organization_id == org_ctx.organization_id
                )
            )
        ).scalar_one_or_none()
        if policy is None:
            policy = OrganizationProviderPolicy(
                organization_id=org_ctx.organization_id,
                allow_l1_external_egress=True,
                embedding_primary_provider=settings.embedding_primary_provider,
                reranker_primary_provider=settings.reranker_primary_provider,
                local_bge_enabled=settings.local_bge_embedding_enabled,
                local_ocr_enabled=settings.local_ocr_enabled,
            )
            session.add(policy)
            await session.flush()

        if payload.allow_l1_external_egress is not None:
            policy.allow_l1_external_egress = payload.allow_l1_external_egress
        if payload.embedding_primary_provider is not None:
            policy.embedding_primary_provider = _normalize_provider_storage(
                payload.embedding_primary_provider
            )
        if payload.reranker_primary_provider is not None:
            policy.reranker_primary_provider = _normalize_provider_storage(
                payload.reranker_primary_provider
            )
        if payload.local_bge_enabled is not None:
            policy.local_bge_enabled = payload.local_bge_enabled
        if payload.local_ocr_enabled is not None:
            policy.local_ocr_enabled = payload.local_ocr_enabled
        if payload.llm_base_url is not None:
            policy.llm_base_url_override = payload.llm_base_url.strip() or None
        if payload.llm_model is not None:
            policy.llm_model_override = payload.llm_model.strip() or None

        record_security_audit_event(
            session,
            organization_id=org_ctx.organization_id,
            actor_user_id=org_ctx.principal.user_id,
            event_type="provider.policy.updated",
            severity="HIGH",
            resource_type="organization_provider_policy",
            resource_id=policy.id,
            details={
                "allow_l1_external_egress": policy.allow_l1_external_egress,
                "embedding_primary_provider": policy.embedding_primary_provider,
                "local_bge_enabled": policy.local_bge_enabled,
                "local_ocr_enabled": policy.local_ocr_enabled,
            },
        )

    return _build_response(policy=policy, settings=settings)
