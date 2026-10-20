"""Celery: очередь входящих сообщений и фоновых задач на RabbitMQ.

- Подтверждение задачи — после выполнения (acks_late): упавший воркер не теряет сообщение.
- Ошибка — повтор с растущей паузой (в задаче); после последней попытки задача отклоняется
  без возврата в очередь и по DLX попадает в очередь «мёртвых» задач, администратор получает уведомление.
- Async-код в синхронном Celery: у каждого процесса воркера свой постоянный цикл событий и свой
  Runtime (движок БД, Redis, HTTP). Движок asyncpg нельзя переиспользовать между разными asyncio.run.
- trace_id запроса API уходит в заголовке задачи: в логах и Sentry воркера — тот же ID, что у вебхука.
- Метрики: дочерние процессы пишут в PROMETHEUS_MULTIPROC_DIR, главный отдаёт сводку на METRICS_PORT.
"""

import asyncio
import os
import uuid
from collections.abc import Coroutine
from typing import Any

import sentry_sdk
import structlog
from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    before_task_publish,
    task_postrun,
    task_prerun,
    worker_init,
    worker_process_init,
    worker_process_shutdown,
)
from kombu import Exchange, Queue

from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.metrics import serve_worker_metrics
from beauty_agent.core.sentry import init_sentry

MESSAGES_QUEUE = "messages"
PERIODIC_QUEUE = "periodic"
DEAD_QUEUE = "dead"
_DLX = Exchange("dlx", type="direct")
_DEAD_LETTER = {"x-dead-letter-exchange": "dlx", "x-dead-letter-routing-key": DEAD_QUEUE}


def configure(app: Celery, settings: Settings) -> None:
    app.conf.update(
        broker_url=settings.rabbitmq.url,
        broker_connection_retry_on_startup=True,
        task_serializer="json",
        accept_content=["json"],  # без pickle: из очереди не выполнится чужой код
        task_ignore_result=True,
        result_backend=None,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_acks_on_failure_or_timeout=False,  # окончательно упавшая задача — в DLX, а не в никуда
        worker_prefetch_multiplier=1,
        task_time_limit=300,
        task_soft_time_limit=240,
        task_default_queue=MESSAGES_QUEUE,
        task_queues=(
            Queue(
                MESSAGES_QUEUE,
                Exchange(MESSAGES_QUEUE, type="direct"),
                routing_key=MESSAGES_QUEUE,
                queue_arguments=_DEAD_LETTER,
            ),
            Queue(
                PERIODIC_QUEUE,
                Exchange(PERIODIC_QUEUE, type="direct"),
                routing_key=PERIODIC_QUEUE,
                queue_arguments=_DEAD_LETTER,
            ),
            Queue(DEAD_QUEUE, _DLX, routing_key=DEAD_QUEUE),
        ),
        # jobs.* — долгие задачи по запросу администратора (переиндексация): рядом с периодическими,
        # чтобы не задерживать ответы клиентам в очереди messages
        task_routes={
            "messages.*": {"queue": MESSAGES_QUEUE},
            "periodic.*": {"queue": PERIODIC_QUEUE},
            "jobs.*": {"queue": PERIODIC_QUEUE},
        },
        timezone="Europe/Moscow",
        enable_utc=True,
        worker_hijack_root_logger=False,  # логи — через наш structlog с маскированием ПДн
        # RabbitMQ 4 запрещает временные неэксклюзивные очереди, а их создают удалённое управление
        # воркерами (pidbox) и события задач. Без этих двух строк воркер бесконечно переподключается.
        worker_enable_remote_control=False,
        worker_send_task_events=False,
        beat_schedule={
            # ночью, когда салоны закрыты: удаление переписки старше срока хранения
            "retention-nightly": {"task": "periodic.retention", "schedule": crontab(hour=3, minute=30)},
            "catalog-sync-nightly": {"task": "periodic.catalog_sync", "schedule": crontab(hour=4, minute=0)},
            "reminders": {"task": "periodic.reminders", "schedule": crontab(minute="*/5")},
            "outbox-sweep": {"task": "periodic.outbox_sweep", "schedule": crontab(minute="*")},
            "embed-messages": {"task": "periodic.embed_messages", "schedule": crontab(minute="*")},
            # визит наступил — запись засчитана ИИ: событие booking.attributed для потребителей
            "attribute-visits": {"task": "periodic.attribute_visits", "schedule": crontab(minute="*/15")},
            "weekly-report": {"task": "periodic.weekly_report", "schedule": crontab(hour=10, minute=0, day_of_week=1)},
            # выгрузка для счёта за прошлый месяц — владельцам салонов файлом Excel
            "monthly-invoice": {
                "task": "periodic.monthly_invoice",
                "schedule": crontab(hour=9, minute=0, day_of_month=1),
            },
        },
    )


def declare_queues(app: Celery) -> None:
    """Объявить все очереди и обменники, включая DLX и очередь мёртвых задач.

    Воркер объявляет только очереди, которые читает (-Q messages,periodic). Без обменника dlx
    RabbitMQ молча выбросил бы «мёртвое» сообщение — объявляем его сами при старте воркера.
    """
    with app.connection_for_write() as connection:
        channel = connection.default_channel
        for queue in app.conf.task_queues:
            queue.bind(channel).declare()


celery = Celery(
    "beauty_agent",
    include=[
        "beauty_agent.workers.messages",
        "beauty_agent.workers.chat",
        "beauty_agent.workers.retention",
        "beauty_agent.workers.reminders",
        "beauty_agent.workers.catalog_sync",
        "beauty_agent.workers.reports",
        "beauty_agent.workers.indexing",
        "beauty_agent.workers.embeddings",
        "beauty_agent.workers.photo",
        "beauty_agent.workers.attribution",
    ],
)
configure(celery, get_settings())


TRACE_HEADER = "trace_id"


@worker_init.connect
def _declare_on_start(sender: Any = None, **_: Any) -> None:
    app = getattr(sender, "app", None)
    if app is not None:
        declare_queues(app)
    port = os.environ.get("METRICS_PORT")
    if port and port.isdigit():
        serve_worker_metrics(int(port))


@before_task_publish.connect
def _attach_trace_id(headers: dict[str, Any] | None = None, **_: Any) -> None:
    """Задача, поставленная из вебхука, несёт trace_id этого запроса."""
    trace_id = structlog.contextvars.get_contextvars().get("trace_id")
    if headers is not None and trace_id and TRACE_HEADER not in headers:
        headers[TRACE_HEADER] = trace_id


@task_prerun.connect
def _bind_trace_id(task: Any = None, **_: Any) -> None:
    trace_id = getattr(getattr(task, "request", None), TRACE_HEADER, None) or uuid.uuid4().hex
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(trace_id=str(trace_id), task=getattr(task, "name", None))
    sentry_sdk.set_tag("trace_id", str(trace_id))


@task_postrun.connect
def _unbind_trace_id(**_: Any) -> None:
    structlog.contextvars.clear_contextvars()


_loop: asyncio.AbstractEventLoop | None = None
_runtime: Any = None


def runtime() -> Any:
    """Runtime процесса воркера; создаётся при старте процесса или при первой задаче (pool=solo)."""
    global _loop, _runtime
    if _runtime is None:
        from beauty_agent.runtime import create_runtime
        from beauty_agent.workers.queue import CeleryQueue

        settings = get_settings()
        configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
        init_sentry(settings.sentry)  # интеграция Celery включается сама: ошибки задач с trace_id
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        _runtime = create_runtime(settings, queue=CeleryQueue())
    return _runtime


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    runtime()
    assert _loop is not None  # noqa: S101 — создан в runtime()
    return _loop.run_until_complete(coroutine)


@worker_process_init.connect
def _init_process(**_: Any) -> None:
    runtime()


@worker_process_shutdown.connect
def _shutdown_process(**_: Any) -> None:
    global _runtime
    if _runtime is not None and _loop is not None:
        _loop.run_until_complete(_runtime.close())
        _loop.close()
        _runtime = None
