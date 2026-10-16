"""Фоновая векторизация сообщений клиентов — для поиска по истории (rag/history_search.py).

Раз в минуту задача берёт свежие сообщения клиентов без вектора и считает эмбеддинги пачками
по салонам (расход на модель записывается салону). Только при согласии на обработку ПДн (152-ФЗ):
последнее событие журнала согласий клиента — «дано». Сообщения, написанные до согласия в том же
диалоге, векторизуются после него: окно — двое суток.

- Срок хранения тот же, что у переписки: вектор — колонка сообщения и удаляется вместе с ним
  (ночная задача хранения, отзыв согласия).
- Телефон и e-mail вырезаются до отправки в модель: для поиска они не нужны.
- Транзакция не держится, пока считается эмбеддинг: прочитали → закрыли → посчитали → записали
  только тем строкам, у которых вектора всё ещё нет. Сообщение, удалённое за это время, просто
  не обновится.
- Сбой модели — сообщения остаются без вектора и попадут в следующий запуск.
"""

import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core import metrics
from beauty_agent.db.models import Consent, Conversation, Message
from beauty_agent.domain.consent import strip_personal_data
from beauty_agent.domain.enums import ConsentType, MessageRole
from beauty_agent.domain.errors import DomainError
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_agent.workers.celery_app import celery, run, runtime

logger = structlog.get_logger(__name__)

PENDING_WINDOW = timedelta(days=2)
BATCH_SIZE = 64
MAX_BATCHES = 20  # за один запуск; остальное — через минуту
MAX_TEXT_CHARS = 2000


def _consent_granted() -> ColumnElement[bool]:
    """Последнее событие журнала согласий клиента этого сообщения — «дано»."""
    latest = (
        select(Consent.granted)
        .where(
            Consent.salon_id == Message.salon_id,
            Consent.client_id == Conversation.client_id,
            Consent.kind == ConsentType.PERSONAL_DATA,
        )
        .order_by(Consent.created_at.desc(), Consent.id.desc())
        .limit(1)
        .correlate(Message, Conversation)
        .scalar_subquery()
    )
    return latest.is_(True)


async def _pending(
    session_factory: async_sessionmaker[AsyncSession], since: datetime, limit: int
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    async with session_factory() as session:
        rows = await session.execute(
            select(Message.id, Message.salon_id, Message.content)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.role == MessageRole.CLIENT,
                Message.embedding.is_(None),
                Message.created_at > since,
                _consent_granted(),
            )
            .order_by(Message.created_at)
            .limit(limit)
        )
        return [(row.id, row.salon_id, row.content) for row in rows]


async def embed_pending(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingsClient,
    *,
    clock: Callable[[], datetime] | None = None,
    batch_size: int = BATCH_SIZE,
    max_batches: int = MAX_BATCHES,
) -> int:
    """Сколько сообщений получили вектор."""
    now = (clock or (lambda: datetime.now(UTC)))()
    embedded = 0
    failed: set[uuid.UUID] = set()  # салоны, у которых модель упала: в этом запуске не повторяем
    for _ in range(max_batches):
        pending = [row for row in await _pending(session_factory, now - PENDING_WINDOW, batch_size)]
        pending = [row for row in pending if row[1] not in failed]
        if not pending:
            break
        by_salon: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = defaultdict(list)
        for message_id, salon_id, content in pending:
            by_salon[salon_id].append((message_id, strip_personal_data(content)[:MAX_TEXT_CHARS]))
        for salon_id, items in by_salon.items():
            try:
                result = await embedder.embed([content for _, content in items])
            except DomainError as exc:
                failed.add(salon_id)
                logger.warning("history_embedding_failed", salon_id=str(salon_id), error=type(exc).__name__)
                continue
            async with session_factory() as session:
                await session.execute(
                    update(Message).where(Message.embedding.is_(None)).execution_options(synchronize_session=False),
                    [
                        {"id": message_id, "embedding": vector, "embedding_model": embedder.model_key}
                        for (message_id, _), vector in zip(items, result.vectors, strict=True)
                    ],
                )
                await session.commit()
            metrics.llm_cost(str(salon_id), result.cost_rub)
            embedded += len(items)
        if len(pending) < batch_size:
            break
    if embedded:
        logger.info("history_embedded", messages=embedded)
    return embedded


@celery.task(name="periodic.embed_messages")
def embed_messages_task() -> int:
    current = runtime()
    if not current.settings.llm.history_embeddings:
        return 0
    return run(embed_pending(current.session_factory, current.conversations.embedder))
