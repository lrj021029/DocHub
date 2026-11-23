"""MinerU 官方云 API 异步文档解析适配器（MinerUOfficialAdapter）。

核心设计遵循 TECHNICAL_SPEC §7.2 - §7.4 与 ADR-003：
1. 从 `Settings` 读取 `mineru_api_base_url`、`mineru_api_version` 与 `mineru_api_key`
   （`SecretStr`，绝不打入日志）。
2. 提交前强制执行 `DataEgressPolicyEngine`
   （即便绕过 Router 直接调用，L2/L3 与未授权 L1 亦绝不外发）。
3. 完整支持官方 v4 异步协议：
   - URL 提取模式：POST `/extract/task`；
   - 本地文件字节批量上传模式：POST `/file-urls/batch` 申请预签名上传链接 -> PUT 上传字节；
   - 已有 `external_task_id` 或同 `idempotency_key` 对账恢复（查询现有任务，不重复提交）；
   - 状态轮询：GET `/extract/task/{task_id}` 或 `/extract-results/batch/{batch_id}`，清晰映射
     `pending`/`running`/`done`/`failed`/`cancelled`；
   - 结果下载与校验：若预签名下载链接过期则按官方语义重查最新链接，
     下载结果 ZIP 并计算/校验 SHA-256；
   - 安全解压 ZIP（拦截 Zip-Slip），提取 `content_list.json` / `middle.json` / `.md` / 渲染图，
     并规范化为 `ParsedBlockData`（保留页码、Bounding Box、表格、LaTeX 公式、图片图注与标题路径）；
   - 任务取消接口 `cancel()`。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import posixpath
import zipfile
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.modules.parsing.schemas import (
    BlockType,
    CancelResult,
    ParseArtifactBundle,
    ParsedBlockData,
    ParseJobState,
    ParseRequest,
    ParseStatus,
    ParseSubmission,
    RenderedPageArtifact,
)
from app.modules.providers.egress import (
    DataEgressPolicyEngine,
    EgressPurpose,
    ProviderScope,
)

logger = logging.getLogger(__name__)

# MinerU 官方状态字面量 -> 统一内部状态机映射
_MINERU_STATE_MAP: dict[str, tuple[ParseJobState, str]] = {
    "pending": (ParseJobState.SUBMITTED, "pending"),
    "waiting-file": (ParseJobState.SUBMITTED, "pending"),
    "queued": (ParseJobState.SUBMITTED, "pending"),
    "running": (ParseJobState.RUNNING, "running"),
    "converting": (ParseJobState.RUNNING, "running"),
    "processing": (ParseJobState.RUNNING, "running"),
    "done": (ParseJobState.SUCCEEDED, "done"),
    "completed": (ParseJobState.SUCCEEDED, "done"),
    "succeeded": (ParseJobState.SUCCEEDED, "done"),
    "failed": (ParseJobState.FAILED, "failed"),
    "error": (ParseJobState.FAILED, "failed"),
    "cancelled": (ParseJobState.CANCELLED, "cancelled"),
    "canceled": (ParseJobState.CANCELLED, "cancelled"),
}

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


class MinerUOfficialAdapter:
    """MinerU 官方 API 异步解析适配器（实现 `DocumentParser` 协议）。"""

    parser_provider = "mineru_official"

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        egress_engine: DataEgressPolicyEngine | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._egress = egress_engine or DataEgressPolicyEngine()
        self._http_client = http_client
        # 幂等键 -> 外部任务 ID 的进程内对账映射
        # 生产环境由 ParseJob 持久化表提供 existing_external_task_id
        self._idempotency_registry: dict[str, str] = {}
        # 缓存批量任务关联的 data_id，便于从 batch 数组中定位对应条目
        self._batch_data_id_map: dict[str, str] = {}

    @property
    def parser_version(self) -> str:
        """返回 MinerU API 版本标识。"""
        return f"mineru-official-{self._settings.mineru_api_version}"

    @property
    def base_url(self) -> str:
        """返回去除尾部斜杠的 MinerU 官方 API 基址。"""
        return self._settings.mineru_api_base_url.rstrip("/")

    def _build_api_headers(self) -> dict[str, str]:
        """构造官方 API 请求头；SecretStr 仅在此处解包且严禁写入日志。"""
        api_key = self._settings.mineru_api_key.get_secret_value().strip()
        if not api_key:
            raise AppError(
                "未配置 MinerU 官方 API Key（MINERU_API_KEY），无法调用外部解析服务",
                code="MINERU_API_KEY_MISSING",
                status_code=503,
            )
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get_client(self) -> tuple[httpx.AsyncClient, bool]:
        if self._http_client is not None:
            return self._http_client, False
        return (
            httpx.AsyncClient(timeout=httpx.Timeout(self._settings.mineru_timeout_seconds)),
            True,
        )

    @staticmethod
    def _unwrap_mineru_payload(body: dict[str, Any]) -> dict[str, Any]:
        """校验并解包 MinerU 统一响应封装 `{code: 0, msg: 'ok', data: {...}}`。"""
        code = body.get("code")
        if code is not None and code != 0:
            msg = str(body.get("msg") or body.get("message") or "MinerU API 返回业务错误")
            raise AppError(
                f"MinerU 官方接口调用失败（code={code}）：{msg}",
                code="MINERU_API_ERROR",
                status_code=502,
                detail={"mineru_code": code},
            )
        data = body.get("data")
        if isinstance(data, dict):
            return data
        return body

    async def submit(self, request: ParseRequest) -> ParseSubmission:
        """提交 MinerU 解析任务；若存在已有任务 ID 或同幂等键记录，则先对账恢复而不重复提交。"""
        # 1. 提交外部云服务前强制执行外发门禁策略（L2/L3 严禁外发）
        self._egress.enforce(
            sensitivity_level=request.sensitivity_level,
            org_allow_external_l1=request.org_allow_external_l1,
            provider_scope=ProviderScope.EXTERNAL,
            purpose=EgressPurpose.PARSE,
            provider_name=self.parser_provider,
            model_name=self.parser_version,
            organization_id=request.organization_id,
            resource_id=request.document_version_id,
        )

        # 2. 对账恢复：优先使用请求携带的 existing_external_task_id 或已登记幂等键
        existing_task_id = request.existing_external_task_id or self._idempotency_registry.get(
            request.idempotency_key
        )
        if existing_task_id:
            status_obj = await self.get_status(existing_task_id)
            return ParseSubmission(
                external_task_id=existing_task_id,
                parser_provider=self.parser_provider,
                parser_version=self.parser_version,
                status=status_obj.state,
                idempotency_key=request.idempotency_key,
                is_reconciled=True,
                metadata={
                    "provider_state": status_obj.provider_state,
                    "reconciled_from": "existing_external_task_id",
                },
            )

        enable_formula = bool(request.extra_options.get("enable_formula", True))
        enable_table = bool(request.extra_options.get("enable_table", True))
        is_ocr = bool(request.force_ocr or request.extra_options.get("is_ocr", False))
        model_version = str(request.extra_options.get("model_version", "vlm"))

        client, owns_client = self._get_client()
        try:
            # 3A. 远程可访问 URL 提取模式：POST /extract/task
            if (
                request.source_ref
                and request.source_ref.startswith(("http://", "https://"))
                and request.file_bytes is None
            ):
                payload = {
                    "url": request.source_ref,
                    "is_ocr": is_ocr,
                    "enable_formula": enable_formula,
                    "enable_table": enable_table,
                    "data_id": request.idempotency_key,
                    "model_version": model_version,
                }
                resp = await client.post(
                    f"{self.base_url}/extract/task",
                    json=payload,
                    headers=self._build_api_headers(),
                )
                resp.raise_for_status()
                data = self._unwrap_mineru_payload(resp.json())
                task_id = str(data.get("task_id") or "")
                if not task_id:
                    raise AppError(
                        "MinerU 任务创建响应缺少 task_id",
                        code="MINERU_INVALID_RESPONSE",
                        status_code=502,
                    )
                external_task_id = f"task:{task_id}"
                self._idempotency_registry[request.idempotency_key] = external_task_id
                return ParseSubmission(
                    external_task_id=external_task_id,
                    parser_provider=self.parser_provider,
                    parser_version=self.parser_version,
                    status=ParseJobState.SUBMITTED,
                    idempotency_key=request.idempotency_key,
                    is_reconciled=False,
                    metadata={"mode": "url_extract"},
                )

            # 3B. 本地文件字节模式：POST /file-urls/batch 申请预签名上传链接并 PUT 上传
            if request.file_bytes is None:
                raise AppError(
                    "MinerU 本地上传模式必须提供 file_bytes 或可访问的 HTTP(S) source_ref",
                    code="MINERU_MISSING_SOURCE",
                    status_code=400,
                )

            batch_req_payload = {
                "enable_formula": enable_formula,
                "enable_table": enable_table,
                "model_version": model_version,
                "files": [
                    {
                        "name": request.filename,
                        "is_ocr": is_ocr,
                        "data_id": request.idempotency_key,
                    }
                ],
            }
            batch_resp = await client.post(
                f"{self.base_url}/file-urls/batch",
                json=batch_req_payload,
                headers=self._build_api_headers(),
            )
            batch_resp.raise_for_status()
            batch_data = self._unwrap_mineru_payload(batch_resp.json())
            batch_id = str(batch_data.get("batch_id") or "")
            file_urls = batch_data.get("file_urls") or []
            if not batch_id or not file_urls:
                raise AppError(
                    "MinerU 预签名上传响应缺少 batch_id 或 file_urls",
                    code="MINERU_INVALID_UPLOAD_URL",
                    status_code=502,
                )

            upload_url = str(file_urls[0])
            # 上传至对象存储预签名链接时不附带 MinerU Bearer Token，避免泄露凭证给第三方存储域
            upload_resp = await client.put(
                upload_url,
                content=request.file_bytes,
                headers={"Content-Type": request.mime_type or "application/octet-stream"},
            )
            upload_resp.raise_for_status()

            external_task_id = f"batch:{batch_id}"
            self._idempotency_registry[request.idempotency_key] = external_task_id
            self._batch_data_id_map[batch_id] = request.idempotency_key
            return ParseSubmission(
                external_task_id=external_task_id,
                parser_provider=self.parser_provider,
                parser_version=self.parser_version,
                status=ParseJobState.SUBMITTED,
                idempotency_key=request.idempotency_key,
                is_reconciled=False,
                metadata={"mode": "batch_presigned_upload", "batch_id": batch_id},
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("MinerU 提交 HTTP 异常: status=%s", exc.response.status_code)
            raise AppError(
                f"MinerU 解析任务提交失败（HTTP {exc.response.status_code}）",
                code="MINERU_HTTP_ERROR",
                status_code=502,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("MinerU 提交网络异常: error=%s", type(exc).__name__)
            raise AppError(
                "MinerU 解析服务网络连接异常，请稍后对账重试",
                code="MINERU_NETWORK_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

    @staticmethod
    def _parse_task_ref(external_task_id: str) -> tuple[str, str]:
        """拆解内部约定的 `batch:<id>` 或 `task:<id>` 任务前缀。"""
        if external_task_id.startswith("batch:"):
            return "batch", external_task_id[len("batch:") :]
        if external_task_id.startswith("task:"):
            return "task", external_task_id[len("task:") :]
        return "task", external_task_id

    async def get_status(self, external_task_id: str) -> ParseStatus:
        """查询 MinerU 异步解析任务状态并归一化为 `ParseStatus`。"""
        mode, raw_id = self._parse_task_ref(external_task_id)
        endpoint = (
            f"{self.base_url}/extract-results/batch/{raw_id}"
            if mode == "batch"
            else f"{self.base_url}/extract/task/{raw_id}"
        )

        client, owns_client = self._get_client()
        try:
            resp = await client.get(endpoint, headers=self._build_api_headers())
            resp.raise_for_status()
            data = self._unwrap_mineru_payload(resp.json())
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "MinerU 状态查询 HTTP 异常: task=%s status=%s",
                external_task_id,
                exc.response.status_code,
            )
            raise AppError(
                f"MinerU 状态查询失败（HTTP {exc.response.status_code}）",
                code="MINERU_STATUS_HTTP_ERROR",
                status_code=502,
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("MinerU 状态查询网络异常: error=%s", type(exc).__name__)
            raise AppError(
                "MinerU 状态查询网络连接失败",
                code="MINERU_NETWORK_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        # 批量接口返回 extract_result 数组，优先按登记的 data_id 匹配
        record: dict[str, Any] = data
        if mode == "batch":
            extract_list = data.get("extract_result") or []
            if not extract_list:
                return ParseStatus(
                    external_task_id=external_task_id,
                    state=ParseJobState.SUBMITTED,
                    provider_state="pending",
                    progress=0.0,
                )
            target_data_id = self._batch_data_id_map.get(raw_id)
            matched = next(
                (item for item in extract_list if item.get("data_id") == target_data_id),
                extract_list[0],
            )
            record = matched

        raw_state = str(record.get("state") or record.get("status") or "unknown").strip().lower()
        internal_state, provider_state = _MINERU_STATE_MAP.get(
            raw_state,
            (ParseJobState.UNKNOWN, raw_state),
        )

        # 解析可选提取进度（如 extract_progress: {extracted_pages, total_pages}）
        progress: float | None = None
        progress_obj = record.get("extract_progress")
        if isinstance(progress_obj, dict):
            total_pages = int(progress_obj.get("total_pages") or 0)
            extracted_pages = int(progress_obj.get("extracted_pages") or 0)
            if total_pages > 0:
                progress = round(min(100.0, (extracted_pages / total_pages) * 100.0), 2)
        elif internal_state == ParseJobState.SUCCEEDED:
            progress = 100.0
        elif internal_state == ParseJobState.SUBMITTED:
            progress = 0.0

        result_url = record.get("full_zip_url") or record.get("result_url")
        err_msg = record.get("err_msg") or record.get("error_message")
        expected_sha256 = record.get("zip_sha256") or record.get("sha256")

        meta: dict[str, Any] = {}
        if expected_sha256:
            meta["expected_sha256"] = str(expected_sha256)
        if record.get("file_name"):
            meta["file_name"] = str(record["file_name"])

        return ParseStatus(
            external_task_id=external_task_id,
            state=internal_state,
            provider_state=provider_state,
            progress=progress,
            result_url=str(result_url) if result_url else None,
            error_code="MINERU_TASK_FAILED" if internal_state == ParseJobState.FAILED else None,
            error_message=str(err_msg) if err_msg else None,
            metadata=meta,
        )

    async def wait_for_completion(
        self,
        external_task_id: str,
        *,
        poll_interval_seconds: float | None = None,
        max_attempts: int | None = None,
    ) -> ParseStatus:
        """按配置轮询任务直至进入终态（SUCCEEDED / FAILED / CANCELLED）。"""
        interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else float(self._settings.mineru_poll_interval_seconds)
        )
        attempts = (
            max_attempts if max_attempts is not None else self._settings.mineru_max_poll_attempts
        )
        terminal_states = {
            ParseJobState.SUCCEEDED,
            ParseJobState.FAILED,
            ParseJobState.CANCELLED,
        }

        last_status: ParseStatus | None = None
        for idx in range(max(1, attempts)):
            last_status = await self.get_status(external_task_id)
            if last_status.state in terminal_states:
                return last_status
            if idx + 1 < attempts and interval > 0:
                await asyncio.sleep(interval)

        raise AppError(
            f"MinerU 解析任务轮询超时（task={external_task_id}）",
            code="MINERU_POLL_TIMEOUT",
            status_code=504,
            detail={
                "external_task_id": external_task_id,
                "last_state": last_status.state.value if last_status else "UNKNOWN",
            },
        )

    async def _download_result_zip(
        self,
        external_task_id: str,
        status_obj: ParseStatus,
    ) -> tuple[bytes, str | None]:
        """下载结果 ZIP；若遇到 403/410 链接过期则按官方语义重新查询一次最新链接。"""
        current_status = status_obj
        client, owns_client = self._get_client()
        try:
            for attempt in range(2):
                if not current_status.result_url:
                    raise AppError(
                        "MinerU 已完成任务缺少可下载的 full_zip_url",
                        code="MINERU_MISSING_RESULT_URL",
                        status_code=502,
                    )
                resp = await client.get(current_status.result_url)
                if resp.status_code in {403, 410} and attempt == 0:
                    logger.info(
                        "MinerU 下载链接可能已过期，重新查询任务刷新链接: task=%s",
                        external_task_id,
                    )
                    current_status = await self.get_status(external_task_id)
                    continue
                resp.raise_for_status()
                expected_sha = resp.headers.get("x-checksum-sha256") or current_status.metadata.get(
                    "expected_sha256"
                )
                return resp.content, (str(expected_sha) if expected_sha else None)
        except httpx.HTTPError as exc:
            logger.warning("MinerU 结果 ZIP 下载异常: error=%s", type(exc).__name__)
            raise AppError(
                "MinerU 解析结果压缩包下载失败",
                code="MINERU_DOWNLOAD_ERROR",
                status_code=502,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

        raise AppError(
            "MinerU 解析结果压缩包下载失败", code="MINERU_DOWNLOAD_ERROR", status_code=502
        )

    async def fetch_result(self, external_task_id: str) -> ParseArtifactBundle:
        """下载 MinerU 解析结果 ZIP、校验 SHA-256 并规范化为结构化 `ParseArtifactBundle`。"""
        status_obj = await self.get_status(external_task_id)
        if status_obj.state != ParseJobState.SUCCEEDED:
            raise AppError(
                f"MinerU 任务尚未成功完成（当前状态：{status_obj.state.value}），无法拉取结果",
                code="MINERU_TASK_NOT_READY",
                status_code=409,
                detail={"state": status_obj.state.value},
            )

        zip_bytes, expected_sha256 = await self._download_result_zip(external_task_id, status_obj)
        actual_sha256 = hashlib.sha256(zip_bytes).hexdigest()
        if expected_sha256 and actual_sha256.lower() != expected_sha256.strip().lower():
            raise AppError(
                "MinerU 解析结果压缩包 SHA-256 校验失败，已拒绝受损产物",
                code="MINERU_CHECKSUM_MISMATCH",
                status_code=502,
            )

        return self.parse_result_zip(zip_bytes, sha256=actual_sha256)

    def parse_result_zip(
        self,
        zip_bytes: bytes,
        *,
        sha256: str | None = None,
    ) -> ParseArtifactBundle:
        """安全解压并解析 MinerU 返回的 ZIP 压缩包（支持独立单测复用）。"""
        computed_sha256 = sha256 or hashlib.sha256(zip_bytes).hexdigest()
        content_list_data: list[dict[str, Any]] | None = None
        middle_json_data: dict[str, Any] | None = None
        markdown_text = ""
        rendered_pages: list[RenderedPageArtifact] = []

        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # 防止 Zip-Slip 路径穿越攻击
                    normalized_name = posixpath.normpath(info.filename.replace("\\", "/"))
                    if normalized_name.startswith("../") or normalized_name.startswith("/"):
                        raise AppError(
                            "解析产物压缩包包含非法路径穿越条目，已安全拦截",
                            code="MINERU_UNSAFE_ARCHIVE",
                            status_code=400,
                        )
                    lower_name = normalized_name.lower()
                    raw_entry = zf.read(info)

                    if lower_name.endswith("content_list.json"):
                        parsed = json.loads(raw_entry.decode("utf-8"))
                        if isinstance(parsed, list):
                            content_list_data = parsed
                    elif lower_name.endswith("middle.json"):
                        parsed = json.loads(raw_entry.decode("utf-8"))
                        if isinstance(parsed, dict):
                            middle_json_data = parsed
                    elif lower_name.endswith(".md") and not markdown_text:
                        markdown_text = raw_entry.decode("utf-8", errors="replace")
                    else:
                        ext = posixpath.splitext(lower_name)[1]
                        if ext in _IMAGE_EXTENSIONS:
                            guessed_mime = mimetypes.guess_type(normalized_name)[0] or "image/png"
                            page_num = self._infer_page_number_from_filename(normalized_name)
                            rendered_pages.append(
                                RenderedPageArtifact(
                                    page_number=page_num,
                                    filename=normalized_name,
                                    mime_type=guessed_mime,
                                    sha256=hashlib.sha256(raw_entry).hexdigest(),
                                    size_bytes=len(raw_entry),
                                    content_bytes=raw_entry,
                                )
                            )
        except zipfile.BadZipFile as exc:
            raise AppError(
                "MinerU 返回的解析结果不是合法 ZIP 文件",
                code="MINERU_INVALID_ZIP",
                status_code=502,
            ) from exc

        blocks = self._normalize_blocks(
            content_list=content_list_data,
            middle_json=middle_json_data,
            markdown_text=markdown_text,
        )
        if not markdown_text and blocks:
            markdown_text = "\n\n".join(
                b.markdown or b.text for b in blocks if b.markdown or b.text
            )

        raw_json: dict[str, Any] = {}
        if content_list_data is not None:
            raw_json["content_list"] = content_list_data
        if middle_json_data is not None:
            raw_json["middle"] = middle_json_data

        return ParseArtifactBundle(
            blocks=blocks,
            markdown=markdown_text,
            raw_json=raw_json,
            rendered_pages=rendered_pages,
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            sha256=computed_sha256,
            metadata={
                "block_count": len(blocks),
                "rendered_page_count": len(rendered_pages),
                "has_content_list": content_list_data is not None,
                "has_middle_json": middle_json_data is not None,
            },
        )

    @staticmethod
    def _infer_page_number_from_filename(filename: str) -> int | None:
        """尝试从页面渲染图文件名（如 `pages/page_1.png`）推断 1-based 页码。"""
        base = posixpath.basename(filename)
        digits = "".join(ch if ch.isdigit() else " " for ch in base).split()
        if len(digits) == 1:
            num = int(digits[0])
            return num if num >= 1 else 1
        return None

    @staticmethod
    def _coerce_bbox(raw_bbox: Any) -> list[float] | None:
        """将 MinerU 的 4 元素坐标数组规整为 float 列表。"""
        if isinstance(raw_bbox, (list, tuple)) and len(raw_bbox) == 4:
            try:
                return [float(coord) for coord in raw_bbox]
            except (TypeError, ValueError):
                return None
        return None

    def _normalize_blocks(
        self,
        *,
        content_list: list[dict[str, Any]] | None,
        middle_json: dict[str, Any] | None,
        markdown_text: str,
    ) -> list[ParsedBlockData]:
        """将 MinerU `content_list.json` / `middle.json` 规范化为统一 `ParsedBlockData`。"""
        if content_list:
            return self._normalize_from_content_list(content_list)
        if middle_json:
            return self._normalize_from_middle_json(middle_json)
        if markdown_text.strip():
            return [
                ParsedBlockData(
                    block_type=BlockType.PARAGRAPH,
                    page_number=1,
                    heading_path=[],
                    text=markdown_text.strip(),
                    markdown=markdown_text.strip(),
                    metadata={"source": "markdown_fallback"},
                )
            ]
        return []

    def _normalize_from_content_list(
        self, content_list: list[dict[str, Any]]
    ) -> list[ParsedBlockData]:
        blocks: list[ParsedBlockData] = []
        heading_stack: list[tuple[int, str]] = []

        for idx, item in enumerate(content_list):
            raw_type = str(item.get("type") or "text").strip().lower()
            page_idx = item.get("page_idx")
            page_number = int(page_idx) + 1 if page_idx is not None else None
            bbox = self._coerce_bbox(item.get("bbox"))
            current_headings = [title for _, title in heading_stack]

            if raw_type == "text":
                text_val = str(item.get("text") or "").strip()
                if not text_val:
                    continue
                text_level = item.get("text_level")
                if text_level is not None and int(text_level) >= 1:
                    level = min(max(int(text_level), 1), 6)
                    while heading_stack and heading_stack[-1][0] >= level:
                        heading_stack.pop()
                    heading_stack.append((level, text_val))
                    path = [title for _, title in heading_stack]
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.HEADING,
                            page_number=page_number,
                            heading_path=path,
                            text=text_val,
                            markdown=f"{'#' * level} {text_val}",
                            bounding_box=bbox,
                            metadata={"level": level, "mineru_index": idx},
                        )
                    )
                else:
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.PARAGRAPH,
                            page_number=page_number,
                            heading_path=current_headings,
                            text=text_val,
                            markdown=text_val,
                            bounding_box=bbox,
                            metadata={"mineru_index": idx},
                        )
                    )
            elif raw_type == "table":
                table_body = str(item.get("table_body") or item.get("text") or "").strip()
                captions = [
                    str(c).strip() for c in (item.get("table_caption") or []) if str(c).strip()
                ]
                footnotes = [
                    str(f).strip() for f in (item.get("table_footnote") or []) if str(f).strip()
                ]
                caption_text = " ".join(captions)
                md_parts = [p for p in [caption_text, table_body, " ".join(footnotes)] if p]
                combined_md = "\n".join(md_parts)
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.TABLE,
                        page_number=page_number,
                        heading_path=current_headings,
                        text=combined_md,
                        markdown=combined_md,
                        bounding_box=bbox,
                        metadata={
                            "table_caption": captions,
                            "table_footnote": footnotes,
                            "img_path": item.get("img_path"),
                            "mineru_index": idx,
                        },
                    )
                )
            elif raw_type in {"equation", "interline_equation", "formula"}:
                latex = str(item.get("text") or item.get("latex") or "").strip()
                if not latex:
                    continue
                md_formula = latex if latex.startswith("$$") else f"$$\n{latex}\n$$"
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.FORMULA,
                        page_number=page_number,
                        heading_path=current_headings,
                        text=latex,
                        markdown=md_formula,
                        bounding_box=bbox,
                        metadata={
                            "text_format": item.get("text_format", "latex"),
                            "mineru_index": idx,
                        },
                    )
                )
            elif raw_type == "image":
                captions = [
                    str(c).strip() for c in (item.get("img_caption") or []) if str(c).strip()
                ]
                footnotes = [
                    str(f).strip() for f in (item.get("img_footnote") or []) if str(f).strip()
                ]
                ocr_text = str(item.get("text") or "").strip()
                img_path = str(item.get("img_path") or "")
                caption_str = " ".join(captions) or "文档插图"
                desc_parts = [p for p in [caption_str, ocr_text, " ".join(footnotes)] if p]
                desc_text = "\n".join(desc_parts)
                md_img = f"![{caption_str}]({img_path})" if img_path else f"[图片] {desc_text}"
                if ocr_text:
                    md_img = f"{md_img}\n{ocr_text}"
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.IMAGE,
                        page_number=page_number,
                        heading_path=current_headings,
                        text=desc_text,
                        markdown=md_img,
                        bounding_box=bbox,
                        metadata={
                            "img_path": img_path,
                            "img_caption": captions,
                            "img_footnote": footnotes,
                            "mineru_index": idx,
                        },
                    )
                )
            elif raw_type == "code":
                code_text = str(item.get("text") or "").strip()
                lang = str(item.get("lang") or "")
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.CODE,
                        page_number=page_number,
                        heading_path=current_headings,
                        text=code_text,
                        markdown=f"```{lang}\n{code_text}\n```",
                        bounding_box=bbox,
                        metadata={"language": lang, "mineru_index": idx},
                    )
                )
            elif raw_type == "list":
                list_text = str(item.get("text") or "").strip()
                blocks.append(
                    ParsedBlockData(
                        block_type=BlockType.LIST,
                        page_number=page_number,
                        heading_path=current_headings,
                        text=list_text,
                        markdown=list_text,
                        bounding_box=bbox,
                        metadata={"mineru_index": idx},
                    )
                )
            else:
                fallback_text = str(item.get("text") or "").strip()
                if fallback_text:
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.PARAGRAPH,
                            page_number=page_number,
                            heading_path=current_headings,
                            text=fallback_text,
                            markdown=fallback_text,
                            bounding_box=bbox,
                            metadata={"raw_type": raw_type, "mineru_index": idx},
                        )
                    )
        return blocks

    def _normalize_from_middle_json(self, middle_json: dict[str, Any]) -> list[ParsedBlockData]:
        """当压缩包仅提供 `middle.json` 时，从 `pdf_info[].para_blocks` 提取结构块。"""
        blocks: list[ParsedBlockData] = []
        heading_path: list[str] = []
        for page_info in middle_json.get("pdf_info") or []:
            page_idx = page_info.get("page_idx")
            page_number = int(page_idx) + 1 if page_idx is not None else None
            for para in page_info.get("para_blocks") or []:
                para_type = str(para.get("type") or "text").lower()
                bbox = self._coerce_bbox(para.get("bbox"))
                lines_text: list[str] = []
                for line in para.get("lines") or []:
                    spans = [str(span.get("content") or "") for span in (line.get("spans") or [])]
                    joined = "".join(spans).strip()
                    if joined:
                        lines_text.append(joined)
                content = "\n".join(lines_text).strip()
                if not content:
                    continue
                if para_type == "title":
                    heading_path = [content]
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.HEADING,
                            page_number=page_number,
                            heading_path=list(heading_path),
                            text=content,
                            markdown=f"# {content}",
                            bounding_box=bbox,
                            metadata={"source": "middle_json"},
                        )
                    )
                elif para_type == "table":
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.TABLE,
                            page_number=page_number,
                            heading_path=list(heading_path),
                            text=content,
                            markdown=content,
                            bounding_box=bbox,
                            metadata={"source": "middle_json"},
                        )
                    )
                elif para_type in {"interline_equation", "equation"}:
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.FORMULA,
                            page_number=page_number,
                            heading_path=list(heading_path),
                            text=content,
                            markdown=f"$$\n{content}\n$$",
                            bounding_box=bbox,
                            metadata={"source": "middle_json"},
                        )
                    )
                else:
                    blocks.append(
                        ParsedBlockData(
                            block_type=BlockType.PARAGRAPH,
                            page_number=page_number,
                            heading_path=list(heading_path),
                            text=content,
                            markdown=content,
                            bounding_box=bbox,
                            metadata={"source": "middle_json", "raw_type": para_type},
                        )
                    )
        return blocks

    async def cancel(self, external_task_id: str) -> CancelResult:
        """请求取消 MinerU 远端异步解析任务。"""
        mode, raw_id = self._parse_task_ref(external_task_id)
        endpoint = (
            f"{self.base_url}/extract-results/batch/{raw_id}/cancel"
            if mode == "batch"
            else f"{self.base_url}/extract/task/{raw_id}/cancel"
        )
        client, owns_client = self._get_client()
        try:
            resp = await client.post(endpoint, headers=self._build_api_headers())
            if resp.status_code == 404:
                return CancelResult(
                    external_task_id=external_task_id,
                    cancelled=False,
                    state=ParseJobState.UNKNOWN,
                    message="远端 MinerU 任务不存在或已过期",
                )
            resp.raise_for_status()
            return CancelResult(
                external_task_id=external_task_id,
                cancelled=True,
                state=ParseJobState.CANCELLED,
                message="已向 MinerU 提交任务取消请求",
            )
        except httpx.HTTPError as exc:
            logger.warning("MinerU 任务取消请求异常: error=%s", type(exc).__name__)
            return CancelResult(
                external_task_id=external_task_id,
                cancelled=False,
                state=ParseJobState.CANCEL_REQUESTED,
                message="MinerU 取消请求网络失败，已记录为待确认取消",
            )
        finally:
            if owns_client:
                await client.aclose()
