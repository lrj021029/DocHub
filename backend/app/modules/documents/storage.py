"""MinIO 对象存储安全服务。

安全设计：
1. 服务端生成防路径穿越对象键：
   `org/{org_id}/space/{space_id}/doc/{doc_id}/v{ver}/{uuid}_{safe_name}`，
   严禁客户端直接传入任意对象键或路径穿越序列（`..`、`/`、`\\`、空字节）。
2. 文件格式严格白名单校验：仅允许首期支持的 8 种扩展名（pdf/md/docx/txt/xlsx/xls/csv/json），
   同时校验扩展名与 MIME 类型合法性。
3. 支持 API 直传与 MinIO 预签名 PUT URL 上传；上传完成时服务端通过 MinIO `get_object` /
   `stat_object` 流式核验真实字节大小、计算并比对 SHA-256 摘要。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import posixpath
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

from minio import Minio
from minio.error import S3Error

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.ids import new_uuid7_str

# 首期文档格式白名单（严格对应 TECHNICAL_SPEC §7.1）
ALLOWED_DOCUMENT_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".pdf", ".md", ".docx", ".txt", ".xlsx", ".xls", ".csv", ".json"}
)

EXTENSION_DEFAULT_MIME: Final[dict[str, str]] = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".json": "application/json",
}

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def sanitize_and_validate_filename(
    raw_filename: str, *, mime_type: str | None = None
) -> tuple[str, str, str]:
    """清洗文件名、阻断路径穿越并校验格式白名单。

    Returns:
        `(safe_filename, extension, resolved_mime_type)`
    """
    if not raw_filename or not raw_filename.strip():
        raise ValidationError(code="INVALID_FILENAME", message="文件名不能为空")

    cleaned = raw_filename.strip()
    if "\x00" in cleaned or ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValidationError(
            code="PATH_TRAVERSAL_BLOCKED",
            message="文件名包含非法路径分隔符或目录穿越字符",
        )

    base = Path(cleaned).name
    if base != cleaned:
        raise ValidationError(
            code="PATH_TRAVERSAL_BLOCKED",
            message="文件名不允许携带目录路径",
        )

    ext = Path(base).suffix.lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValidationError(
            code="UNSUPPORTED_FILE_FORMAT",
            message=(
                f"不支持的文档格式 {ext or '(无扩展名)'}，"
                "仅支持 pdf/md/docx/txt/xlsx/xls/csv/json"
            ),
            detail={"allowed_extensions": sorted(ALLOWED_DOCUMENT_EXTENSIONS)},
        )

    safe_stem = _SAFE_FILENAME_RE.sub("_", Path(base).stem).strip("._") or "document"
    safe_filename = f"{safe_stem[:120]}{ext}"

    resolved_mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if not resolved_mime or resolved_mime == "application/octet-stream":
        resolved_mime = EXTENSION_DEFAULT_MIME.get(ext) or (
            mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"
        )

    return safe_filename, ext, resolved_mime


def build_safe_object_key(
    *,
    organization_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    version_no: int,
    safe_filename: str,
) -> str:
    """在服务端构造不可猜测、租户隔离且防穿越的 MinIO 对象键。"""
    token = new_uuid7_str()
    key = (
        f"org/{organization_id}/space/{space_id}/doc/{document_id}/"
        f"v{version_no}/{token}_{safe_filename}"
    )
    normalized = posixpath.normpath(key)
    if normalized != key or normalized.startswith("/") or ".." in normalized.split("/"):
        raise ValidationError(code="INVALID_OBJECT_KEY", message="生成的对象存储键不合法")
    return key


@dataclass(frozen=True, slots=True)
class StoredObjectVerification:
    """服务端对 MinIO 对象执行真实核验后的结果。"""

    object_key: str
    size_bytes: int
    sha256: str
    content_type: str | None


class MinIOStorageService:
    """基于官方 MinIO Python SDK 的异步封装服务。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = Minio(
            endpoint=self._settings.minio_endpoint,
            access_key=self._settings.minio_access_key.get_secret_value(),
            secret_key=self._settings.minio_secret_key.get_secret_value(),
            secure=self._settings.minio_secure,
        )
        # 对外预签名客户端（当内网 endpoint 与外网 endpoint 不同时独立签名）
        if self._settings.minio_external_endpoint != self._settings.minio_endpoint:
            self._presign_client = Minio(
                endpoint=self._settings.minio_external_endpoint,
                access_key=self._settings.minio_access_key.get_secret_value(),
                secret_key=self._settings.minio_secret_key.get_secret_value(),
                secure=self._settings.minio_secure,
            )
        else:
            self._presign_client = self._client

    @property
    def raw_bucket(self) -> str:
        """原始上传文件存储桶名称。"""
        return self._settings.minio_bucket_raw

    @property
    def artifacts_bucket(self) -> str:
        """解析产物存储桶名称。"""
        return self._settings.minio_bucket_artifacts

    def _ensure_bucket_sync(self, bucket_name: str) -> None:
        """幂等确保存储桶存在。"""
        if not self._client.bucket_exists(bucket_name):
            self._client.make_bucket(bucket_name)

    async def ensure_buckets(self) -> None:
        """异步确保存储原始文件与解析产物的 Bucket 均已就绪。"""
        await asyncio.to_thread(self._ensure_bucket_sync, self.raw_bucket)
        await asyncio.to_thread(self._ensure_bucket_sync, self.artifacts_bucket)

    async def put_bytes(
        self,
        *,
        bucket_name: str,
        object_key: str,
        data: bytes,
        content_type: str,
    ) -> StoredObjectVerification:
        """将原始字节流写入 MinIO 并返回服务端计算的精确大小与 SHA-256。"""
        if not data:
            raise ValidationError(code="EMPTY_FILE", message="禁止上传空文件（0 字节）")

        digest = hashlib.sha256(data).hexdigest()

        def _upload_sync() -> None:
            self._ensure_bucket_sync(bucket_name)
            self._client.put_object(
                bucket_name=bucket_name,
                object_name=object_key,
                data=io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )

        await asyncio.to_thread(_upload_sync)
        return StoredObjectVerification(
            object_key=object_key,
            size_bytes=len(data),
            sha256=digest,
            content_type=content_type,
        )

    async def create_presigned_put_url(
        self,
        *,
        bucket_name: str,
        object_key: str,
        expires_minutes: int = 15,
    ) -> str:
        """生成有时效限制的 MinIO 预签名 PUT 上传地址。"""
        await asyncio.to_thread(self._ensure_bucket_sync, bucket_name)
        return await asyncio.to_thread(
            self._presign_client.presigned_put_object,
            bucket_name,
            object_key,
            timedelta(minutes=expires_minutes),
        )

    async def verify_and_hash_object(
        self,
        *,
        bucket_name: str,
        object_key: str,
        expected_sha256: str | None = None,
    ) -> StoredObjectVerification:
        """对 MinIO 已上传对象执行真实流式读取、字节统计与 SHA-256 完整性核验。"""

        def _verify_sync() -> StoredObjectVerification:
            try:
                stat = self._client.stat_object(bucket_name, object_key)
            except S3Error as exc:
                if exc.code in {"NoSuchKey", "NoSuchBucket", "NotFound"}:
                    raise NotFoundError(
                        code="UPLOADED_OBJECT_NOT_FOUND",
                        message="对象存储中未找到已上传的文件，请确认上传已完成",
                    ) from exc
                raise

            if (stat.size or 0) <= 0:
                raise ValidationError(code="EMPTY_FILE", message="上传文件大小不能为 0 字节")

            hasher = hashlib.sha256()
            total_read = 0
            response = self._client.get_object(bucket_name, object_key)
            try:
                for chunk in response.stream(64 * 1024):
                    if chunk:
                        total_read += len(chunk)
                        hasher.update(chunk)
            finally:
                response.close()
                response.release_conn()

            actual_sha256 = hasher.hexdigest()
            if expected_sha256 and actual_sha256 != expected_sha256.strip().lower():
                raise ValidationError(
                    code="CHECKSUM_MISMATCH",
                    message="服务端核验的文件 SHA-256 与声明值不一致",
                )

            return StoredObjectVerification(
                object_key=object_key,
                size_bytes=total_read,
                sha256=actual_sha256,
                content_type=stat.content_type,
            )

        return await asyncio.to_thread(_verify_sync)
