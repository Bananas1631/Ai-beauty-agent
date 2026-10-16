"""GET /metrics для Prometheus. Снаружи закрыт в Caddy, Prometheus ходит изнутри сети Docker.

Перед выдачей обновляет то, что меряется состоянием, а не событиями: длину очередей RabbitMQ
и долю дневного лимита LLM у каждого салона. RabbitMQ недоступен — длина очереди -1:
алерт «очередь стоит» не должен молчать из-за того, что брокер упал.
"""

import asyncio

import structlog
from fastapi import APIRouter, Request, Response
from kombu import Connection
from sqlalchemy import select

from beauty_agent.agent.budget import DailyBudget
from beauty_agent.api.deps import SessionDep
from beauty_agent.core.metrics import LLM_BUDGET_RATIO, QUEUE_MESSAGES, render
from beauty_agent.db.models import Salon
from beauty_agent.workers.celery_app import DEAD_QUEUE, MESSAGES_QUEUE, PERIODIC_QUEUE

logger = structlog.get_logger(__name__)
router = APIRouter(include_in_schema=False)

QUEUES = (MESSAGES_QUEUE, PERIODIC_QUEUE, DEAD_QUEUE)


def queue_lengths(url: str) -> dict[str, int]:
    """Пассивное объявление очереди ничего не создаёт и возвращает число сообщений в ней."""
    lengths = dict.fromkeys(QUEUES, -1)
    try:
        with Connection(url, connect_timeout=2) as connection:
            channel = connection.default_channel
            for name in QUEUES:
                try:
                    _, count, _ = channel.queue_declare(queue=name, passive=True)
                    lengths[name] = int(count)
                except Exception:  # очереди ещё нет: воркер не стартовал ни разу
                    channel = connection.channel()
    except Exception as exc:
        logger.warning("metrics_rabbitmq_unavailable", error=type(exc).__name__)
    return lengths


@router.get("/metrics")
async def metrics(request: Request, session: SessionDep) -> Response:
    settings = request.app.state.settings
    lengths = await asyncio.to_thread(queue_lengths, settings.rabbitmq.url)
    for name, count in lengths.items():
        QUEUE_MESSAGES.labels(queue=name).set(count)

    budget = DailyBudget(request.app.state.redis)
    for salon_id, raw_settings in (await session.execute(select(Salon.id, Salon.settings))).all():
        limit = Salon(settings=raw_settings).config.daily_llm_budget_rub
        spent = await budget.spent(str(salon_id))
        LLM_BUDGET_RATIO.labels(salon_id=str(salon_id)).set(float(spent / limit) if limit else 0.0)

    body, content_type = render()
    return Response(body, media_type=content_type)
