"""统一文档解析协议（DocumentParser Protocol）与同步轻量解析器基类。

架构要求（TECHNICAL_SPEC §7.2）：
- 所有外部异步解析器（如 MinerU、本地 OCR）与本地同步轻量解析器均实现 `DocumentParser` 协议，
  使上层 Worker 与 ParserRouter 能够以统一接口提交、轮询、拉取结果与取消任务。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

from app.core.errors import NotFoundError
from app.core.ids import new_uuid7_str
from app.modules.parsing.schemas import (
    CancelResult,
    ParseArtifactBundle,
    ParseJobState,
    ParseRequest,
    ParseStatus,
    ParseSubmission,
)


@runtime_checkable
class DocumentParser(Protocol):
    """统一文档解析器协议（异步与同步适配器均须遵循）。"""

    parser_provider: str
    parser_version: str

    async def submit(self, request: ParseRequest) -> ParseSubmission:
        """提交解析任务并返回任务回执。"""
        ...

    async def get_status(self, external_task_id: str) -> ParseStatus:
        """查询解析任务当前状态。"""
        ...

    async def fetch_result(self, external_task_id: str) -> ParseArtifactBundle:
        """拉取并规范化解析产物包。"""
        ...

    async def cancel(self, external_task_id: str) -> CancelResult:
        """尝试取消进行中的解析任务。"""
        ...


class SyncLightweightParser(ABC):
    """本地同步轻量解析器抽象基类。

    子类只需实现 `parse_bytes()` 专注格式解析，基类自动完成：
    - `DocumentParser` 协议的 `submit / get_status / fetch_result / cancel` 适配；
    - 基于 `idempotency_key` 的有界内存结果缓存与幂等对账；
    - 便捷同步/异步直接解析接口 `parse(request)`。
    """

    parser_provider: str = "lightweight_local"
    parser_version: str = "1.0.0"
    supported_extensions: tuple[str, ...] = ()
    supported_mime_types: tuple[str, ...] = ()

    def __init__(self, *, max_cache_entries: int = 128) -> None:
        self._max_cache_entries = max(1, max_cache_entries)
        self._bundles: OrderedDict[str, ParseArtifactBundle] = OrderedDict()
        self._idempotency_to_task: dict[str, str] = {}
        self._cancelled_tasks: set[str] = set()

    @abstractmethod
    def parse_bytes(
        self,
        file_bytes: bytes,
        *,
        filename: str,
        mime_type: str = "",
        extra_options: dict[str, Any] | None = None,
    ) -> ParseArtifactBundle:
        """同步解析真实文件字节，产出标准化 `ParseArtifactBundle`。"""

    def parse(self, request: ParseRequest) -> ParseArtifactBundle:
        """直接执行同步解析（供本机同步管道与单元测试直接调用）。"""
        if request.file_bytes is None:
            raise ValueError(f"轻量解析器 {self.parser_provider} 需要提供 file_bytes")
        return self.parse_bytes(
            request.file_bytes,
            filename=request.filename,
            mime_type=request.mime_type,
            extra_options=request.extra_options,
        )

    def _store_bundle(
        self, task_id: str, idempotency_key: str, bundle: ParseArtifactBundle
    ) -> None:
        self._bundles[task_id] = bundle
        self._bundles.move_to_end(task_id)
        self._idempotency_to_task[idempotency_key] = task_id
        while len(self._bundles) > self._max_cache_entries:
            evicted_task_id, _ = self._bundles.popitem(last=False)
            for key in [k for k, v in self._idempotency_to_task.items() if v == evicted_task_id]:
                self._idempotency_to_task.pop(key, None)

    @staticmethod
    def compute_sha256(data: bytes) -> str:
        """计算字节内容的 SHA-256 十六进制摘要。"""
        return hashlib.sha256(data).hexdigest()

    async def submit(self, request: ParseRequest) -> ParseSubmission:
        """以同步方式完成本地解析并登记为已完成任务，满足统一 DocumentParser 协议。"""
        existing_id = request.existing_external_task_id or self._idempotency_to_task.get(
            request.idempotency_key
        )
        if existing_id and existing_id in self._bundles:
            return ParseSubmission(
                external_task_id=existing_id,
                parser_provider=self.parser_provider,
                parser_version=self.parser_version,
                status=ParseJobState.SUCCEEDED,
                idempotency_key=request.idempotency_key,
                is_reconciled=True,
                metadata={"mode": "sync_local_reconciled"},
            )

        bundle = self.parse(request)
        task_id = f"{self.parser_provider}:{new_uuid7_str()}"
        self._store_bundle(task_id, request.idempotency_key, bundle)
        return ParseSubmission(
            external_task_id=task_id,
            parser_provider=self.parser_provider,
            parser_version=self.parser_version,
            status=ParseJobState.SUCCEEDED,
            idempotency_key=request.idempotency_key,
            is_reconciled=False,
            metadata={"block_count": len(bundle.blocks)},
        )

    async def get_status(self, external_task_id: str) -> ParseStatus:
        """查询本地同步任务状态。"""
        if external_task_id in self._cancelled_tasks:
            return ParseStatus(
                external_task_id=external_task_id,
                state=ParseJobState.CANCELLED,
                provider_state="cancelled",
                progress=100.0,
            )
        if external_task_id not in self._bundles:
            raise NotFoundError(f"未找到本地解析任务：{external_task_id}")
        return ParseStatus(
            external_task_id=external_task_id,
            state=ParseJobState.SUCCEEDED,
            provider_state="done",
            progress=100.0,
        )

    async def fetch_result(self, external_task_id: str) -> ParseArtifactBundle:
        """获取本地同步解析结果包。"""
        bundle = self._bundles.get(external_task_id)
        if bundle is None:
            raise NotFoundError(f"未找到本地解析任务结果：{external_task_id}")
        return bundle

    async def cancel(self, external_task_id: str) -> CancelResult:
        """取消或清理本地任务缓存；同步任务通常已瞬时完成。"""
        if external_task_id in self._bundles:
            return CancelResult(
                external_task_id=external_task_id,
                cancelled=False,
                state=ParseJobState.SUCCEEDED,
                message="本地同步解析已完成，无需取消",
            )
        self._cancelled_tasks.add(external_task_id)
        return CancelResult(
            external_task_id=external_task_id,
            cancelled=True,
            state=ParseJobState.CANCELLED,
            message="任务已标记取消",
        )
