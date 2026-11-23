"""Celery 异步任务应用、5 队列隔离拓扑、幂等重试基类与 Beat 定时调度骨架。

架构约束（TECHNICAL_SPEC §1.2.10 & §7.6）：
1. 使用真实 Redis 作为 Celery Broker（DB 1）与 Result Backend（DB 2），连接串从
   `Settings.celery_broker_url` / `Settings.celery_result_url`（SecretStr）读取，绝不打入日志。
2. 定义 5 个物理隔离队列，避免大文件解析/向量重建阻塞实时 Outbox 同步与轻量任务：
   - `default`：通用轻量异步任务
   - `ingestion`：文档导入校验、MinerU/本地 OCR 解析与结构化切片
   - `indexing`：Embedding 向量化、Qdrant 写入与蓝绿索引构建/切换
   - `faq`：FAQ 定时聚类（HDBSCAN）、候选生成与缓存预热/失效
   - `outbox`：PostgreSQL Transactional Outbox 中继分发与 ACL 异步投影同步
3. 启用 `task_acks_late=True`、`task_reject_on_worker_lost=True` 与 `worker_prefetch_multiplier=1`，
   配合 `IdempotentRetryTask` 指数退避，实现至少一次投递与消费者幂等容错。
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Celery, Task
from celery.schedules import crontab
from kombu import Exchange, Queue

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 5 个标准隔离队列名称常量（供 Worker 启动参数 `-Q` 与任务路由引用）
QUEUE_DEFAULT = "default"
QUEUE_INGESTION = "ingestion"
QUEUE_INDEXING = "indexing"
QUEUE_FAQ = "faq"
QUEUE_OUTBOX = "outbox"

ALL_CELERY_QUEUES: tuple[str, ...] = (
    QUEUE_DEFAULT,
    QUEUE_INGESTION,
    QUEUE_INDEXING,
    QUEUE_FAQ,
    QUEUE_OUTBOX,
)

_DEFAULT_EXCHANGE = Exchange("dochub", type="direct")

CELERY_TASK_QUEUES: tuple[Queue, ...] = tuple(
    Queue(name, exchange=_DEFAULT_EXCHANGE, routing_key=name) for name in ALL_CELERY_QUEUES
)

# 按任务命名前缀自动路由至对应隔离队列
CELERY_TASK_ROUTES: dict[str, dict[str, str]] = {
    "app.workers.tasks.ingestion.*": {"queue": QUEUE_INGESTION, "routing_key": QUEUE_INGESTION},
    "app.workers.tasks.indexing.*": {"queue": QUEUE_INDEXING, "routing_key": QUEUE_INDEXING},
    "app.workers.tasks.faq.*": {"queue": QUEUE_FAQ, "routing_key": QUEUE_FAQ},
    "app.workers.tasks.outbox.*": {"queue": QUEUE_OUTBOX, "routing_key": QUEUE_OUTBOX},
    "app.workers.tasks.*": {"queue": QUEUE_DEFAULT, "routing_key": QUEUE_DEFAULT},
}

# Beat 定时调度骨架（业务 Task 实现在合并 Slice 2 持久化模型后注册接入）
CELERY_BEAT_SCHEDULE: dict[str, dict[str, Any]] = {
    "outbox-relay-sweep-every-15s": {
        "task": "app.workers.tasks.outbox.relay_pending_events",
        "schedule": 15.0,
        "options": {"queue": QUEUE_OUTBOX},
    },
    "index-reconcile-hourly": {
        "task": "app.workers.tasks.indexing.reconcile_active_index_pointers",
        "schedule": crontab(minute=5),
        "options": {"queue": QUEUE_INDEXING},
    },
    "faq-mining-nightly": {
        "task": "app.workers.tasks.faq.run_scheduled_question_clustering",
        "schedule": crontab(hour=2, minute=30),
        "options": {"queue": QUEUE_FAQ},
    },
}


class IdempotentRetryTask(Task):
    """带指数退避重试与结构化脱敏日志的幂等任务基类。

    说明：所有业务 Consumer 均须结合数据库 `processed_messages` / `idempotency_key`
    保证重复投递安全，严禁信任消息内缓存的权限结论。
    """

    abstract = True
    autoretry_for = (ConnectionError, TimeoutError, OSError)
    retry_backoff = True
    retry_backoff_max = 300
    retry_jitter = True
    max_retries = 5

    def on_retry(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        logger.warning(
            "Celery 任务触发幂等重试: task=%s task_id=%s error=%s",
            self.name,
            task_id,
            type(exc).__name__,
        )

    def on_failure(
        self,
        exc: Exception,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        logger.error(
            "Celery 任务最终执行失败: task=%s task_id=%s error=%s",
            self.name,
            task_id,
            type(exc).__name__,
        )


def create_celery_app(settings: Settings | None = None) -> Celery:
    """基于强类型配置构造 DocHub Celery 应用实例。"""
    cfg = settings or get_settings()
    app = Celery(
        "dochub",
        broker=cfg.celery_broker_url.get_secret_value(),
        backend=cfg.celery_result_url.get_secret_value(),
        include=["app.workers.tasks"],
        task_cls=IdempotentRetryTask,
    )

    app.conf.update(
        task_default_queue=QUEUE_DEFAULT,
        task_default_exchange=_DEFAULT_EXCHANGE.name,
        task_default_routing_key=QUEUE_DEFAULT,
        task_queues=CELERY_TASK_QUEUES,
        task_routes=CELERY_TASK_ROUTES,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # 至少一次投递可靠性与公平调度配置
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        result_expires=3600,
        broker_connection_retry_on_startup=True,
        beat_schedule=CELERY_BEAT_SCHEDULE,
    )
    return app


# 全局默认 Celery 实例（供 `celery -A app.workers.celery_app:celery_app worker` 启动）
celery_app = create_celery_app()


@celery_app.task(name="app.workers.tasks.health.ping")
def ping_worker() -> dict[str, str]:
    """Worker 存活探针任务（用于队列连通性验证）。"""
    return {"status": "ok", "service": "dochub-worker"}
