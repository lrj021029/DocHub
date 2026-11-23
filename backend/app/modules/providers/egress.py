"""数据外发门禁策略引擎（Data Egress Policy Engine）。

安全红线（TECHNICAL_SPEC §6.7 & ADR-003）：
1. 任何外部或本地模型/解析器调用前，必须先执行统一外发策略检查并生成结构化审计决策。
2. 敏感等级严格遵循以下基线（组织只能收紧，不得放松）：
   - L0（公开）：允许调用外部 Provider；
   - L1（内部）：仅在组织显式授权 `org_allow_external_l1=True` 时允许外发，否则禁止外发；
   - L2（机密）：默认仅允许本地处理，禁止外发外部 Provider；
   - L3（绝密）：绝对禁止外发外部 Provider。
3. 严禁将密钥、文档正文或未授权资源标识写入日志或异常详情。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.core.errors import PolicyBlockedError
from app.core.ids import new_uuid7_str

logger = logging.getLogger(__name__)

POLICY_VERSION = "egress-policy-v1.0"


class SensitivityLevel(StrEnum):
    """文档与内容敏感等级枚举（从低到高：L0 < L1 < L2 < L3）。"""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class ProviderScope(StrEnum):
    """Provider 部署边界（外部云端 vs 本地私有部署）。"""

    EXTERNAL = "external"
    LOCAL = "local"


class EgressPurpose(StrEnum):
    """数据流转业务用途枚举。"""

    PARSE = "parse"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    GENERATION = "generation"


_SENSITIVITY_RANK: dict[SensitivityLevel, int] = {
    SensitivityLevel.L0: 0,
    SensitivityLevel.L1: 1,
    SensitivityLevel.L2: 2,
    SensitivityLevel.L3: 3,
}


def normalize_sensitivity(value: SensitivityLevel | str | None) -> SensitivityLevel:
    """将字符串或枚举规范化为 SensitivityLevel；无法识别时按最高敏感等级 L3 Fail-Closed。"""
    if isinstance(value, SensitivityLevel):
        return value
    if value is None:
        return SensitivityLevel.L3
    normalized = str(value).strip().upper()
    try:
        return SensitivityLevel(normalized)
    except ValueError:
        logger.warning("收到未知敏感等级标识，按安全基线 Fail-Closed 降级为 L3")
        return SensitivityLevel.L3


def highest_sensitivity(
    levels: Iterable[SensitivityLevel | str | None],
    *,
    default: SensitivityLevel = SensitivityLevel.L0,
) -> SensitivityLevel:
    """计算一批文档/切片中的最高敏感等级，避免高敏数据随低敏批次泄漏。"""
    resolved = [normalize_sensitivity(item) for item in levels]
    if not resolved:
        return default
    return max(resolved, key=lambda lvl: _SENSITIVITY_RANK[lvl])


class EgressCheckRequest(BaseModel):
    """单次外发门禁策略校验入参。"""

    sensitivity_level: SensitivityLevel = Field(..., description="待处理数据的最高敏感等级")
    org_allow_external_l1: bool = Field(
        default=False,
        description="当前组织是否显式授权 L1 级数据外发给外部 Provider",
    )
    provider_scope: ProviderScope = Field(
        ..., description="目标 Provider 部署边界（external/local）"
    )
    purpose: EgressPurpose = Field(..., description="调用用途（parse/embedding/rerank/generation）")
    provider_name: str = Field(..., min_length=1, description="目标 Provider 标识")
    model_name: str = Field(default="", description="目标模型或解析引擎版本名称")
    organization_id: str | None = Field(default=None, description="可选组织标识（用于审计归属）")
    resource_id: str | None = Field(
        default=None, description="可选关联资源 ID（如 document_version_id）"
    )


class EgressDecision(BaseModel):
    """结构化外发门禁判定结果（同时作为审计留痕载荷）。"""

    call_id: str = Field(default_factory=new_uuid7_str, description="本次策略判定唯一调用 ID")
    allowed: bool = Field(..., description="是否允许放行")
    reason_code: str = Field(..., description="机器可读判定码")
    reason_message: str = Field(..., description="面向调用方与审计的中文原因说明")
    policy_version: str = Field(default=POLICY_VERSION, description="外发策略规则版本号")
    sensitivity_level: SensitivityLevel = Field(..., description="判定依据的敏感等级")
    org_allow_external_l1: bool = Field(..., description="当时组织 L1 外发授权状态")
    provider_scope: ProviderScope = Field(..., description="目标 Provider 边界")
    purpose: EgressPurpose = Field(..., description="数据用途")
    provider_name: str = Field(..., description="目标 Provider 名称")
    model_name: str = Field(default="", description="目标模型名称")
    checked_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="判定 UTC 时间戳",
    )
    audit_metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="脱敏后的结构化审计元数据",
    )


class DataEgressPolicyEngine:
    """统一数据外发门禁引擎（支持 evaluate 软判断与 enforce/check 强制阻断）。"""

    def __init__(self, *, policy_version: str = POLICY_VERSION) -> None:
        self.policy_version = policy_version

    def evaluate(
        self,
        *,
        sensitivity_level: SensitivityLevel | str,
        org_allow_external_l1: bool,
        provider_scope: ProviderScope | str,
        purpose: EgressPurpose | str,
        provider_name: str,
        model_name: str = "",
        organization_id: str | None = None,
        resource_id: str | None = None,
    ) -> EgressDecision:
        """评估一次模型/解析器调用是否符合敏感度外发门禁规则，不抛出异常。"""
        level = normalize_sensitivity(sensitivity_level)
        scope = (
            provider_scope
            if isinstance(provider_scope, ProviderScope)
            else ProviderScope(str(provider_scope).strip().lower())
        )
        purp = (
            purpose
            if isinstance(purpose, EgressPurpose)
            else EgressPurpose(str(purpose).strip().lower())
        )

        allowed = False
        reason_code = "EGRESS_DENIED_DEFAULT"
        reason_message = "默认拒绝外发请求"

        if scope == ProviderScope.LOCAL:
            allowed = True
            reason_code = "ALLOW_LOCAL_PROVIDER"
            reason_message = f"本地 Provider（{provider_name}）允许处理 {level.value} 级数据"
        else:
            # 外部 Provider 严格按 L0/L1/L2/L3 敏感等级门禁判定
            if level == SensitivityLevel.L0:
                allowed = True
                reason_code = "ALLOW_EXTERNAL_L0"
                reason_message = f"L0 公开数据允许外发至外部 Provider（{provider_name}）"
            elif level == SensitivityLevel.L1:
                if org_allow_external_l1:
                    allowed = True
                    reason_code = "ALLOW_EXTERNAL_L1_ORG_APPROVED"
                    reason_message = (
                        f"L1 内部数据已获得组织显式授权，允许外发至外部 Provider（{provider_name}）"
                    )
                else:
                    allowed = False
                    reason_code = "EGRESS_L1_ORG_APPROVAL_REQUIRED"
                    reason_message = (
                        f"L1 内部数据未获得组织外发授权，禁止外发至外部 Provider（{provider_name}）"
                    )
            elif level == SensitivityLevel.L2:
                allowed = False
                reason_code = "EGRESS_L2_LOCAL_ONLY"
                reason_message = (
                    f"L2 机密数据默认仅允许本地处理，禁止外发至外部 Provider（{provider_name}）"
                )
            else:
                allowed = False
                reason_code = "EGRESS_L3_FORBIDDEN"
                reason_message = f"L3 绝密数据绝对禁止外发至外部 Provider（{provider_name}）"

        call_id = new_uuid7_str()
        audit_metadata: dict[str, Any] = {
            "call_id": call_id,
            "policy_version": self.policy_version,
            "provider": provider_name,
            "provider_scope": scope.value,
            "model": model_name,
            "purpose": purp.value,
            "sensitivity_level": level.value,
            "org_allow_external_l1": org_allow_external_l1,
            "allowed": allowed,
            "reason_code": reason_code,
        }
        if organization_id:
            audit_metadata["organization_id"] = organization_id
        if resource_id:
            audit_metadata["resource_id"] = resource_id

        decision = EgressDecision(
            call_id=call_id,
            allowed=allowed,
            reason_code=reason_code,
            reason_message=reason_message,
            policy_version=self.policy_version,
            sensitivity_level=level,
            org_allow_external_l1=org_allow_external_l1,
            provider_scope=scope,
            purpose=purp,
            provider_name=provider_name,
            model_name=model_name,
            audit_metadata=audit_metadata,
        )

        # 仅记录安全审计字段，严禁记录文档正文或密钥
        if allowed:
            logger.info("外发门禁校验通过", extra={"egress_decision": audit_metadata})
        else:
            logger.warning("外发门禁拦截请求", extra={"egress_decision": audit_metadata})

        return decision

    def enforce(
        self,
        *,
        sensitivity_level: SensitivityLevel | str,
        org_allow_external_l1: bool,
        provider_scope: ProviderScope | str,
        purpose: EgressPurpose | str,
        provider_name: str,
        model_name: str = "",
        organization_id: str | None = None,
        resource_id: str | None = None,
    ) -> EgressDecision:
        """强制执行外发门禁校验；若被拒绝则抛出携带结构化审计详情的 PolicyBlockedError。"""
        decision = self.evaluate(
            sensitivity_level=sensitivity_level,
            org_allow_external_l1=org_allow_external_l1,
            provider_scope=provider_scope,
            purpose=purpose,
            provider_name=provider_name,
            model_name=model_name,
            organization_id=organization_id,
            resource_id=resource_id,
        )
        if not decision.allowed:
            raise PolicyBlockedError(
                decision.reason_message,
                code=decision.reason_code,
                detail=decision.model_dump(mode="json"),
            )
        return decision

    def check(
        self,
        request: EgressCheckRequest,
        *,
        raise_on_deny: bool = True,
    ) -> EgressDecision:
        """基于请求对象的便捷入口，兼容 ADR-003 中的 `EgressPolicyEngine.check()` 调用语义。"""
        fn = self.enforce if raise_on_deny else self.evaluate
        return fn(
            sensitivity_level=request.sensitivity_level,
            org_allow_external_l1=request.org_allow_external_l1,
            provider_scope=request.provider_scope,
            purpose=request.purpose,
            provider_name=request.provider_name,
            model_name=request.model_name,
            organization_id=request.organization_id,
            resource_id=request.resource_id,
        )


# 兼容 ADR-003 中的简写类名
EgressPolicyEngine = DataEgressPolicyEngine
