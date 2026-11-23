"""双选 Reranker Provider（Qwen 官方 API 优先 + 本地 BGE Reranker）与敏感度路由。

安全与架构约束（TECHNICAL_SPEC §9.2 & ADR-003）：
1. 仅允许已经通过 PostgreSQL 权威 ACL 复核的候选进入 Reranker。
2. `RerankerRouter` 必须按本批候选中的**最高文档敏感等级**执行外发策略检查：
   - L0 / 已获组织授权的 L1：默认优先路由 `QwenRerankerProvider`（DashScope `text-rerank`）；
   - L2 / L3（或未获外发授权的 L1）：绝不发送至外部 Qwen Reranker；若本地 BGE 可用则自动切本地，
     否则根据调用模式执行 Fail-Closed 拦截（默认）或回退为已授权 RRF 排序（绝不外发）。
3. 本地 BGE Reranker 内置单 GPU 异步并发信号量（`asyncio.Semaphore`），避免 RTX 4060 8GB 显存打满。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

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
    highest_sensitivity,
)

logger = logging.getLogger(__name__)


class RerankCandidate(BaseModel):
    """待重排的已授权文档切片候选。"""

    chunk_id: str = Field(..., description="切片唯一 ID")
    text: str = Field(..., description="已通过 ACL 校验的切片正文")
    sensitivity_level: SensitivityLevel = Field(
        default=SensitivityLevel.L0,
        description="来源文档敏感等级",
    )
    initial_score: float = Field(default=0.0, description="进入重排前的 RRF/融合初始分")
    metadata: dict[str, Any] = Field(default_factory=dict, description="透传安全元数据")


class RerankItem(BaseModel):
    """单条重排结果。"""

    chunk_id: str = Field(..., description="切片唯一 ID")
    index: int = Field(..., ge=0, description="在输入候选列表中的原始索引")
    relevance_score: float = Field(..., description="相关性重排得分")
    initial_score: float = Field(default=0.0, description="原始 RRF 得分")
    sensitivity_level: SensitivityLevel = Field(..., description="敏感等级")
    metadata: dict[str, Any] = Field(default_factory=dict, description="透传元数据")


class RerankResult(BaseModel):
    """批量重排响应结果与审计信息。"""

    items: list[RerankItem] = Field(default_factory=list, description="按相关性降序排列的重排列表")
    provider: str = Field(..., description="实际执行的 Reranker Provider 名称")
    model: str = Field(..., description="实际模型标识")
    latency_ms: float = Field(..., ge=0.0, description="重排耗时（毫秒）")
    fallback_used: bool = Field(
        default=False,
        description="是否因本地服务未启用而安全降级为授权 RRF 排序",
    )
    egress_decision: EgressDecision = Field(..., description="本次重排的外发门禁审计决策")


class BaseRerankerProvider(ABC):
    """Reranker Provider 抽象基类。"""

    provider_name: str
    provider_scope: ProviderScope

    @property
    @abstractmethod
    def model_name(self) -> str:
        """返回当前配置的模型名称。"""

    @abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        """对候选文档计算相关性得分，返回 `(原索引, 得分)` 降序列表。"""


class QwenRerankerProvider(BaseRerankerProvider):
    """Qwen 官方 DashScope `text-rerank` 重排 Provider（默认优先）。"""

    provider_name = "qwen_reranker"
    provider_scope = ProviderScope.EXTERNAL

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    @property
    def model_name(self) -> str:
        return self._settings.qwen_rerank_model

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        if not candidates:
            return []

        api_key = self._settings.qwen_api_key.get_secret_value().strip()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        effective_top_n = min(top_n, len(candidates)) if top_n else len(candidates)
        payload = {
            "model": self.model_name,
            "input": {
                "query": query,
                "documents": [item.text for item in candidates],
            },
            "parameters": {
                "top_n": effective_top_n,
                "return_documents": False,
            },
        }

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds)
        )
        try:
            response = await client.post(
                self._settings.qwen_rerank_base_url,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Qwen Reranker HTTP 错误: status=%s model=%s",
                exc.response.status_code,
                self.model_name,
            )
            raise AppError(
                f"Qwen Reranker 服务返回错误（HTTP {exc.response.status_code}）",
                code="RERANKER_HTTP_ERROR",
                status_code=502,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("Qwen Reranker 网络异常: error=%s", type(exc).__name__)
            raise AppError(
                "Qwen Reranker 网络连接失败",
                code="RERANKER_NETWORK_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        raw_results = (data.get("output") or {}).get("results")
        if raw_results is None:
            raw_results = data.get("results") or []

        scored: list[tuple[int, float]] = []
        for row in raw_results:
            idx = int(row["index"])
            score = float(row.get("relevance_score", row.get("score", 0.0)))
            if 0 <= idx < len(candidates):
                scored.append((idx, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:effective_top_n]


class LocalBGERerankerProvider(BaseRerankerProvider):
    """本地部署 BGE Reranker Provider（兼容 `/v1/rerank` 与 TEI `/rerank`，内置单 GPU 信号量）。"""

    provider_name = "local_bge_reranker"
    provider_scope = ProviderScope.LOCAL

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
        max_concurrency: int = 1,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._http_client = http_client
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._timeout_seconds = timeout_seconds

    @property
    def model_name(self) -> str:
        return self._settings.local_bge_rerank_model

    @property
    def max_concurrency(self) -> int:
        """暴露并发槽位数，便于测试与容量观测。"""
        return self._semaphore._value  # noqa: SLF001

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_n: int | None = None,
    ) -> list[tuple[int, float]]:
        if not candidates:
            return []

        endpoint = self._settings.local_bge_rerank_url.strip()
        if not endpoint:
            raise PolicyBlockedError(
                "本地 BGE Reranker 服务地址未配置，无法执行本地重排",
                code="LOCAL_BGE_RERANK_UNCONFIGURED",
            )

        effective_top_n = min(top_n, len(candidates)) if top_n else len(candidates)
        payload = {
            "model": self.model_name,
            "query": query,
            "documents": [item.text for item in candidates],
            "texts": [item.text for item in candidates],
            "top_n": effective_top_n,
        }

        async with self._semaphore:
            owns_client = self._http_client is None
            client = self._http_client or httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout_seconds)
            )
            try:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "本地 BGE Reranker HTTP 错误: status=%s model=%s",
                    exc.response.status_code,
                    self.model_name,
                )
                raise AppError(
                    f"本地 BGE Reranker 服务异常（HTTP {exc.response.status_code}）",
                    code="LOCAL_RERANKER_HTTP_ERROR",
                    status_code=502,
                ) from exc
            except httpx.HTTPError as exc:
                logger.warning("本地 BGE Reranker 网络异常: error=%s", type(exc).__name__)
                raise AppError(
                    "本地 BGE Reranker 连接失败",
                    code="LOCAL_RERANKER_NETWORK_ERROR",
                    status_code=502,
                ) from exc
            finally:
                if owns_client:
                    await client.aclose()

        raw_results = data if isinstance(data, list) else data.get("results") or []
        scored: list[tuple[int, float]] = []
        for row in raw_results:
            idx = int(row["index"])
            score = float(row.get("relevance_score", row.get("score", 0.0)))
            if 0 <= idx < len(candidates):
                scored.append((idx, score))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:effective_top_n]


class RerankerRouter:
    """双选 Reranker 路由器：强制按本批最高敏感等级校验外发策略。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        egress_engine: DataEgressPolicyEngine | None = None,
        qwen_provider: QwenRerankerProvider | None = None,
        local_bge_provider: LocalBGERerankerProvider | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._egress = egress_engine or DataEgressPolicyEngine()
        self._qwen = qwen_provider or QwenRerankerProvider(settings=self._settings)
        self._local_bge = local_bge_provider or LocalBGERerankerProvider(settings=self._settings)

    def _is_local_bge_ready(self) -> bool:
        return bool(
            self._settings.local_bge_rerank_enabled and self._settings.local_bge_rerank_url.strip()
        )

    def _build_rrf_fallback(
        self,
        candidates: Sequence[RerankCandidate],
        *,
        top_n: int | None,
        decision: EgressDecision,
        latency_ms: float,
    ) -> RerankResult:
        """在不外发任何外部模型的前提下，仅按已授权 RRF 得分降序返回安全降级结果。"""
        indexed = sorted(
            enumerate(candidates),
            key=lambda pair: pair[1].initial_score,
            reverse=True,
        )
        limit = min(top_n, len(indexed)) if top_n else len(indexed)
        items = [
            RerankItem(
                chunk_id=cand.chunk_id,
                index=orig_idx,
                relevance_score=cand.initial_score,
                initial_score=cand.initial_score,
                sensitivity_level=cand.sensitivity_level,
                metadata=cand.metadata,
            )
            for orig_idx, cand in indexed[:limit]
        ]
        return RerankResult(
            items=items,
            provider="authorized_rrf_fallback",
            model="rrf-only",
            latency_ms=latency_ms,
            fallback_used=True,
            egress_decision=decision,
        )

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_n: int | None = None,
        org_allow_external_l1: bool = False,
        sensitivity_override: SensitivityLevel | str | None = None,
        allow_rrf_fallback: bool = False,
        organization_id: str | None = None,
    ) -> RerankResult:
        """按本批最高敏感等级路由至 Qwen 或本地 BGE 重排，严防高敏数据外泄。"""
        start_ts = time.perf_counter()

        # 取本批候选与显式覆盖中的最高敏感等级作为外发门禁依据
        all_levels: list[SensitivityLevel | str] = [c.sensitivity_level for c in candidates]
        if sensitivity_override is not None:
            all_levels.append(sensitivity_override)
        batch_max_level = highest_sensitivity(all_levels, default=SensitivityLevel.L0)

        if not candidates:
            empty_decision = self._egress.evaluate(
                sensitivity_level=batch_max_level,
                org_allow_external_l1=org_allow_external_l1,
                provider_scope=ProviderScope.LOCAL,
                purpose=EgressPurpose.RERANK,
                provider_name="noop_empty_reranker",
                model_name="none",
                organization_id=organization_id,
            )
            return RerankResult(
                items=[],
                provider="noop_empty_reranker",
                model="none",
                latency_ms=0.0,
                fallback_used=False,
                egress_decision=empty_decision,
            )

        selected_provider: BaseRerankerProvider | None = None
        final_decision: EgressDecision | None = None

        # 优先根据主选策略与外发门禁决定目标 Provider
        if self._settings.reranker_primary_provider == "local_bge" and self._is_local_bge_ready():
            selected_provider = self._local_bge
            final_decision = self._egress.enforce(
                sensitivity_level=batch_max_level,
                org_allow_external_l1=org_allow_external_l1,
                provider_scope=ProviderScope.LOCAL,
                purpose=EgressPurpose.RERANK,
                provider_name=self._local_bge.provider_name,
                model_name=self._local_bge.model_name,
                organization_id=organization_id,
            )
        else:
            # 评估是否允许外发至外部 Qwen Reranker
            qwen_decision = self._egress.evaluate(
                sensitivity_level=batch_max_level,
                org_allow_external_l1=org_allow_external_l1,
                provider_scope=ProviderScope.EXTERNAL,
                purpose=EgressPurpose.RERANK,
                provider_name=self._qwen.provider_name,
                model_name=self._qwen.model_name,
                organization_id=organization_id,
            )
            if qwen_decision.allowed:
                selected_provider = self._qwen
                final_decision = qwen_decision
            elif self._is_local_bge_ready():
                # 高敏数据（如 L2/L3 或未授权 L1）禁止外发，自动切换至本地 BGE Reranker
                selected_provider = self._local_bge
                final_decision = self._egress.enforce(
                    sensitivity_level=batch_max_level,
                    org_allow_external_l1=org_allow_external_l1,
                    provider_scope=ProviderScope.LOCAL,
                    purpose=EgressPurpose.RERANK,
                    provider_name=self._local_bge.provider_name,
                    model_name=self._local_bge.model_name,
                    organization_id=organization_id,
                )
            else:
                # 既不允许外发，本地 BGE 又未启用：严格拦截或按显式授权退回纯 RRF 排序
                elapsed_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)
                if allow_rrf_fallback:
                    return self._build_rrf_fallback(
                        candidates,
                        top_n=top_n,
                        decision=qwen_decision,
                        latency_ms=elapsed_ms,
                    )
                raise PolicyBlockedError(
                    f"{batch_max_level.value} 敏感候选禁止外发外部 Qwen Reranker，"
                    "且本地 BGE Reranker 未启用，重排任务已安全阻断",
                    code=qwen_decision.reason_code,
                    detail={
                        "egress_decision": qwen_decision.model_dump(mode="json"),
                        "local_bge_rerank_enabled": self._settings.local_bge_rerank_enabled,
                    },
                )

        assert selected_provider is not None and final_decision is not None
        scored_pairs = await selected_provider.rerank(query, candidates, top_n=top_n)
        latency_ms = round((time.perf_counter() - start_ts) * 1000.0, 2)

        items = [
            RerankItem(
                chunk_id=candidates[idx].chunk_id,
                index=idx,
                relevance_score=score,
                initial_score=candidates[idx].initial_score,
                sensitivity_level=candidates[idx].sensitivity_level,
                metadata=candidates[idx].metadata,
            )
            for idx, score in scored_pairs
        ]
        return RerankResult(
            items=items,
            provider=selected_provider.provider_name,
            model=selected_provider.model_name,
            latency_ms=latency_ms,
            fallback_used=False,
            egress_decision=final_decision,
        )
