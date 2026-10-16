"""Векторизация истории и поиск по ней: только с согласием, только свой салон, период, повтор после сбоя."""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.db.models import Client, Message, Salon
from beauty_agent.db.repositories.consents import ConsentsRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.consent import strip_personal_data
from beauty_agent.domain.enums import Channel, MessageRole
from beauty_agent.domain.errors import LLMUnavailableError
from beauty_agent.rag.embeddings import Embedded, HashingEmbeddings
from beauty_agent.rag.history_export import export_dialogs
from beauty_agent.rag.history_search import HistorySearch
from beauty_agent.workers.embeddings import embed_pending
from evals.metrics import Scenario

EMBEDDER = HashingEmbeddings()


class BrokenEmbeddings(HashingEmbeddings):
    async def embed(self, texts: Sequence[str]) -> Embedded:
        raise LLMUnavailableError("embeddings down")


async def client_with(
    session: AsyncSession, salon: Salon, name: str, texts: Sequence[str], *, consent: bool | None
) -> list[uuid.UUID]:
    """Клиент с диалогом; consent: True — дал, False — дал и отозвал, None — не давал."""
    client = Client(salon_id=salon.id, name=name)
    session.add(client)
    await session.flush()
    conversations = ConversationsRepository(session, salon.id)
    conversation = await conversations.open_for(client.id, Channel.TELEGRAM)
    ids = []
    for text in texts:
        message_id = await conversations.add_message(conversation.id, MessageRole.CLIENT, text)
        ids.append(uuid.UUID(str(message_id)))
        await conversations.add_message(conversation.id, MessageRole.AGENT, f"Ответ на «{text}»")
    if consent is not None:
        consents = ConsentsRepository(session, salon.id)
        await consents.record(client.id, Channel.TELEGRAM, granted=True)
        if not consent:
            await consents.record(client.id, Channel.TELEGRAM, granted=False)
    await session.commit()
    return ids


async def embedded_ids(session: AsyncSession) -> set[uuid.UUID]:
    rows = await session.scalars(select(Message.id).where(Message.embedding.is_not(None)))
    return set(rows)


async def test_only_consented_client_messages_are_embedded(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    anna = await client_with(
        session, salon, "Анна", ["Сколько стоит наращивание? Мой телефон +7 916 150-22-39"], consent=True
    )
    olga = await client_with(session, salon, "Ольга", ["Делаете наращивание ресниц?"], consent=None)
    irina = await client_with(session, salon, "Ирина", ["Наращивание ногтей есть?"], consent=False)

    count = await embed_pending(session_factory, EMBEDDER)
    stored = (
        await session.execute(
            select(Message.content, Message.embedding, Message.embedding_model).where(Message.id == anna[0])
        )
    ).one()

    assert count == 1
    assert await embedded_ids(session) == set(anna)  # ответы агента и клиенты без согласия — без векторов
    assert not set(olga + irina) & await embedded_ids(session)
    assert stored.embedding_model == "hashing"
    # телефон вырезан до отправки в модель эмбеддингов
    expected = EMBEDDER.vector(strip_personal_data(stored.content))
    assert [round(float(value), 5) for value in stored.embedding] == [round(value, 5) for value in expected]
    assert await embed_pending(session_factory, EMBEDDER) == 0  # повторно не считает


async def test_failed_embedding_is_retried_next_run(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    ids = await client_with(session, salon, "Анна", ["Есть ли наращивание?"], consent=True)

    assert await embed_pending(session_factory, BrokenEmbeddings()) == 0
    assert await embedded_ids(session) == set()
    assert await embed_pending(session_factory, EMBEDDER) == 1
    assert await embedded_ids(session) == set(ids)


async def test_search_is_scoped_to_salon_and_period(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon, other_salon: Salon
) -> None:
    september = await client_with(session, salon, "Анна", ["Сколько стоит наращивание ногтей?"], consent=True)
    october = await client_with(session, salon, "Вера", ["А наращивание ногтей долго держится?"], consent=True)
    await client_with(session, salon, "Мария", ["Где вы находитесь?"], consent=True)
    await client_with(session, other_salon, "Чужая", ["Сколько стоит наращивание ногтей?"], consent=True)
    await embed_pending(session_factory, EMBEDDER)
    await session.execute(
        update(Message).where(Message.id == september[0]).values(created_at=datetime(2026, 9, 15, 12, tzinfo=UTC))
    )
    await session.commit()

    search = HistorySearch(session, salon.id, EMBEDDER)
    everything = await search.search("наращивание ногтей")
    in_september = await search.search(
        "наращивание ногтей", since=datetime(2026, 9, 1, tzinfo=UTC), until=datetime(2026, 10, 1, tzinfo=UTC)
    )

    assert {hit.message_id for hit in everything} == {september[0], october[0]}  # чужой салон не найден
    assert all(hit.score >= 0.2 for hit in everything)
    assert everything[0].score >= everything[-1].score
    assert [hit.message_id for hit in in_september] == september
    assert in_september[0].client_name == "Анна"


async def test_real_dialogs_are_exported_anonymized(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    await client_with(
        session,
        salon,
        "Анна Петрова",
        ["Здравствуйте, это Анна. Сколько стоит наращивание ногтей?", "Пишите anna.petrova@mail.ru или 89161502239"],
        consent=True,
    )
    await embed_pending(session_factory, EMBEDDER)

    scenarios = await export_dialogs(session, salon.id, EMBEDDER, "наращивание ногтей", salt="s" * 16)
    dumped = json.dumps(scenarios, ensure_ascii=False)

    assert len(scenarios) == 1
    assert scenarios[0]["id"].startswith("real-")
    assert scenarios[0]["tags"] == ["real", "needs_review"]
    assert scenarios[0]["messages"][0] == "Здравствуйте, это [имя]. Сколько стоит наращивание ногтей?"
    for leaked in ("Анна", "Петров", "anna.petrova", "1502239"):
        assert leaked not in dumped
    Scenario.model_validate(scenarios[0])  # формат датасета evals
