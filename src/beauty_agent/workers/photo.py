"""Задача Celery для фото-референса: логика — conversation/photo.py, здесь — повторы и очередь.

Модель или мессенджер временно недоступны — повтор с растущей паузой; после последней попытки
фото уходит администратору, а не теряется.
"""

import uuid
from typing import Any

import structlog

from beauty_agent.adapters.channels.base import ChannelUpdate
from beauty_agent.conversation.photo import RETRYABLE, PhotoOutcome, PhotoProcessor
from beauty_agent.workers.celery_app import celery, run, runtime
from beauty_agent.workers.messages import MAX_RETRIES, backoff_s

logger = structlog.get_logger(__name__)


@celery.task(bind=True, name="messages.photo", max_retries=MAX_RETRIES, soft_time_limit=150, time_limit=180)
def photo_task(self: Any, account_id: str, update: dict[str, Any], text: str) -> str:
    current = runtime()
    processor: PhotoProcessor | None = getattr(current, "photo", None)
    if processor is None:
        return PhotoOutcome.SKIPPED.value
    final = self.request.retries >= MAX_RETRIES
    try:
        outcome = run(
            processor.process(uuid.UUID(account_id), ChannelUpdate.from_json(update), text, final_attempt=final)
        )
    except RETRYABLE as exc:
        logger.warning("photo_retry", attempt=self.request.retries + 1, error=type(exc).__name__)
        raise self.retry(exc=exc, countdown=backoff_s(self.request.retries)) from exc
    return outcome.value
