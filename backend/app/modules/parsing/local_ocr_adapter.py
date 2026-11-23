"""本地扫描 PDF / OCR 预留扩展适配器（LocalOCRParserAdapter）。

安全红线（TECHNICAL_SPEC §6.7 & ADR-003）：
1. 当 `settings.local_ocr_enabled=False` 时，严格执行 Fail-Closed：
   显式抛出 `PolicyBlockedError`（提示 L2/L3 敏感扫描 PDF 禁止外发 MinerU 且本地 OCR 未启用），
   绝不静默降级至外部云解析服务！
2. 当 `settings.local_ocr_enabled=True` 且配置了 `settings.local_ocr_endpoint` 时，
   通过异步 HTTP 调用本地 OCR 服务并将识别结果规范化为统一 `ParsedBlockData`。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import AppError, PolicyBlockedError
from app.modules.parsing.schemas import (
    BlockType,
    CancelResult,
    ParseArtifactBundle,
    ParsedBlockData,
    ParseJobState,
    ParseRequest,
    ParseStatus,
    ParseSubmission,
)
from app.modules.providers.egress import (
    DataEgressPolicyEngine,
    EgressPurpose,
    ProviderScope,
)

logger = logging.getLogger(__name__)

_LOCAL_OCR_DISABLED_MSG = (
    "L2/L3 敏感扫描 PDF 禁止外发 MinerU，且本地 OCR 引擎未启用，任务已安全阻断"
)

_OCR_STATE_MAP: dict[str, tuple[ParseJobState, str]] = {
    "pending": (ParseJobState.SUBMITTED, "pending"),
    "queued": (ParseJobState.SUBMITTED, "pending"),
    "running": (ParseJobState.RUNNING, "running"),
    "done": (ParseJobState.SUCCEEDED, "done"),
    "succeeded": (ParseJobState.SUCCEEDED, "done"),
    "failed": (ParseJobState.FAILED, "failed"),
    "cancelled": (ParseJobState.CANCELLED, "cancelled"),
}


class LocalOCRParserAdapter:
    """本地私有部署 OCR 解析器适配器（实现 `DocumentParser` 协议）。"""

    parser_provider = "local_ocr"
    parser_version = "local-ocr-v1"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        egress_engine: DataEgressPolicyEngine | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._settings = settings or get_settings()
        self._egress = egress_engine or DataEgressPolicyEngine()
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds

    def _ensure_enabled(self) -> str:
        """检查本地 OCR 是否启用且配置完整；缺失时严格 Fail-Closed 阻断。"""
        if not self._settings.local_ocr_enabled:
            raise PolicyBlockedError(
                _LOCAL_OCR_DISABLED_MSG,
                code="LOCAL_OCR_DISABLED",
                detail={
                    "parse_job_status": ParseJobState.BLOCKED_POLICY.value,
                    "local_ocr_enabled": False,
                },
            )
        endpoint = self._settings.local_ocr_endpoint.strip().rstrip("/")
        if not endpoint:
            raise PolicyBlockedError(
                "本地 OCR 引擎已启用但未配置 local_ocr_endpoint，任务已安全阻断",
                code="LOCAL_OCR_ENDPOINT_MISSING",
                detail={
                    "parse_job_status": ParseJobState.BLOCKED_POLICY.value,
                    "local_ocr_enabled": True,
                },
            )
        return endpoint

    def _get_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http_client is not None:
            return self._http_client, False
        return httpx.AsyncClient(timeout=httpx.Timeout(self._timeout_seconds)), True

    async def submit(self, request: ParseRequest) -> ParseSubmission:
        """向本地 OCR 服务提交异步解析任务（若未启用则严格 Fail-Closed）。"""
        endpoint = self._ensure_enabled()

        # 记录本地外发审计（Local 边界允许处理 L0-L3）
        self._egress.enforce(
            sensitivity_level=request.sensitivity_level,
            org_allow_external_l1=request.org_allow_external_l1,
            provider_scope=ProviderScope.LOCAL,
            purpose=EgressPurpose.PARSE,
            provider_name=self.parser_provider,
            model_name=self.parser_version,
            organization_id=request.organization_id,
            resource_id=request.document_version_id,
        )

        if request.existing_external_task_id:
            status_obj = await self.get_status(request.existing_external_task_id)
            return ParseSubmission(
                external_task_id=request.existing_external_task_id,
                parser_provider=self.parser_provider,
                parser_version=self.parser_version,
                status=status_obj.state,
                idempotency_key=request.idempotency_key,
                is_reconciled=True,
                metadata={"provider_state": status_obj.provider_state},
            )

        if request.file_bytes is None:
            raise AppError(
                "本地 OCR 适配器需要提供原始 file_bytes",
                code="LOCAL_OCR_MISSING_BYTES",
                status_code=400,
            )

        payload = {
            "document_version_id": request.document_version_id,
            "filename": request.filename,
            "mime_type": request.mime_type or "application/pdf",
            "idempotency_key": request.idempotency_key,
            "content_base64": base64.b64encode(request.file_bytes).decode("ascii"),
            "options": request.extra_options,
        }

        client, owns_client = self._get_client()
        try:
            resp = await client.post(f"{endpoint}/tasks", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("本地 OCR 提交 HTTP 异常: status=%s", exc.response.status_code)
            raise AppError(
                f"本地 OCR 服务提交失败（HTTP {exc.response.status_code}）",
                code="LOCAL_OCR_HTTP_ERROR",
                status_code=502,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("本地 OCR 网络异常: error=%s", type(exc).__name__)
            raise AppError(
                "本地 OCR 服务网络连接失败",
                code="LOCAL_OCR_NETWORK_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        task_id = str(data.get("task_id") or "")
        if not task_id:
            raise AppError(
                "本地 OCR 响应缺少 task_id",
                code="LOCAL_OCR_INVALID_RESPONSE",
                status_code=502,
            )
        raw_state = str(data.get("state") or "pending").lower()
        state, _ = _OCR_STATE_MAP.get(raw_state, (ParseJobState.SUBMITTED, raw_state))
        return ParseSubmission(
            external_task_id=task_id,
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            status=state,
            idempotency_key=request.idempotency_key,
            is_reconciled=False,
        )

    async def get_status(self, external_task_id: str) -> ParseStatus:
        """查询本地 OCR 异步任务状态。"""
        endpoint = self._ensure_enabled()
        client, owns_client = self._get_client()
        try:
            resp = await client.get(f"{endpoint}/tasks/{external_task_id}")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise AppError(
                "本地 OCR 状态查询失败",
                code="LOCAL_OCR_STATUS_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        raw_state = str(data.get("state") or "unknown").lower()
        state, provider_state = _OCR_STATE_MAP.get(raw_state, (ParseJobState.UNKNOWN, raw_state))
        return ParseStatus(
            external_task_id=external_task_id,
            state=state,
            provider_state=provider_state,
            progress=float(data["progress"]) if data.get("progress") is not None else None,
            error_message=str(data["error_message"]) if data.get("error_message") else None,
        )

    async def fetch_result(self, external_task_id: str) -> ParseArtifactBundle:
        """拉取本地 OCR 结果并规范化为 `ParseArtifactBundle`。"""
        endpoint = self._ensure_enabled()
        client, owns_client = self._get_client()
        try:
            resp = await client.get(f"{endpoint}/tasks/{external_task_id}/result")
            resp.raise_for_status()
            raw_bytes = resp.content
            data: dict[str, Any] = resp.json()
        except httpx.HTTPError as exc:
            raise AppError(
                "本地 OCR 结果拉取失败",
                code="LOCAL_OCR_RESULT_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        blocks: list[ParsedBlockData] = []
        heading_path: list[str] = []
        for idx, raw_block in enumerate(data.get("blocks") or []):
            raw_type = str(
                raw_block.get("block_type") or raw_block.get("type") or "paragraph"
            ).lower()
            try:
                block_type = BlockType(raw_type)
            except ValueError:
                block_type = BlockType.PARAGRAPH

            text_val = str(raw_block.get("text") or "").strip()
            md_val = str(raw_block.get("markdown") or text_val).strip()
            if not text_val and not md_val:
                continue

            if block_type == BlockType.HEADING:
                heading_path = [text_val]
            page_num = raw_block.get("page_number")
            bbox = raw_block.get("bounding_box") or raw_block.get("bbox")
            blocks.append(
                ParsedBlockData(
                    block_type=block_type,
                    page_number=int(page_num) if page_num is not None else None,
                    heading_path=list(raw_block.get("heading_path") or heading_path),
                    text=text_val,
                    markdown=md_val,
                    bounding_box=bbox,
                    metadata={"ocr_index": idx, **(raw_block.get("metadata") or {})},
                )
            )

        markdown = str(data.get("markdown") or "").strip()
        if not markdown:
            markdown = "\n\n".join(b.markdown or b.text for b in blocks)

        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown,
            raw_json=data,
            rendered_pages=[],
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            metadata={"block_count": len(blocks)},
        )

    async def cancel(self, external_task_id: str) -> CancelResult:
        """请求取消本地 OCR 任务。"""
        endpoint = self._ensure_enabled()
        client, owns_client = self._get_client()
        try:
            resp = await client.post(f"{endpoint}/tasks/{external_task_id}/cancel")
            resp.raise_for_status()
            return CancelResult(
                external_task_id=external_task_id,
                cancelled=True,
                state=ParseJobState.CANCELLED,
                message="本地 OCR 任务已取消",
            )
        except httpx.HTTPError:
            return CancelResult(
                external_task_id=external_task_id,
                cancelled=False,
                state=ParseJobState.CANCEL_REQUESTED,
                message="本地 OCR 取消请求发送失败",
            )
        finally:
            if owns_client:
                await client.aclose()
