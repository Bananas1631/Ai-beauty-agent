"""Задачи Celery для мессенджеров: пачка склейки → сервис диалога → outbox; обновления админ-бота.

Повторы — с растущей паузой (5, 10, 20 секунд). После третьего повтора задача отклоняется и уходит
в очередь «мёртвых» задач (DLX), администратор салона получает уведомление.
"""

import uuid
from typing import Any

import structlog
from celery import Task

from beauty_agent.workers.celery_app import celery, run, runtime

logger = structlog.get_logger(__name__)

MAX_RETRIES = 3


def backoff_s(retries: int) -> int:
    return 5 * (1 << retries)


class MessageTask(Task):  # type: ignore[misc]  # у Celery нет стабов типов
    def on_failure(self, exc: BaseException, task_id: str, args: Any, kwargs: Any, einfo: Any) -> None:
        logger.error("message_dead_lettered", task_id=task_id, error=type(exc).__name__)
        processor = runtime().processor
        if self.name != process_batch_task.name or processor is None or not args:
            return
        try:
            run(processor.dead_letter(uuid.UUID(str(args[0]))))
        except Exception:  # уведомление не должно мешать отклонить задачу
            logger.exception("dead_letter_notify_failed")


@celery.task(bind=True, base=MessageTask, name="messages.process_batch", max_retries=MAX_RETRIES)
def process_batch_task(self: MessageTask, account_id: str, chat_id: str, token: str) -> None:
    try:
        run(runtime().processor.process(uuid.UUID(account_id), chat_id, token))
    except Exception as exc:
        logger.warning("message_retry", attempt=self.request.retries + 1, error=type(exc).__name__)
        raise self.retry(exc=exc, countdown=backoff_s(self.request.retries)) from exc


@celery.task(bind=True, base=MessageTask, name="messages.admin_update", max_retries=MAX_RETRIES)
def admin_update_task(self: MessageTask, payload: dict[str, Any]) -> None:
    """Нажатие кнопки или ответ администратора. Решения атомарны, outbox идемпотентен — повтор безопасен."""
    bot = runtime().admin_bot
    if bot is None:
        return
    try:
        run(bot.handle(payload))
    except Exception as exc:
        logger.warning("admin_update_retry", attempt=self.request.retries + 1, error=type(exc).__name__)
        raise self.retry(exc=exc, countdown=backoff_s(self.request.retries)) from exc
