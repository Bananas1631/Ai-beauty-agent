"""Celery: очередь входящих сообщений и фоновых задач на RabbitMQ.

- Подтверждение задачи — после выполнения (acks_late): упавший воркер не теряет сообщение.
- Ошибка — повтор с растущей паузой (в задаче); после последней попытки задача отклоняется
  без возврата в очередь и по DLX попадает в очередь «мёртвых» задач, администратор получает уведомление.
- Async-код в синхронном Celery: у каждого процесса воркера свой постоянный цикл событий и свой
  Runtime (движок БД, Redis, HTTP). Движок asyncpg нельзя переиспользовать между разными asyncio.run.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from celery import Celery
from celery.signals import worker_init, worker_process_init, worker_process_shutdown
from kombu import Exchange, Queue

from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.logging import configure_logging

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
        task_routes={"messages.*": {"queue": MESSAGES_QUEUE}, "periodic.*": {"queue": PERIODIC_QUEUE}},
        timezone="Europe/Moscow",
        enable_utc=True,
        worker_hijack_root_logger=False,  # логи — через наш structlog с маскированием ПДн
        # RabbitMQ 4 запрещает временные неэксклюзивные очереди, а их создают удалённое управление
        # воркерами (pidbox) и события задач. Без этих двух строк воркер бесконечно переподключается.
        worker_enable_remote_control=False,
        worker_send_task_events=False,
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


celery = Celery("beauty_agent", include=["beauty_agent.workers.messages"])
configure(celery, get_settings())


@worker_init.connect
def _declare_on_start(sender: Any = None, **_: Any) -> None:
    app = getattr(sender, "app", None)
    if app is not None:
        declare_queues(app)


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
