"""双选 Embedding Provider（Qwen 官方 API 优先 + 本地 BGE 接口）与敏感度路由。

架构与安全约束（TECHNICAL_SPEC §8.2 & ADR-003）：
1. 提供统一的 `embed_documents(texts, profile)`、`embed_query(query, profile)` 与能力元数据。
2. `QwenEmbeddingProvider` 调用 DashScope OpenAI 兼容 `/embeddings` 接口（默认优先）。
3. `LocalBGEEmbeddingProvider` 调用 `LOCAL_BGE_EMBEDDING_URL`，兼容 OpenAI `/embeddings`
   与 HuggingFace TEI `/embed` 两种本地部署协议，并内置单 GPU 异步信号量 `asyncio.Semaphore`。
4. `EmbeddingRouter` 在所有调用前经过 `DataEgressPolicyEngine` 校验：
   - L0 / 已获组织授权的 L1：按主选配置（默认 Qwen）执行；
   - L2 / L3（或未获外发授权的 L1）：严禁外发 Qwen，若本地 BGE 已启用则自动切换本地 BGE，
     否则严格 Fail-Closed 抛出 `PolicyBlockedError`。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.errors import AppError, PolicyBlockedError
from app.modules.providers.egress import (
    DataEgressPolicyEngine,
    EgressDecision,
    EgressPurpose,
    ProviderScope,
    SensitivityLevel,
    normalize_sensitivity,
)

logger = logging.getLogger(__name__)


class EmbeddingCapability(BaseModel):
    """Embedding Provider 能力元数据（TECHNICAL_SPEC §8.2）。"""

    provider: str = Field(..., description="Provider 唯一标识")
    model_id: str = Field(..., description="默认模型标识")
    dimension: int = Field(..., ge=1, description="向量维度")
    max_input_tokens: int = Field(..., ge=1, description="单条输入最大 Token 数")
    languages: list[str] = Field(default_factory=lambda: ["zh", "en"], description="支持语言列表")
    normalized: bool = Field(default=True, description="输出向量是否完成 L2 归一化")
    max_batch_size: int = Field(default=16, ge=1, description="单次批量请求最大条数")


class EmbeddingProfile(BaseModel):
    """单次向量化调用可选覆盖的 Profile 参数。"""

    provider: Literal["qwen", "local_bge"] | None = Field(
        default=None,
        description="显式指定偏好的 Provider（仍受敏感度外发门禁约束）",
    )
    model_id: str | None = Field(default=None, description="可选覆盖模型 ID")
    model_version: str = Field(default="v1", description="模型配置版本号")
    dimensions: int | None = Field(default=None, ge=64, le=8192, description="可选输出维度")
    max_batch_size: int | None = Field(default=None, ge=1, le=256, description="批大小覆盖")
    normalize: bool = Field(default=True, description="是否对输出向量执行 L2 归一化")


class EmbeddingBatchResult(BaseModel):
    """批量文本向量化结果。"""

    vectors: list[list[float]] = Field(default_factory=list, description="按输入顺序对齐的向量列表")
    provider: str = Field(..., description="实际执行的 Embedding Provider")
    model: str = Field(..., description="实际执行的模型 ID")
    dimensions: int = Field(..., ge=0, description="实际向量维度")
    usage_tokens: int = Field(default=0, ge=0, description="消耗 Token 数")
    latency_ms: float = Field(..., ge=0.0, description="端到端耗时（毫秒）")
    egress_decision: EgressDecision = Field(..., description="本次向量化的外发门禁审计决策")


def _l2_normalize(vector: list[float]) -> list[float]:
    """对单条浮点向量执行 L2 归一化，零向量保持原样。"""
    norm = math.sqrt(sum(val * val for val in vector))
    if norm <= 1e-12:
        return vector
    return [val / norm for val in vector]


class BaseEmbeddingProvider(ABC):
    """Embedding Provider 抽象基类。"""

    provider_name: str
    provider_scope: ProviderScope

    @property
    @abstractmethod
    def capability(self) -> EmbeddingCapability:
        """返回该 Provider 的能力元数据。"""

    @abstractmethod
    async def embed_documents(
        self,
        texts: Sequence[str],
        profile: EmbeddingProfile | None = None,
    ) -> tuple[list[list[float]], int]:
        """批量向量化文档文本，返回 `(向量列表, 消耗 Token 数)`。"""

    async def embed_query(
        self,
        query: str,
        profile: EmbeddingProfile | None = None,
    ) -> tuple[list[float], int]:
        """向量化单条查询文本，返回 `(向量, 消耗 Token 数)`。"""
        vectors, usage = await self.embed_documents([query], profile=profile)
        return (vectors[0] if vectors else []), usage


class QwenEmbeddingProvider(BaseEmbeddingProvider):
    """Qwen 官方 DashScope OpenAI 兼容 `/embeddings` Provider（默认优先）。"""

    provider_name = "qwen_embedding"
    provider_scope = ProviderScope.EXTERNAL

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 45.0,
        max_batch_size: int = 10,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._max_batch_size = max(1, max_batch_size)

    @property
    def capability(self) -> EmbeddingCapability:
        return EmbeddingCapability(
            provider=self.provider_name,
            model_id=self._settings.qwen_embedding_model,
            dimension=self._settings.qwen_embedding_dimensions,
            max_input_tokens=8192,
            languages=["zh", "en", "multilingual"],
            normalized=True,
            max_batch_size=self._max_batch_size,
        )

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        api_key = self._settings.qwen_api_key.get_secret_value().strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def embed_documents(
        self,
        texts: Sequence[str],
        profile: EmbeddingProfile | None = None,
    ) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0

        model_id = (profile.model_id if profile and profile.model_id else None) or (
            self._settings.qwen_embedding_model
        )
        dimensions = (profile.dimensions if profile and profile.dimensions else None) or (
            self._settings.qwen_embedding_dimensions
        )
        batch_size = (
            profile.max_batch_size if profile and profile.max_batch_size else self._max_batch_size
        )
        should_normalize = profile.normalize if profile is not None else True

        url = f"{self._settings.qwen_api_base_url.rstrip('/')}/embeddings"
        all_vectors: list[list[float]] = []
        total_tokens = 0

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds)
        )
        try:
            for offset in range(0, len(texts), batch_size):
                batch = list(texts[offset : offset + batch_size])
                payload = {
                    "model": model_id,
                    "input": batch,
                    "dimensions": dimensions,
                    "encoding_format": "float",
                }
                response = await client.post(url, json=payload, headers=self._build_headers())
                response.raise_for_status()
                body = response.json()

                items = sorted(body.get("data") or [], key=lambda item: int(item.get("index", 0)))
                if len(items) != len(batch):
                    raise AppError(
                        "Qwen Embedding 返回向量数量与输入批次不匹配",
                        code="EMBEDDING_INVALID_RESPONSE",
                        status_code=502,
                    )

                for item in items:
                    vec = [float(x) for x in item["embedding"]]
                    all_vectors.append(_l2_normalize(vec) if should_normalize else vec)

                usage = body.get("usage") or {}
                total_tokens += int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Qwen Embedding HTTP 错误: status=%s model=%s",
                exc.response.status_code,
                model_id,
            )
            raise AppError(
                f"Qwen Embedding 服务异常（HTTP {exc.response.status_code}）",
                code="EMBEDDING_HTTP_ERROR",
                status_code=502,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("Qwen Embedding 网络异常: error=%s", type(exc).__name__)
            raise AppError(
                "Qwen Embedding 网络连接失败",
                code="EMBEDDING_NETWORK_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        return all_vectors, total_tokens


class LocalBGEEmbeddingProvider(BaseEmbeddingProvider):
    """本地部署 BGE Embedding Provider（支持 OpenAI `/embeddings` 与 TEI `/embed` 协议）。

    内置 `asyncio.Semaphore`（默认单 GPU 并发 1），保护本机显存稳定性。
    """

    provider_name = "local_bge_embedding"
    provider_scope = ProviderScope.LOCAL

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        protocol: Literal["auto", "openai", "tei"] = "auto",
        max_concurrency: int = 1,
        max_batch_size: int = 16,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client
        self._protocol = protocol
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._max_batch_size = max(1, max_batch_size)
        self._timeout_seconds = timeout_seconds

    @property
    def capability(self) -> EmbeddingCapability:
        return EmbeddingCapability(
            provider=self.provider_name,
            model_id=self._settings.local_bge_embedding_model,
            dimension=self._settings.local_bge_embedding_dimensions,
            max_input_tokens=8192,
            languages=["zh", "en", "multilingual"],
            normalized=True,
            max_batch_size=self._max_batch_size,
        )

    def _resolve_endpoint_and_protocol(self) -> tuple[str, Literal["openai", "tei"]]:
        base = self._settings.local_bge_embedding_url.strip().rstrip("/")
        if not base:
            raise PolicyBlockedError(
                "本地 BGE Embedding 服务地址未配置",
                code="LOCAL_BGE_EMBEDDING_UNCONFIGURED",
            )
        if self._protocol == "tei" or (self._protocol == "auto" and base.endswith("/embed")):
            url = base if base.endswith("/embed") else f"{base}/embed"
            return url, "tei"
        url = base if base.endswith("/embeddings") else f"{base}/embeddings"
        return url, "openai"

    async def embed_documents(
        self,
        texts: Sequence[str],
        profile: EmbeddingProfile | None = None,
    ) -> tuple[list[list[float]], int]:
        if not texts:
            return [], 0

        url, resolved_protocol = self._resolve_endpoint_and_protocol()
        model_id = (profile.model_id if profile and profile.model_id else None) or (
            self._settings.local_bge_embedding_model
        )
        batch_size = (
            profile.max_batch_size if profile and profile.max_batch_size else self._max_batch_size
        )
        should_normalize = profile.normalize if profile is not None else True

        all_vectors: list[list[float]] = []
        total_tokens = 0

        async with self._semaphore:
            owns_client = self._http_client is None
            client = self._http_client or httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds)
            )
            try:
                for offset in range(0, len(texts), batch_size):
                    batch = list(texts[offset : offset + batch_size])
                    if resolved_protocol == "tei":
                        payload: dict[str, object] = {
                            "inputs": batch,
                            "normalize": should_normalize,
                            "truncate": True,
                        }
                    else:
                        payload = {
                            "model": model_id,
                            "input": batch,
                            "encoding_format": "float",
                        }

                    response = await client.post(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    response.raise_for_status()
                    body = response.json()

                    # 兼容 TEI 直接返回二维浮点列表与 OpenAI 标准 {data: [{embedding, index}]}
                    if isinstance(body, list):
                        raw_vectors = [[float(x) for x in row] for row in body]
                    else:
                        items = sorted(
                            body.get("data") or [],
                            key=lambda item: int(item.get("index", 0)),
                        )
                        raw_vectors = [[float(x) for x in item["embedding"]] for item in items]
                        usage = body.get("usage") or {}
                        total_tokens += int(
                            usage.get("total_tokens") or usage.get("prompt_tokens") or 0
                        )

                    if len(raw_vectors) != len(batch):
                        raise AppError(
                            "本地 BGE Embedding 返回向量数量与输入批次不匹配",
                            code="LOCAL_EMBEDDING_INVALID_RESPONSE",
                            status_code=502,
                        )
                    for vec in raw_vectors:
                        all_vectors.append(_l2_normalize(vec) if should_normalize else vec)
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "本地 BGE Embedding HTTP 错误: status=%s model=%s",
                    exc.response.status_code,
                    model_id,
                )
                raise AppError(
                    f"本地 BGE Embedding 服务异常（HTTP {exc.response.status_code}）",
                    code="LOCAL_EMBEDDING_HTTP_ERROR",
                    status_code=502,
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning("本地 BGE Embedding 网络异常: error=%s", type(exc).__name__)
                raise AppError(
                    "本地 BGE Embedding 网络连接失败",
                    code="LOCAL_EMBEDDING_NETWORK_ERROR",
                    status_code=502,
                ) from exc
            finally:
                if owns_client:
                    await client.aclose()

        return all_vectors, total_tokens


class EmbeddingRouter:
    """双选 Embedding 路由器（Qwen 优先，L2/L3 强制本地 BGE 或 Fail-Closed）。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        egress_engine: DataEgressPolicyEngine | None = None,
        qwen_provider: QwenEmbeddingProvider | None = None,
        local_bge_provider: LocalBGEEmbeddingProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._egress = egress_engine or DataEgressPolicyEngine()
        self._qwen = qwen_provider or QwenEmbeddingProvider(settings=self._settings)
        self._local_bge = local_bge_provider or LocalBGEEmbeddingProvider(settings=self._settings)

    def _is_local_bge_ready(self) -> bool:
        return bool(
            self._settings.local_bge_embedding_enabled
            and self._settings.local_bge_embedding_url.strip()
        )

    def resolve_provider(
        self,
        *,
        sensitivity_level: SensitivityLevel | str,
        org_allow_external_l1: bool = False,
        profile: EmbeddingProfile | None = None,
        organization_id: str | None = None,
        resource_id: str | None = None,
    ) -> tuple[BaseEmbeddingProvider, EgressDecision]:
        """依据敏感等级、外发授权与主备偏好解析实际执行 Provider，并产出外发审计决策。"""
        level = normalize_sensitivity(sensitivity_level)
        preferred = (
            profile.provider
            if profile and profile.provider
            else self._settings.embedding_primary_provider
        )

        # 若明确偏好本地 BGE 且本地服务已启用，直接使用本地 Provider
        if preferred == "local_bge" and self._is_local_bge_ready():
            local_model = (
                profile.model_id if profile and profile.model_id else None
            ) or self._local_bge.capability.model_id
            decision = self._egress.enforce(
                sensitivity_level=level,
                org_allow_external_l1=org_allow_external_l1,
                provider_scope=ProviderScope.LOCAL,
                purpose=EgressPurpose.EMBEDDING,
                provider_name=self._local_bge.provider_name,
                model_name=local_model,
                organization_id=organization_id,
                resource_id=resource_id,
            )
            return self._local_bge, decision

        # 尝试评估是否允许外发外部 Qwen Embedding
        qwen_model = (
            profile.model_id if profile and profile.model_id else None
        ) or self._qwen.capability.model_id
        external_decision = self._egress.evaluate(
            sensitivity_level=level,
            org_allow_external_l1=org_allow_external_l1,
            provider_scope=ProviderScope.EXTERNAL,
            purpose=EgressPurpose.EMBEDDING,
            provider_name=self._qwen.provider_name,
            model_name=qwen_model,
            organization_id=organization_id,
            resource_id=resource_id,
        )
        if external_decision.allowed and preferred == "qwen":
            return self._qwen, external_decision

        # 外部被策略拒绝（如 L2/L3 或未授权 L1），或偏好本地但未在上方命中：仅在本地 BGE 就绪时切换
        if self._is_local_bge_ready():
            local_model = (
                profile.model_id if profile and profile.model_id else None
            ) or self._local_bge.capability.model_id
            local_decision = self._egress.enforce(
                sensitivity_level=level,
                org_allow_external_l1=org_allow_external_l1,
                provider_scope=ProviderScope.LOCAL,
                purpose=EgressPurpose.EMBEDDING,
                provider_name=self._local_bge.provider_name,
                model_name=local_model,
                organization_id=organization_id,
                resource_id=resource_id,
            )
            return self._local_bge, local_decision

        # 若主选为 local_bge 但本地未启用且外发允许，仍禁止悄悄越权外发，要求显式配置
        if preferred == "local_bge" and external_decision.allowed:
            raise PolicyBlockedError(
                "已配置优先使用本地 BGE Embedding，但本地服务未启用，拒绝隐式降级外发",
                code="LOCAL_BGE_EMBEDDING_DISABLED",
                detail={
                    "sensitivity_level": level.value,
                    "preferred_provider": preferred,
                    "local_bge_embedding_enabled": self._settings.local_bge_embedding_enabled,
                },
            )

        # 高敏数据不允许外发且无可用本地 BGE：严格 Fail-Closed
        raise PolicyBlockedError(
            f"{level.value} 敏感内容禁止外发 Qwen Embedding，且本地 BGE 服务未启用，任务已安全阻断",
            code=external_decision.reason_code,
            detail={
                "egress_decision": external_decision.model_dump(mode="json"),
                "local_bge_embedding_enabled": self._settings.local_bge_embedding_enabled,
            },
        )

    async def embed_documents(
        self,
        texts: Sequence[str],
        *,
        sensitivity_level: SensitivityLevel | str = SensitivityLevel.L0,
        org_allow_external_l1: bool = False,
        profile: EmbeddingProfile | None = None,
        organization_id: str | None = None,
        resource_id: str | None = None,
    ) -> EmbeddingBatchResult:
        """路由并执行文档切片批量向量化。"""
        start_ts = time.perf_counter()
        provider, decision = self.resolve_provider(
            sensitivity_level=sensitivity_level,
            org_allow_external_l1=org_allow_external_l1,
            profile=profile,
            organization_id=organization_id,
            resource_id=resource_id,
        )
        vectors, usage_tokens = await provider.embed_documents(texts, profile=profile)
        latency_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
        dims = len(vectors[0]) if vectors else provider.capability.dimension
        return EmbeddingBatchResult(
            vectors=vectors,
            provider=provider.provider_name,
            model=decision.model_name or provider.capability.model_id,
            dimensions=dims,
            usage_tokens=usage_tokens,
            latency_ms=latency_ms,
            egress_decision=decision,
        )

    async def embed_query(
        self,
        query: str,
        *,
        sensitivity_level: SensitivityLevel | str = SensitivityLevel.L0,
        org_allow_external_l1: bool = False,
        profile: EmbeddingProfile | None = None,
        organization_id: str | None = None,
        resource_id: str | None = None,
    ) -> EmbeddingBatchResult:
        """路由并执行单条查询向量化。"""
        return await self.embed_documents(
            [query],
            sensitivity_level=sensitivity_level,
            org_allow_external_l1=org_allow_external_l1,
            profile=profile,
            organization_id=organization_id,
            resource_id=resource_id,
        )
