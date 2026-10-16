"""Согласия по 152-ФЗ на настоящих PostgreSQL и Redis: выдача, отзыв, удаление данных, срок хранения."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import RecordingNotifier

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.llm.fake import ScriptedLLM, call_tool, reply
from beauty_agent.agent.state import StateStore
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.privacy import PrivacyService
from beauty_agent.conversation.service import ConversationService, IncomingMessage
from beauty_agent.core.config import YclientsSettings
from beauty_agent.db.models import Client, ClientIdentity, Consent, Conversation, Message, Salon, ToolCall
from beauty_agent.domain.consent import CONSENT_VERSION, WITHDRAWN_ANSWER
from beauty_agent.domain.enums import Channel, CrmKind, MessageRole
from beauty_agent.workers.retention import purge_expired

USER = "web-session-privacy-01"


async def with_fake_crm(session: AsyncSession, salon: Salon) -> Salon:
    salon.settings = {"crm_kind": CrmKind.FAKE.value}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    return salon


@pytest.fixture
async def fake_salon(session: AsyncSession, salon: Salon) -> Salon:
    return await with_fake_crm(session, salon)


def make_service(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, llm: ScriptedLLM
) -> ConversationService:
    return ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=ProviderRegistry(YclientsSettings()),
        llm_clients={"scripted": llm},
        default_models=["scripted"],
        notifier=RecordingNotifier(),
        privacy=PrivacyService(redis, public_url="https://agent.example"),
    )


def message(salon: Salon, text: str, *, user: str = USER) -> IncomingMessage:
    return IncomingMessage(salon, Channel.WEB, user, text)


async def test_without_consent_phone_is_not_stored_or_sent_to_llm(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    llm = ScriptedLLM([reply("Подберу время. Сколько стоит — подскажу.")])
    service = make_service(session_factory, redis, llm)

    result = await service.handle(message(fake_salon, "Запишите на маникюр, мой телефон +7 916 150-22-39"))

    assert result.text is not None
    assert f"https://agent.example/privacy/{fake_salon.slug}" in result.text  # первый ответ — с политикой
    assert "150-22-39" not in (llm.calls[0][0][-1].content or "")  # в LLM телефон не ушёл
    stored = await session.scalar(
        select(Message.content).where(
            Message.conversation_id == result.conversation_id, Message.role == MessageRole.CLIENT
        )
    )
    assert stored is not None
    assert "[телефон скрыт]" in stored
    assert "916" not in stored


async def test_booking_needs_consent_then_works(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    llm = ScriptedLLM(
        [
            reply("Здравствуйте!"),
            call_tool("book", {"option": 1, "client_name": "Анна", "phone": "[телефон скрыт]"}),
            reply("Нужно ваше согласие."),
            reply("Спасибо! Напишите номер телефона."),
        ]
    )
    service = make_service(session_factory, redis, llm)

    await service.handle(message(fake_salon, "Здравствуйте"))
    refused = await service.handle(message(fake_salon, "Запишите на первый вариант, Анна, 89161502239"))
    granted = await service.handle(message(fake_salon, "Согласна"))

    # без согласия инструмент записи — заглушка: телефон не уходит в CRM, к ответу добавлена просьба
    assert refused.result is not None
    assert refused.result.tool_calls[0].result == {
        "status": "consent_required",
        "message": refused.result.tool_calls[0].result["message"],  # type: ignore[index]
    }
    assert refused.text is not None
    assert "/privacy/" in refused.text
    assert granted.text == "Спасибо! Напишите номер телефона."  # после согласия просьба больше не добавляется
    consents = (await session.scalars(select(Consent).where(Consent.salon_id == fake_salon.id))).all()
    assert [(c.granted, c.document_version) for c in consents] == [(True, CONSENT_VERSION)]


async def test_withdrawal_erases_client_data(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, fake_salon: Salon
) -> None:
    llm = ScriptedLLM(
        [
            reply("Спасибо!"),
            call_tool("find_service", {"query": "маникюр"}),
            reply("Маникюр — 1 800 ₽."),
            reply("Привет!"),
        ]
    )
    service = make_service(session_factory, redis, llm)
    await service.handle(message(fake_salon, "Согласна"))
    first = await service.handle(message(fake_salon, "Сколько стоит маникюр?"))
    client_id = await session.scalar(select(ClientIdentity.client_id).where(ClientIdentity.external_user_id == USER))
    await session.execute(update(Client).where(Client.id == client_id).values(name="Анна", phone="+79161502239"))
    await session.commit()

    withdrawn = await service.handle(message(fake_salon, "Отзываю согласие, удалите мои данные"))

    assert withdrawn.text == WITHDRAWN_ANSWER
    assert await session.get(Conversation, first.conversation_id, populate_existing=True) is None
    for model in (Message, ToolCall):
        count = await session.scalar(select(func.count()).select_from(model).where(model.salon_id == fake_salon.id))
        assert count == 0
    client = await session.get(Client, client_id, populate_existing=True)
    assert client is not None
    assert (client.name, client.phone) == (None, None)  # карточка обезличена
    assert await session.scalar(select(ClientIdentity).where(ClientIdentity.client_id == client_id)) is None
    journal = (
        await session.scalars(
            select(Consent.granted).where(Consent.client_id == client_id).order_by(Consent.created_at)
        )
    ).all()
    assert journal == [True, False]  # доказательство согласия и его отзыва остаётся
    assert await StateStore(redis).redis.keys(f"*{first.conversation_id}*") == []

    again = await service.handle(message(fake_salon, "Привет"))  # пишет снова — это новый клиент без согласия
    assert again.text is not None
    assert "/privacy/" in again.text


async def test_withdrawal_does_not_touch_other_salon(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    other_salon: Salon,
) -> None:
    other = await with_fake_crm(session, other_salon)
    service = make_service(session_factory, redis, ScriptedLLM([reply("А"), reply("Б")]))
    in_other = await service.handle(message(other, "Привет"))  # тот же человек пишет второму салону
    await service.handle(message(fake_salon, "Привет"))

    await service.handle(message(fake_salon, "/forget"))

    assert await session.get(Conversation, in_other.conversation_id, populate_existing=True) is not None
    other_identity = await session.scalar(select(ClientIdentity).where(ClientIdentity.salon_id == other.id))
    assert other_identity is not None


async def test_retention_purges_old_dialogs_per_salon(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    other_salon: Salon,
) -> None:
    other = await with_fake_crm(session, other_salon)
    other.settings = {**other.settings, "retention_days": 730}
    await session.commit()
    service = make_service(session_factory, redis, ScriptedLLM([reply("1"), reply("2"), reply("3")]))
    old = await service.handle(message(fake_salon, "Привет", user="old-client-0001"))
    fresh = await service.handle(message(fake_salon, "Привет", user="new-client-0001"))
    kept_longer = await service.handle(message(other, "Привет", user="old-client-0001"))
    long_ago = datetime.now(UTC) - timedelta(days=400)  # больше 365 дней по умолчанию, меньше 730
    await session.execute(
        update(Conversation)
        .where(Conversation.id.in_([old.conversation_id, kept_longer.conversation_id]))
        .values(last_message_at=long_ago)
    )
    await session.commit()

    report = await purge_expired(session_factory)

    assert report == {fake_salon.id: 1}
    assert await session.get(Conversation, old.conversation_id, populate_existing=True) is None
    assert await session.get(Conversation, fresh.conversation_id, populate_existing=True) is not None
    assert await session.get(Conversation, kept_longer.conversation_id, populate_existing=True) is not None


async def test_privacy_page(client: AsyncClient, session: AsyncSession, salon: Salon) -> None:
    salon.settings = {
        "retention_days": 180,
        "privacy": {
            "operator_name": "ИП Иванова <script>",
            "inn": "500100732259",
            "address": "Москва",
            "email": "privacy@lavanda.example",
        },
    }
    await session.commit()

    page = await client.get(f"/privacy/{salon.slug}")
    missing = await client.get("/privacy/no-such-salon")

    assert page.status_code == 200
    assert "ИП Иванова &lt;script&gt;" in page.text  # автоэкранирование Jinja2
    assert "500100732259" in page.text
    assert "180 дн." in page.text
    assert CONSENT_VERSION in page.text
    assert page.headers["X-Robots-Tag"] == "noindex"
    assert missing.status_code == 404
