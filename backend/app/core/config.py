"""应用配置中心（基于 pydantic-settings）。

严格遵循以下安全与架构约束：
1. 兼容从仓库根目录（../.env）与后端目录（.env）加载环境变量。
2. 强制区分数据库迁移角色（app_migrator）与运行期角色（app_runtime）连接串。
3. 所有密钥与凭证字段统一使用 SecretStr，严禁在日志、错误或调试输出中打印明文。
4. 完整支持 MinerU 官方 API / 本地 OCR 预留、OpenAI 兼容 LLM、Qwen / 本地 BGE 双选路由配置。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 计算可能的 .env 路径：支持工作目录位于 backend/ 或仓库根目录
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT_DIR = _BACKEND_DIR.parent
_DEFAULT_ENV_FILES = (
    str(_REPO_ROOT_DIR / ".env"),
    str(_BACKEND_DIR / ".env"),
    "../.env",
    ".env",
)


class Settings(BaseSettings):
    """DocHub 后端强类型运行配置。"""

    model_config = SettingsConfigDict(
        env_file=_DEFAULT_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- 运行环境与服务监听 ---
    app_name: str = Field(default="DocHub API", description="服务展示名称")
    app_version: str = Field(default="0.1.0", description="服务语义化版本号")
    app_env: Literal["development", "test", "staging", "production"] = Field(
        default="development",
        description="当前运行环境标识",
    )
    app_debug: bool = Field(default=False, description="是否启用调试模式（生产必须为 false）")
    app_host: str = Field(default="0.0.0.0", description="监听主机地址")
    app_port: int = Field(default=8000, ge=1, le=65535, description="监听端口")
    log_level: str = Field(default="INFO", description="全局日志级别")
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description="CORS 严格白名单（逗号分隔）",
    )

    # --- 认证安全、JWT、Cookie 与 CSRF ---
    jwt_secret_key: SecretStr = Field(
        default=SecretStr("CHANGE_ME_AT_LEAST_32_CHARS_RANDOM_SECRET"),
        description="Access JWT 签名密钥（仅服务端持有）",
    )
    jwt_algorithm: str = Field(default="HS256", description="JWT 签名算法")
    jwt_issuer: str = Field(default="dochub", description="JWT 签发者声明（iss）")
    jwt_access_token_expire_minutes: int = Field(
        default=15,
        ge=1,
        le=1440,
        description="Access JWT 过期时间（分钟）",
    )
    jwt_refresh_token_expire_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="Refresh Token 过期时间（天）",
    )
    cookie_secure: bool = Field(
        default=False,
        description="Refresh Cookie 是否仅通过 HTTPS 发送（生产必须为 true）",
    )
    cookie_samesite: Literal["lax", "strict", "none"] = Field(
        default="lax",
        description="Refresh Cookie SameSite 策略",
    )
    csrf_secret_key: SecretStr = Field(
        default=SecretStr("CHANGE_ME_CSRF_RANDOM_SECRET"),
        description="CSRF Token 签名密钥",
    )

    # --- PostgreSQL 双角色连接配置（严禁运行期使用迁移角色或超级用户） ---
    postgres_host: str = Field(default="127.0.0.1", description="PostgreSQL 主机")
    postgres_port: int = Field(default=15432, ge=1, le=65535, description="PostgreSQL 端口")
    postgres_db: str = Field(default="dochub", description="PostgreSQL 数据库名")
    postgres_migrator_user: str = Field(
        default="app_migrator",
        description="Alembic DDL 迁移专用角色（表 Owner）",
    )
    postgres_migrator_password: SecretStr = Field(
        default=SecretStr("dochub_migrator_dev"),
        description="迁移角色密码",
    )
    postgres_runtime_user: str = Field(
        default="app_runtime",
        description="API/Worker 运行期受限角色（无 BYPASSRLS）",
    )
    postgres_runtime_password: SecretStr = Field(
        default=SecretStr("dochub_runtime_dev"),
        description="运行期角色密码",
    )
    migrator_database_url_override: SecretStr | None = Field(
        default=None,
        alias="MIGRATOR_DATABASE_URL",
        description="显式覆盖迁移数据库连接串（可选）",
    )
    runtime_database_url_override: SecretStr | None = Field(
        default=None,
        alias="RUNTIME_DATABASE_URL",
        description="显式覆盖运行期数据库连接串（可选）",
    )
    db_pool_size: int = Field(default=10, ge=1, le=100, description="运行期数据库连接池基础大小")
    db_max_overflow: int = Field(default=10, ge=0, le=100, description="运行期连接池最大溢出数")
    db_pool_timeout: int = Field(default=30, ge=1, le=300, description="连接池获取超时（秒）")
    db_pool_recycle: int = Field(default=1800, ge=60, description="连接池回收周期（秒）")
    db_echo: bool = Field(default=False, description="是否打印 SQL 语句（生产禁止开启）")

    # --- Redis（缓存 + Celery Broker/Result） ---
    redis_host: str = Field(default="127.0.0.1", description="Redis 主机")
    redis_port: int = Field(default=16379, ge=1, le=65535, description="Redis 端口")
    redis_password: SecretStr = Field(
        default=SecretStr("dochub_redis_dev"),
        description="Redis 访问密码",
    )
    redis_cache_db: int = Field(default=0, ge=0, le=15, description="业务缓存 DB 编号")
    redis_celery_broker_db: int = Field(default=1, ge=0, le=15, description="Celery Broker DB 编号")
    redis_celery_result_db: int = Field(default=2, ge=0, le=15, description="Celery Result DB 编号")

    # --- MinIO（原始文档与解析制品对象存储） ---
    minio_endpoint: str = Field(default="127.0.0.1:19000", description="MinIO 内部服务地址")
    minio_external_endpoint: str = Field(
        default="127.0.0.1:19000",
        description="MinIO 预签名对外访问地址",
    )
    minio_access_key: SecretStr = Field(
        default=SecretStr("dochub_minio_dev"),
        description="MinIO Access Key",
    )
    minio_secret_key: SecretStr = Field(
        default=SecretStr("dochub_minio_secret_dev"),
        description="MinIO Secret Key",
    )
    minio_secure: bool = Field(default=False, description="MinIO 是否启用 HTTPS")
    minio_bucket_raw: str = Field(default="dochub-raw", description="原始上传文件存储桶")
    minio_bucket_artifacts: str = Field(
        default="dochub-artifacts",
        description="解析产物存储桶",
    )

    # --- Qdrant（混合检索向量库） ---
    qdrant_host: str = Field(default="127.0.0.1", description="Qdrant 主机")
    qdrant_port: int = Field(default=6333, ge=1, le=65535, description="Qdrant HTTP 端口")
    qdrant_grpc_port: int = Field(default=6334, ge=1, le=65535, description="Qdrant gRPC 端口")
    qdrant_api_key: SecretStr | None = Field(
        default=SecretStr("dochub_qdrant_dev_key"),
        description="Qdrant API Key",
    )
    qdrant_https: bool = Field(default=False, description="Qdrant 是否启用 HTTPS")

    # --- MinerU 官方 API 与本地 OCR 扩展接口 ---
    mineru_api_base_url: str = Field(
        default="https://mineru.net/api/v4",
        description="MinerU 官方 API 基础地址",
    )
    mineru_api_version: str = Field(default="v4", description="MinerU API 版本标识")
    mineru_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="MinerU 官方 API Key（仅存服务端）",
    )
    mineru_timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=600,
        description="MinerU 单次请求超时",
    )
    mineru_poll_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=120,
        description="MinerU 轮询间隔",
    )
    mineru_max_poll_attempts: int = Field(
        default=120,
        ge=1,
        le=1000,
        description="MinerU 最大轮询次数",
    )
    local_ocr_enabled: bool = Field(
        default=False,
        description="是否启用本地 OCR（L2/L3 扫描 PDF 缺失本地 OCR 时必须 Fail-Closed）",
    )
    local_ocr_endpoint: str = Field(default="", description="本地 OCR 服务端点地址")

    # --- OpenAI-compatible LLM Provider ---
    llm_provider: str = Field(default="openai_compatible", description="LLM Provider 类型")
    llm_base_url: str = Field(
        default="https://100milionseek.top/v1",
        description="OpenAI 兼容 LLM 接口地址",
    )
    llm_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="LLM API Key（仅存服务端）",
    )
    llm_default_model: str = Field(default="gpt-4o-mini", description="默认对话模型名称")
    llm_timeout_seconds: int = Field(default=120, ge=5, le=600, description="LLM 请求超时（秒）")

    # --- Embedding Provider 双选架构（Qwen 优先 + 本地 BGE） ---
    embedding_primary_provider: Literal["qwen", "local_bge"] = Field(
        default="qwen",
        description="主 Embedding Provider（优先 qwen，支持切换 local_bge）",
    )
    qwen_api_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="Qwen OpenAI 兼容 API 地址",
    )
    qwen_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="Qwen API Key（仅存服务端）",
    )
    qwen_embedding_model: str = Field(
        default="text-embedding-v3",
        description="Qwen 默认向量模型",
    )
    qwen_embedding_dimensions: int = Field(
        default=1024,
        ge=64,
        le=8192,
        description="Qwen 向量维度",
    )
    local_bge_embedding_enabled: bool = Field(
        default=False,
        description="是否启用本地 BGE Embedding 服务",
    )
    local_bge_embedding_url: str = Field(
        default="http://127.0.0.1:8081/v1",
        description="本地 BGE Embedding 接口地址",
    )
    local_bge_embedding_model: str = Field(
        default="BAAI/bge-m3",
        description="本地 BGE Embedding 模型标识",
    )
    local_bge_embedding_dimensions: int = Field(
        default=1024,
        ge=64,
        le=8192,
        description="本地 BGE 向量维度",
    )

    # --- Reranker Provider 双选架构（Qwen 优先 + 本地 BGE） ---
    reranker_primary_provider: Literal["qwen", "local_bge"] = Field(
        default="qwen",
        description="主 Reranker Provider（优先 qwen，支持切换 local_bge）",
    )
    qwen_rerank_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
        description="Qwen Rerank API 地址",
    )
    qwen_rerank_model: str = Field(default="gte-rerank", description="Qwen 默认重排模型")
    local_bge_rerank_enabled: bool = Field(
        default=False,
        description="是否启用本地 BGE Reranker 服务",
    )
    local_bge_rerank_url: str = Field(
        default="http://127.0.0.1:8082/v1/rerank",
        description="本地 BGE Reranker 接口地址",
    )
    local_bge_rerank_model: str = Field(
        default="BAAI/bge-reranker-v2-m3",
        description="本地 BGE Reranker 模型标识",
    )

    # --- 可观测性（OpenTelemetry / Prometheus） ---
    otel_enabled: bool = Field(default=True, description="是否启用 OpenTelemetry 链路追踪")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://127.0.0.1:4317",
        description="OTLP gRPC 导出地址",
    )
    otel_service_name: str = Field(default="dochub-api", description="OTel 服务标识")
    readiness_check_timeout_seconds: float = Field(
        default=3.0,
        ge=0.5,
        le=30.0,
        description="/api/v1/readyz 单项依赖探活超时时间（秒）",
    )

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        """统一日志级别为大写字符串。"""
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            return "INFO"
        return normalized

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        """解析严格 CORS 白名单列表，禁止隐式通配符放行。"""
        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]

    def _build_pg_async_url(self, username: str, password: SecretStr) -> str:
        """按安全编码规则拼接 SQLAlchemy asyncpg 连接串。"""
        encoded_user = quote_plus(username)
        encoded_pwd = quote_plus(password.get_secret_value())
        return (
            f"postgresql+asyncpg://{encoded_user}:{encoded_pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def migrator_database_url(self) -> SecretStr:
        """获取 Alembic DDL 迁移专用的 PostgreSQL 异步连接串（app_migrator）。"""
        if self.migrator_database_url_override is not None:
            return self.migrator_database_url_override
        return SecretStr(
            self._build_pg_async_url(
                self.postgres_migrator_user,
                self.postgres_migrator_password,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def runtime_database_url(self) -> SecretStr:
        """获取运行期受限 PostgreSQL 异步连接串（app_runtime，强制受 RLS 约束）。"""
        if self.runtime_database_url_override is not None:
            return self.runtime_database_url_override
        return SecretStr(
            self._build_pg_async_url(
                self.postgres_runtime_user,
                self.postgres_runtime_password,
            )
        )

    def _build_redis_url(self, db_index: int) -> SecretStr:
        """构造带鉴权的 Redis 连接串。"""
        raw_pwd = self.redis_password.get_secret_value()
        auth_part = f":{quote_plus(raw_pwd)}@" if raw_pwd else ""
        return SecretStr(f"redis://{auth_part}{self.redis_host}:{self.redis_port}/{db_index}")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_cache_url(self) -> SecretStr:
        """获取业务缓存 Redis 连接串。"""
        return self._build_redis_url(self.redis_cache_db)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_broker_url(self) -> SecretStr:
        """获取 Celery Broker Redis 连接串。"""
        return self._build_redis_url(self.redis_celery_broker_db)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def celery_result_url(self) -> SecretStr:
        """获取 Celery Result Backend Redis 连接串。"""
        return self._build_redis_url(self.redis_celery_result_db)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def qdrant_http_url(self) -> str:
        """获取 Qdrant HTTP 探活与调用基础地址。"""
        scheme = "https" if self.qdrant_https else "http"
        return f"{scheme}://{self.qdrant_host}:{self.qdrant_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取进程级缓存的配置实例。"""
    return Settings()
