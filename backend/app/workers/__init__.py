"""异步 Worker 模块：Celery 应用实例、5 队列拓扑、Outbox 消费者与异步处理任务。"""

from app.workers.celery_app import (
    ALL_CELERY_QUEUES,
    CELERY_BEAT_SCHEDULE,
    CELERY_TASK_QUEUES,
    CELERY_TASK_ROUTES,
    QUEUE_DEFAULT,
    QUEUE_FAQ,
    QUEUE_INDEXING,
    QUEUE_INGESTION,
    QUEUE_OUTBOX,
    IdempotentRetryTask,
    celery_app,
    create_celery_app,
    ping_worker,
)
from app.workers.tasks import (
    OutboxDispatcherService,
    OutboxDispatchOutcome,
    OutboxRelayBatchReport,
    process_document_version_task,
    reconcile_active_index_pointers_task,
    relay_outbox_events_task,
    run_scheduled_question_clustering_task,
)

__all__ = [
    "ALL_CELERY_QUEUES",
    "CELERY_BEAT_SCHEDULE",
    "CELERY_TASK_QUEUES",
    "CELERY_TASK_ROUTES",
    "IdempotentRetryTask",
    "OutboxDispatchOutcome",
    "OutboxDispatcherService",
    "OutboxRelayBatchReport",
    "QUEUE_DEFAULT",
    "QUEUE_FAQ",
    "QUEUE_INDEXING",
    "QUEUE_INGESTION",
    "QUEUE_OUTBOX",
    "celery_app",
    "create_celery_app",
    "ping_worker",
    "process_document_version_task",
    "reconcile_active_index_pointers_task",
    "relay_outbox_events_task",
    "run_scheduled_question_clustering_task",
]
