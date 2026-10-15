"""Диалоги, сообщения и журнал инструментов в настоящем PostgreSQL."""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.agent.registry import ToolOutcome
from beauty_agent.db.models import Client, Salon
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole


async def make_client(session: AsyncSession, salon: Salon) -> Client:
    client = Client(salon_id=salon.id, name="Анна")
    session.add(client)
    await session.flush()
    return client


async def test_open_for_reuses_active_conversation(session: AsyncSession, salon: Salon) -> None:
    client = await make_client(session, salon)
    repo = ConversationsRepository(session, salon.id)

    first = await repo.open_for(client.id, Channel.WEB)
    again = await repo.open_for(client.id, Channel.WEB)
    other_channel = await repo.open_for(client.id, Channel.TELEGRAM)

    assert again.id == first.id
    assert other_channel.id != first.id

    client_id, first_id = client.id, first.id
    await repo.set_status(first_id, ConversationStatus.CLOSED)
    await session.commit()
    session.expire_all()  # после expire обращение к атрибутам — скрытый запрос, поэтому ID сохранены заранее
    assert (await repo.open_for(client_id, Channel.WEB)).id != first_id  # закрытый диалог не продолжается


async def test_repeated_webhook_message_is_skipped(session: AsyncSession, salon: Salon) -> None:
    client = await make_client(session, salon)
    repo = ConversationsRepository(session, salon.id)
    conversation = await repo.open_for(client.id, Channel.TELEGRAM)

    first = await repo.add_message(conversation.id, MessageRole.CLIENT, "Привет", external_id="tg-42")
    second = await repo.add_message(conversation.id, MessageRole.CLIENT, "Привет", external_id="tg-42")

    assert first is not None
    assert second is None
    assert len(await repo.last_messages(conversation.id)) == 1


async def test_restore_state_from_database(session: AsyncSession, salon: Salon) -> None:
    client = await make_client(session, salon)
    repo = ConversationsRepository(session, salon.id)
    conversation = await repo.open_for(client.id, Channel.WEB)
    for index in range(5):
        await repo.add_message(conversation.id, MessageRole.CLIENT, f"вопрос {index}")
        await repo.add_message(
            conversation.id, MessageRole.AGENT, f"ответ {index}", tokens_in=400, tokens_out=40, cost_rub=Decimal("0.01")
        )
    await repo.add_tool_calls(
        conversation.id,
        [ToolOutcome("c1", "find_service", {"query": "маникюр"}, {"found": [{"price": "2 500 ₽"}]}, None, 12)],
    )
    await repo.set_summary(conversation.id, "Клиент хочет маникюр")
    conversation_id = conversation.id
    await session.commit()
    session.expire_all()

    state = await repo.restore_state(conversation_id)

    assert state.summary == "Клиент хочет маникюр"
    assert len(state.turns) == 8  # последние 8 реплик
    assert state.turns[-1].role == "assistant"
    assert state.turns[-1].content == "ответ 4"
    assert state.turns[-2].content == "вопрос 4"
    assert "2 500 ₽" in state.facts[0]


async def test_other_salon_does_not_see_messages(session: AsyncSession, salon: Salon, other_salon: Salon) -> None:
    client = await make_client(session, salon)
    repo = ConversationsRepository(session, salon.id)
    conversation = await repo.open_for(client.id, Channel.WEB)
    await repo.add_message(conversation.id, MessageRole.CLIENT, "секрет")
    await session.commit()

    assert await ConversationsRepository(session, other_salon.id).last_messages(conversation.id) == []
