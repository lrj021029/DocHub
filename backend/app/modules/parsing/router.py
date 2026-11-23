"""文档解析路由器（ParserRouter）——集成敏感度外发门禁与 Fail-Closed 守卫。

路由规则（TECHNICAL_SPEC §6.7 & §7.1 & ADR-003）：
1. 轻量格式（Markdown / DOCX / TXT / XLSX / CSV / JSON）一律路由至本地专用解析器，不外发。
2. PDF 文档（复杂版面 / 扫描 PDF）：
   - 先通过 `DataEgressPolicyEngine` 判定是否允许外发外部 `MinerUOfficialAdapter`：
     * L0：允许外发 MinerU；
     * L1：仅当 `org_allow_external_l1=True` 时允许外发 MinerU；
     * L2：默认仅本地处理，严禁外发 MinerU；
     * L3：绝对禁止外发 MinerU。
   - 若允许外发 MinerU：优先路由 `MinerUOfficialAdapter`（除非显式配置简单纯文本 PDF 优先本机）。
   - 若禁止外发 MinerU（L2/L3 或未授权 L1）：
     * 若未强制 OCR 且提供 `file_bytes`，先使用本机 `PlainPdfFallbackParser` 探查是否为纯文本 PDF；
     * 若为可读纯文本 PDF（`needs_ocr=False`），安全使用本机 `PlainPdfFallbackParser` 解析；
     * 若为扫描件/复杂无法纯文本提取 PDF（`needs_ocr=True` 或 `force_ocr=True`）：
       - 当 `settings.local_ocr_enabled=True` 时路由至 `LocalOCRParserAdapter`；
       - 当 `settings.local_ocr_enabled=False` 时严格 Fail-Closed，
         抛出 `PolicyBlockedError`（标注 `parse_job_status=BLOCKED_POLICY`），绝不降级外发 MinerU！
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.config import Settings, get_settings
from app.core.errors import PolicyBlockedError, ValidationError
from app.modules.parsing.base import DocumentParser, SyncLightweightParser
from app.modules.parsing.lightweight import (
    CsvParser,
    DocxParser,
    ExcelParser,
    JsonParser,
    MarkdownParser,
    PlainPdfFallbackParser,
    TxtParser,
)
from app.modules.parsing.local_ocr_adapter import LocalOCRParserAdapter
from app.modules.parsing.mineru_adapter import MinerUOfficialAdapter
from app.modules.parsing.schemas import (
    ParseArtifactBundle,
    ParseJobState,
    ParseRequest,
    ParseSubmission,
)
from app.modules.providers.egress import (
    DataEgressPolicyEngine,
    EgressDecision,
    EgressPurpose,
    ProviderScope,
)

logger = logging.getLogger(__name__)


class ParserRouteDecision(BaseModel):
    """解析器路由判定结果与外发审计记录。"""

    parser_provider: str = Field(..., description="选中的解析器 Provider 名称")
    provider_scope: ProviderScope = Field(..., description="解析器边界（local/external）")
    reason: str = Field(..., description="中文路由说明")
    scanned_pdf_detected: bool = Field(default=False, description="是否检测到扫描件 PDF")
    egress_decision: EgressDecision = Field(..., description="对应的外发策略审计决策")


class ParserRouter:
    """统一文档解析路由器（带敏感度外发门禁与 Fail-Closed 保护）。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        egress_engine: DataEgressPolicyEngine | None = None,
        mineru_adapter: MinerUOfficialAdapter | None = None,
        local_ocr_adapter: LocalOCRParserAdapter | None = None,
        plain_pdf_parser: PlainPdfFallbackParser | None = None,
        lightweight_parsers: list[SyncLightweightParser] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._egress = egress_engine or DataEgressPolicyEngine()
        self._mineru = mineru_adapter or MinerUOfficialAdapter(
            settings=self._settings,
            egress_engine=self._egress,
        )
        self._local_ocr = local_ocr_adapter or LocalOCRParserAdapter(
            settings=self._settings,
            egress_engine=self._egress,
        )
        self._plain_pdf = plain_pdf_parser or PlainPdfFallbackParser()

        parsers = lightweight_parsers or [
            MarkdownParser(),
            DocxParser(),
            TxtParser(),
            ExcelParser(),
            CsvParser(),
            JsonParser(),
        ]
        self._ext_map: dict[str, SyncLightweightParser] = {}
        self._mime_map: dict[str, SyncLightweightParser] = {}
        for parser in parsers:
            for ext in parser.supported_extensions:
                self._ext_map[ext.lower()] = parser
            for mime in parser.supported_mime_types:
                self._mime_map[mime.lower()] = parser

    @staticmethod
    def _is_pdf(request: ParseRequest) -> bool:
        ext = Path(request.filename).suffix.lower()
        mime = (request.mime_type or "").split(";")[0].strip().lower()
        return ext == ".pdf" or mime == "application/pdf"

    def _find_lightweight_parser(self, request: ParseRequest) -> SyncLightweightParser | None:
        ext = Path(request.filename).suffix.lower()
        if ext in self._ext_map:
            return self._ext_map[ext]
        mime = (request.mime_type or "").split(";")[0].strip().lower()
        if mime in self._mime_map:
            return self._mime_map[mime]
        return None

    def select_parser(
        self,
        request: ParseRequest,
        *,
        prefer_local_plain_pdf: bool = False,
    ) -> tuple[DocumentParser, ParserRouteDecision]:
        """根据文件格式、扫描件检测结果与敏感度外发策略选定解析器。

        若 L2/L3（或未获授权 L1）扫描 PDF 无法本机纯文本提取且 `local_ocr_enabled=False`，
        强制抛出 `PolicyBlockedError`（状态 `BLOCKED_POLICY`），绝不触发外部 MinerU。
        """
        # 1. 非 PDF 格式优先命中本机轻量解析器（全部本地处理）
        if not self._is_pdf(request):
            lightweight = self._find_lightweight_parser(request)
            if lightweight is None:
                mime_label = request.mime_type or "unknown"
                raise ValidationError(
                    f"暂不支持的文件格式：{request.filename}（mime={mime_label}）"
                )
            decision = self._egress.enforce(
                sensitivity_level=request.sensitivity_level,
                org_allow_external_l1=request.org_allow_external_l1,
                provider_scope=ProviderScope.LOCAL,
                purpose=EgressPurpose.PARSE,
                provider_name=lightweight.parser_provider,
                model_name=lightweight.parser_version,
                organization_id=request.organization_id,
                resource_id=request.document_version_id,
            )
            return lightweight, ParserRouteDecision(
                parser_provider=lightweight.parser_provider,
                provider_scope=ProviderScope.LOCAL,
                reason=f"轻量文档格式命中本地专用解析器 {lightweight.parser_provider}",
                scanned_pdf_detected=False,
                egress_decision=decision,
            )

        # 2. PDF 格式：先评估是否允许外发 MinerU 官方云 API
        mineru_decision = self._egress.evaluate(
            sensitivity_level=request.sensitivity_level,
            org_allow_external_l1=request.org_allow_external_l1,
            provider_scope=ProviderScope.EXTERNAL,
            purpose=EgressPurpose.PARSE,
            provider_name=self._mineru.parser_provider,
            model_name=self._mineru.parser_version,
            organization_id=request.organization_id,
            resource_id=request.document_version_id,
        )

        # 2A. 若策略允许外发 MinerU（L0 或组织显式授权的 L1）
        if mineru_decision.allowed:
            if prefer_local_plain_pdf and not request.force_ocr and request.file_bytes is not None:
                probe = self._plain_pdf.inspect_pdf_bytes(request.file_bytes)
                if not probe["needs_ocr"]:
                    local_decision = self._egress.enforce(
                        sensitivity_level=request.sensitivity_level,
                        org_allow_external_l1=request.org_allow_external_l1,
                        provider_scope=ProviderScope.LOCAL,
                        purpose=EgressPurpose.PARSE,
                        provider_name=self._plain_pdf.parser_provider,
                        model_name=self._plain_pdf.parser_version,
                        organization_id=request.organization_id,
                        resource_id=request.document_version_id,
                    )
                    return self._plain_pdf, ParserRouteDecision(
                        parser_provider=self._plain_pdf.parser_provider,
                        provider_scope=ProviderScope.LOCAL,
                        reason="简单纯文本 PDF 命中本机快速解析回退路径",
                        scanned_pdf_detected=False,
                        egress_decision=local_decision,
                    )

            return self._mineru, ParserRouteDecision(
                parser_provider=self._mineru.parser_provider,
                provider_scope=ProviderScope.EXTERNAL,
                reason="PDF 文档符合外发策略，优先路由至 MinerU 官方 API 解析器",
                scanned_pdf_detected=request.force_ocr,
                egress_decision=mineru_decision,
            )

        # 2B. 敏感策略禁止外发 MinerU（L2/L3 或未授权 L1）：严禁任何路径流向 MinerU
        scanned_detected = bool(request.force_ocr)
        if not scanned_detected and request.file_bytes is not None:
            probe = self._plain_pdf.inspect_pdf_bytes(request.file_bytes)
            scanned_detected = bool(probe["needs_ocr"])
            if not scanned_detected:
                local_decision = self._egress.enforce(
                    sensitivity_level=request.sensitivity_level,
                    org_allow_external_l1=request.org_allow_external_l1,
                    provider_scope=ProviderScope.LOCAL,
                    purpose=EgressPurpose.PARSE,
                    provider_name=self._plain_pdf.parser_provider,
                    model_name=self._plain_pdf.parser_version,
                    organization_id=request.organization_id,
                    resource_id=request.document_version_id,
                )
                return self._plain_pdf, ParserRouteDecision(
                    parser_provider=self._plain_pdf.parser_provider,
                    provider_scope=ProviderScope.LOCAL,
                    reason=(
                        f"{request.sensitivity_level.value} 敏感纯文本 PDF 禁止外发，"
                        "已安全路由至本机纯文本 PDF 解析器"
                    ),
                    scanned_pdf_detected=False,
                    egress_decision=local_decision,
                )
        else:
            # 无法本机提取纯文本（如仅提供远端引用或强制 OCR），视作必须走本地 OCR 路径
            scanned_detected = True

        # 需要本地 OCR：检查本地 OCR 引擎开关，未启用则严格 Fail-Closed -> BLOCKED_POLICY
        if not self._settings.local_ocr_enabled or not self._settings.local_ocr_endpoint.strip():
            logger.warning(
                "敏感 PDF 解析策略阻断（BLOCKED_POLICY）: level=%s doc_ver=%s local_ocr=%s",
                request.sensitivity_level.value,
                request.document_version_id,
                self._settings.local_ocr_enabled,
            )
            raise PolicyBlockedError(
                f"{request.sensitivity_level.value} 敏感扫描 PDF 禁止外发 MinerU，"
                "且本地 OCR 引擎未启用，任务已安全阻断（BLOCKED_POLICY）",
                code="PARSE_BLOCKED_POLICY",
                detail={
                    "parse_job_status": ParseJobState.BLOCKED_POLICY.value,
                    "sensitivity_level": request.sensitivity_level.value,
                    "scanned_pdf_detected": scanned_detected,
                    "local_ocr_enabled": self._settings.local_ocr_enabled,
                    "egress_decision": mineru_decision.model_dump(mode="json"),
                },
            )

        local_ocr_decision = self._egress.enforce(
            sensitivity_level=request.sensitivity_level,
            org_allow_external_l1=request.org_allow_external_l1,
            provider_scope=ProviderScope.LOCAL,
            purpose=EgressPurpose.PARSE,
            provider_name=self._local_ocr.parser_provider,
            model_name=self._local_ocr.parser_version,
            organization_id=request.organization_id,
            resource_id=request.document_version_id,
        )
        return self._local_ocr, ParserRouteDecision(
            parser_provider=self._local_ocr.parser_provider,
            provider_scope=ProviderScope.LOCAL,
            reason=f"{request.sensitivity_level.value} 敏感扫描 PDF 已安全路由至本地 OCR 引擎",
            scanned_pdf_detected=True,
            egress_decision=local_ocr_decision,
        )

    async def submit(
        self,
        request: ParseRequest,
        *,
        prefer_local_plain_pdf: bool = False,
    ) -> tuple[ParseSubmission, ParserRouteDecision]:
        """选择合规解析器并提交解析任务，返回提交回执与路由审计决策。"""
        parser, route_decision = self.select_parser(
            request,
            prefer_local_plain_pdf=prefer_local_plain_pdf,
        )
        submission = await parser.submit(request)
        return submission, route_decision

    async def parse_sync_or_raise(
        self,
        request: ParseRequest,
        *,
        prefer_local_plain_pdf: bool = False,
    ) -> tuple[ParseArtifactBundle, ParserRouteDecision]:
        """便捷方法：若命中本地同步解析器则直接返回完整解析产物；异步解析器需改用 submit+轮询。"""
        parser, route_decision = self.select_parser(
            request,
            prefer_local_plain_pdf=prefer_local_plain_pdf,
        )
        if not isinstance(parser, SyncLightweightParser):
            raise ValidationError(
                f"解析器 {parser.parser_provider} 为异步引擎，"
                "请使用 submit() + get_status() + fetch_result()"
            )
        bundle = parser.parse(request)
        return bundle, route_decision
