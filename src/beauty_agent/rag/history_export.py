"""Реальные диалоги в датасет evals: отбор поиском по истории и обезличивание.

Сценарии из жизни ловят то, чего нет в придуманных («а можно у Кати, но не в субботу»). Но это
переписка клиентов, поэтому:

- берутся только сообщения клиентов с согласием (у остальных нет векторов — поиск их не найдёт);
- телефоны и e-mail вырезаются, имя клиента из карточки заменяется на «[имя]», ID диалога
  заменяется хешем — по файлу нельзя найти человека;
- файл пишется только вне репозитория (scripts/export_dialogs.py проверяет путь): репозиторий
  публичный, датасеты реальных диалогов живут в приватном хранилище.

Ожидаемый итог в сценарии — заглушка с тегом needs_review: её заполняет человек, прочитав диалог.
"""

import hashlib
import re
import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Client, Conversation, Message
from beauty_agent.domain.consent import strip_personal_data
from beauty_agent.domain.enums import MessageRole
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_agent.rag.history_search import HistorySearch

NAME_PLACEHOLDER = "[имя]"
MAX_MESSAGES = 12
_MIN_NAME_CHARS = 2


def anonymize(text: str, names: Iterable[str]) -> str:
    """Без телефонов, e-mail и известных имён клиента (целыми словами, без учёта регистра)."""
    result = strip_personal_data(text)
    for name in {part for full in names for part in full.split() if len(part) >= _MIN_NAME_CHARS}:
        # «Анна», «Анне», «Анну»: основа имени без последней буквы, если имя длиннее трёх букв
        stem = re.escape(name[:-1] if len(name) > 3 else name)
        result = re.sub(rf"(?<!\w){stem}\w*", NAME_PLACEHOLDER, result, flags=re.IGNORECASE)
    return result


def scenario_id(conversation_id: uuid.UUID, salt: str) -> str:
    """Стабильный, но необратимый ID: соль — секрет экспорта, без неё диалог не сопоставить."""
    return "real-" + hashlib.sha256(f"{salt}:{conversation_id}".encode()).hexdigest()[:12]


async def export_dialogs(
    session: AsyncSession,
    salon_id: uuid.UUID,
    embedder: EmbeddingsClient,
    query: str,
    *,
    salt: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    hits = await HistorySearch(session, salon_id, embedder).search(query, since=since, until=until, limit=limit)
    conversation_ids = list(dict.fromkeys(hit.conversation_id for hit in hits))
    scenarios = []
    for conversation_id in conversation_ids:
        name = await session.scalar(
            select(Client.name)
            .join(Conversation, Conversation.client_id == Client.id)
            .where(Conversation.salon_id == salon_id, Conversation.id == conversation_id)
        )
        texts: Sequence[str] = (
            await session.scalars(
                select(Message.content)
                .where(
                    Message.salon_id == salon_id,
                    Message.conversation_id == conversation_id,
                    Message.role == MessageRole.CLIENT,
                )
                .order_by(Message.created_at, Message.id)
                .limit(MAX_MESSAGES)
            )
        ).all()
        scenarios.append(
            {
                "id": scenario_id(conversation_id, salt),
                "tags": ["real", "needs_review"],
                "messages": [anonymize(text, [name] if name else []) for text in texts],
                "expect": {"outcome": "no_booking"},
            }
        )
    return scenarios
