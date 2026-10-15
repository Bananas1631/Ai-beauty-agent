"""Сервис диалога на настоящих PostgreSQL и Redis, фейковом CRM и сценарной LLM."""

from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.llm.fake import ScriptedLLM, call_tool, reply
from beauty_agent.agent.budget import BUDGET_EXCEEDED_ANSWER, DailyBudget
from beauty_agent.agent.loop import HANDOFF_ANSWER
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind
from beauty_agent.conversation.service import HANDED_OFF_ANSWER, ConversationService, IncomingMessage
from beauty_agent.core.config import YclientsSettings
from beauty_agent.db.models import Conversation, Message, Salon, ToolCall
from beauty_agent.db.repositories.clients import ClientsRepository
from beauty_agent.domain.enums import Channel, ConversationStatus, CrmKind, MessageRole


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[AdminEvent] = []

    async def notify(self, event: AdminEvent) -> None:
        self.events.append(event)


@pytest.fixture
async def fake_salon(session: AsyncSession, salon: Salon) -> Salon:
    salon.settings = {"crm_kind": CrmKind.FAKE.value, "daily_llm_budget_rub": "1.00"}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    return salon


def make_service(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    llm: ScriptedLLM | None,
    notifier: RecordingNotifier,
) -> ConversationService:
    return ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=ProviderRegistry(YclientsSettings()),
        llm_clients={"scripted": llm} if llm else {},
        default_models=["scripted"],
        notifier=notifier,
    )


def message(
    salon: Salon, text: str, *, user: str = "web-session-0001", message_id: str | None = None
) -> IncomingMessage:
    return IncomingMessage(salon, Channel.WEB, user, text, external_message_id=message_id)


async def test_message_is_answered_and_logged(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    llm = ScriptedLLM([call_tool("find_service", {"query": "педикюр"}), reply("Педикюр с покрытием — 3 200 ₽.")])
    service = make_service(session_factory, redis, llm, RecordingNotifier())

    result = await service.handle(message(fake_salon, "Сколько стоит педикюр?"))

    assert result.text == "Педикюр с покрытием — 3 200 ₽."
    assert result.conversation_id is not None
    messages = (await session.scalars(select(Message).where(Message.conversation_id == result.conversation_id))).all()
    assert [m.role for m in messages] == [MessageRole.CLIENT, MessageRole.AGENT]
    assert messages[1].cost_rub == Decimal("0.0200")
    calls = (await session.scalars(select(ToolCall).where(ToolCall.conversation_id == result.conversation_id))).all()
    assert [call.name for call in calls] == ["find_service"]
    assert calls[0].result is not None  # журнал хранит, что вернул инструмент
    assert await DailyBudget(redis).spent(str(fake_salon.id)) == Decimal("0.02")


async def test_repeated_webhook_is_not_answered_twice(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    llm = ScriptedLLM([reply("Здравствуйте!")])
    service = make_service(session_factory, redis, llm, RecordingNotifier())

    first = await service.handle(message(fake_salon, "Привет", message_id="tg-1"))
    second = await service.handle(message(fake_salon, "Привет", message_id="tg-1"))

    assert first.text == "Здравствуйте!"
    assert second.duplicate
    assert second.text is None
    assert len(llm.calls) == 1


async def test_handoff_stops_the_agent(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    notifier = RecordingNotifier()
    llm = ScriptedLLM([call_tool("handoff_to_admin", {"reason": "Клиент просит живого человека"})])
    service = make_service(session_factory, redis, llm, notifier)

    first = await service.handle(message(fake_salon, "Позовите живого человека"))
    second = await service.handle(message(fake_salon, "Ну так что?"))

    assert first.handed_off
    assert first.text == HANDOFF_ANSWER
    assert second.text == HANDED_OFF_ANSWER  # администратор ведёт диалог — агент молчит
    assert [event.kind for event in notifier.events] == [AdminEventKind.HANDOFF]
    conversation = await session.get(Conversation, first.conversation_id)
    assert conversation is not None
    await session.refresh(conversation)
    assert conversation.status is ConversationStatus.HANDED_OFF


async def test_budget_exceeded_goes_to_admin_with_one_alert(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    notifier = RecordingNotifier()
    llm = ScriptedLLM()
    await DailyBudget(redis).add(str(fake_salon.id), Decimal("1.00"))  # лимит салона — 1 ₽ в день
    service = make_service(session_factory, redis, llm, notifier)

    first = await service.handle(message(fake_salon, "Привет", user="session-aaaa-0001"))
    second = await service.handle(message(fake_salon, "Привет", user="session-bbbb-0002"))

    assert first.text == BUDGET_EXCEEDED_ANSWER
    assert second.text == BUDGET_EXCEEDED_ANSWER
    assert llm.calls == []
    assert [event.kind for event in notifier.events] == [AdminEventKind.BUDGET_EXCEEDED]


async def test_no_models_configured(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    notifier = RecordingNotifier()
    service = make_service(session_factory, redis, None, notifier)

    result = await service.handle(message(fake_salon, "Привет"))

    assert result.handed_off
    assert notifier.events[0].text == "Не настроена ни одна LLM"


async def test_state_survives_between_messages(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    llm = ScriptedLLM([reply("Здравствуйте! Чем помочь?"), reply("Слушаю вас.")])
    service = make_service(session_factory, redis, llm, RecordingNotifier())

    await service.handle(message(fake_salon, "Привет"))
    await service.handle(message(fake_salon, "Ещё вопрос"))

    second_call = llm.calls[1][0]
    assert [m.content for m in second_call if m.role != "system"] == [
        "Привет",
        "Здравствуйте! Чем помочь?",
        "Ещё вопрос",
    ]


async def test_client_identity_and_phone(session: AsyncSession, salon: Salon) -> None:
    clients = ClientsRepository(session, salon.id)
    web = await clients.find_or_create(Channel.WEB, "session-1", name="Анна")
    same = await clients.find_or_create(Channel.WEB, "session-1")
    telegram = await clients.find_or_create(Channel.TELEGRAM, "tg-1")

    assert same.id == web.id
    assert telegram.id != web.id
    await clients.attach_phone(web, "8 (916) 150-22-39", Channel.WEB, "session-1")
    await session.flush()
    assert web.phone == "+79161502239"
    # тот же номер из Telegram — это тот же человек: учётная запись привязывается к существующей карточке
    owner = await clients.attach_phone(telegram, "+7 916 150 22 39", Channel.TELEGRAM, "tg-1")
    await session.flush()
    assert owner.id == web.id
    assert (await clients.by_identity(Channel.TELEGRAM, "tg-1")) is web
    profile = await clients.profile(web)
    assert profile.name == "Анна"
    assert profile.visits == 0
