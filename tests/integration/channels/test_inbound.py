"""Мессенджеры на настоящих PostgreSQL и Redis: вебхуки, склейка, сервис диалога и outbox."""

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.channels.base import ChannelRejectedError, ChannelUnavailableError, Delivery
from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.llm.fake import ScriptedLLM, reply
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.debounce import Debouncer
from beauty_agent.conversation.inbound import UNSUPPORTED_ANSWER, InboundGate, InboundProcessor
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind
from beauty_agent.conversation.outbox import DeliveryStatus, Outbox
from beauty_agent.conversation.service import ConversationService
from beauty_agent.core.config import SecuritySettings, YclientsSettings
from beauty_agent.core.security import Security
from beauty_agent.db.models import ChannelAccount, OutboundMessage, Salon
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.enums import Channel, CrmKind, OutboxStatus

TOKEN = "123456789:AAH-valid_token_for_tests_000000000000"
SECURITY = Security(SecuritySettings(), env="test")


class RecordingQueue:
    def __init__(self) -> None:
        self.scheduled: list[tuple[uuid.UUID, str, str]] = []

    async def schedule(self, account_id: uuid.UUID, chat_id: str, token: str, *, delay_s: float) -> None:
        self.scheduled.append((account_id, chat_id, token))


class RecordingSender:
    def __init__(self, *failures: Exception) -> None:
        self.sent: list[Delivery] = []
        self.failures = list(failures)

    async def send(self, credentials: dict[str, Any], delivery: Delivery) -> str | None:
        assert credentials == {"token": TOKEN}  # учётные данные расшифрованы из БД
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(delivery)
        return f"msg-{len(self.sent)}"


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[AdminEvent] = []

    async def notify(self, event: AdminEvent) -> None:
        self.events.append(event)


def telegram_update(update_id: int, text: str | None, *, chat_id: int = 555) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "from": {"id": chat_id, "is_bot": False, "first_name": "Анна"},
        "chat": {"id": chat_id, "type": "private"},
    }
    if text is None:
        message["voice"] = {"file_id": "v"}
    else:
        message["text"] = text
    return {"update_id": update_id, "message": message}


@pytest.fixture
async def fake_salon(session: AsyncSession, salon: Salon) -> Salon:
    salon.settings = {"crm_kind": CrmKind.FAKE.value}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    return salon


@pytest.fixture
async def account(session: AsyncSession, fake_salon: Salon) -> ChannelAccount:
    connected = await ChannelAccountsRepository(session).connect(
        fake_salon.id, Channel.TELEGRAM, f"bot-{uuid.uuid4().hex[:6]}", {"token": TOKEN}, SECURITY
    )
    await session.commit()
    return connected


def pipeline(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, llm: ScriptedLLM, sender: RecordingSender
) -> tuple[InboundGate, InboundProcessor, RecordingQueue, RecordingNotifier]:
    notifier = RecordingNotifier()
    debouncer = Debouncer(redis)
    conversations = ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=ProviderRegistry(YclientsSettings()),
        llm_clients={"scripted": llm},
        default_models=["scripted"],
        notifier=notifier,
    )
    outbox = Outbox(session_factory, SECURITY, {Channel.TELEGRAM: sender})
    queue = RecordingQueue()
    gate = InboundGate(redis, debouncer, queue)
    return gate, InboundProcessor(session_factory, debouncer, conversations, outbox, notifier), queue, notifier


async def test_three_quick_messages_give_one_answer(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, account: ChannelAccount
) -> None:
    from beauty_agent.adapters.channels.telegram import parse_update

    llm = ScriptedLLM([reply("Здравствуйте! Записать вас на маникюр в пятницу?")])
    sender = RecordingSender()
    gate, processor, queue, _ = pipeline(session_factory, redis, llm, sender)
    for number, text in enumerate(["Здравствуйте", "хочу на маникюр", "в пятницу"], start=1):
        update = parse_update(telegram_update(number, text))
        assert update is not None
        assert await gate.accept(account.id, update)

    for account_id, chat_id, token in queue.scheduled:  # три отложенные задачи — ответит только последняя
        await processor.process(account_id, chat_id, token)

    assert len(llm.calls) == 1
    user_message = llm.calls[0][0][-1].content
    assert user_message == "Здравствуйте\nхочу на маникюр\nв пятницу"
    assert [delivery.text for delivery in sender.sent] == ["Здравствуйте! Записать вас на маникюр в пятницу?"]
    assert sender.sent[0].chat_id == "555"


async def test_repeated_webhook_is_not_queued_twice(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, account: ChannelAccount
) -> None:
    from beauty_agent.adapters.channels.telegram import parse_update

    gate, _, queue, _ = pipeline(session_factory, redis, ScriptedLLM(), RecordingSender())
    update = parse_update(telegram_update(1, "Привет"))
    assert update is not None

    assert await gate.accept(account.id, update)
    assert not await gate.accept(account.id, update)  # Telegram повторил тот же update_id
    assert len(queue.scheduled) == 1


async def test_retried_task_does_not_send_twice(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, account: ChannelAccount
) -> None:
    from beauty_agent.adapters.channels.telegram import parse_update

    llm = ScriptedLLM([reply("Ответ")])
    sender = RecordingSender(ChannelUnavailableError("Telegram: 502"))  # первая отправка падает
    gate, processor, queue, _ = pipeline(session_factory, redis, llm, sender)
    update = parse_update(telegram_update(1, "Привет"))
    assert update is not None
    await gate.accept(account.id, update)
    account_id, chat_id, token = queue.scheduled[0]

    with pytest.raises(ChannelUnavailableError):
        await processor.process(account_id, chat_id, token)  # агент ответил, отправка упала
    await processor.process(account_id, chat_id, token)  # Celery повторил задачу
    await processor.process(account_id, chat_id, token)  # и ещё раз — «хотя бы один раз»

    assert len(llm.calls) == 1  # агент не отвечал заново: ответ уже сохранён
    assert [delivery.text for delivery in sender.sent] == ["Ответ"]
    row = await session.scalar(select(OutboundMessage).where(OutboundMessage.channel_account_id == account.id))
    assert row is not None
    await session.refresh(row)
    assert row.status is OutboxStatus.SENT
    assert row.attempts == 2
    assert row.external_id == "msg-1"


async def test_voice_gets_polite_answer_and_admin_alert(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, account: ChannelAccount
) -> None:
    from beauty_agent.adapters.channels.telegram import parse_update

    llm = ScriptedLLM()
    sender = RecordingSender()
    gate, processor, queue, notifier = pipeline(session_factory, redis, llm, sender)
    update = parse_update(telegram_update(1, None))
    assert update is not None
    await gate.accept(account.id, update)

    await processor.process(*queue.scheduled[0])

    assert llm.calls == []
    assert [delivery.text for delivery in sender.sent] == [UNSUPPORTED_ANSWER]
    assert [event.kind for event in notifier.events] == [AdminEventKind.UNSUPPORTED_MESSAGE]


async def test_blocked_bot_marks_message_failed(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, account: ChannelAccount
) -> None:
    from beauty_agent.adapters.channels.telegram import parse_update

    sender = RecordingSender(ChannelRejectedError("bot was blocked by the user"))
    gate, processor, queue, _ = pipeline(session_factory, redis, ScriptedLLM([reply("Ответ")]), sender)
    update = parse_update(telegram_update(1, "Привет"))
    assert update is not None
    await gate.accept(account.id, update)

    await processor.process(*queue.scheduled[0])  # окончательный отказ — без исключения и повторов

    row = await session.scalar(select(OutboundMessage).where(OutboundMessage.channel_account_id == account.id))
    assert row is not None
    await session.refresh(row)
    assert row.status is OutboxStatus.FAILED
    assert await processor.outbox.deliver(account.salon_id, row.dedupe_key) is DeliveryStatus.ALREADY


# ---------------------------------------------------------------- вебхуки


@pytest.fixture
def queue(app: FastAPI) -> RecordingQueue:
    recording = RecordingQueue()
    app.state.inbound.queue = recording
    return recording


async def test_telegram_webhook(
    app: FastAPI, client: AsyncClient, account: ChannelAccount, queue: RecordingQueue
) -> None:
    url = f"/api/v1/webhooks/telegram/{account.id}"
    secret = app.state.security.webhook_secret("telegram", account.id)

    forged = await client.post(url, json=telegram_update(1, "Привет"), headers={"X-Telegram-Bot-Api-Secret-Token": "x"})
    first = await client.post(
        url, json=telegram_update(1, "Привет"), headers={"X-Telegram-Bot-Api-Secret-Token": secret}
    )
    repeat = await client.post(
        url, json=telegram_update(1, "Привет"), headers={"X-Telegram-Bot-Api-Secret-Token": secret}
    )
    group = telegram_update(2, "Привет")
    group["message"]["chat"]["type"] = "group"
    ignored = await client.post(url, json=group, headers={"X-Telegram-Bot-Api-Secret-Token": secret})

    assert forged.status_code == 401
    assert (first.status_code, repeat.status_code, ignored.status_code) == (200, 200, 200)
    assert len(queue.scheduled) == 1


async def test_webhook_for_disabled_channel_is_acknowledged(
    app: FastAPI, client: AsyncClient, session: AsyncSession, account: ChannelAccount, queue: RecordingQueue
) -> None:
    account.is_active = False
    await session.commit()
    secret = app.state.security.webhook_secret("telegram", account.id)

    response = await client.post(
        f"/api/v1/webhooks/telegram/{account.id}",
        json=telegram_update(1, "Привет"),
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
    )

    assert response.status_code == 200  # иначе Telegram будет повторять часами
    assert queue.scheduled == []


async def test_webhook_body_limit(app: FastAPI, client: AsyncClient, account: ChannelAccount) -> None:
    secret = app.state.security.webhook_secret("telegram", account.id)

    response = await client.post(
        f"/api/v1/webhooks/telegram/{account.id}",
        content=b'{"x": "' + b"a" * 300_000 + b'"}',
        headers={"X-Telegram-Bot-Api-Secret-Token": secret, "content-type": "application/json"},
    )

    assert response.status_code == 413


async def test_vk_webhook(
    app: FastAPI, client: AsyncClient, session: AsyncSession, fake_salon: Salon, queue: RecordingQueue
) -> None:
    security: Security = app.state.security
    vk_account = await ChannelAccountsRepository(session).connect(
        fake_salon.id, Channel.VK, "club-1", {"token": "vk-token", "confirmation": "a1b2c3"}, security
    )
    await session.commit()
    url = f"/api/v1/webhooks/vk/{vk_account.id}"
    secret = security.webhook_secret("vk", vk_account.id)[:48]
    message = {
        "type": "message_new",
        "event_id": "e1",
        "secret": secret,
        "object": {"message": {"id": 5, "peer_id": 321, "from_id": 321, "text": "Привет"}},
    }

    confirmation = await client.post(url, json={"type": "confirmation", "group_id": 1, "secret": secret})
    accepted = await client.post(url, json=message)
    forged = await client.post(url, json={**message, "secret": "wrong"})

    assert confirmation.text == "a1b2c3"
    assert accepted.text == "ok"
    assert forged.status_code == 401
    assert len(queue.scheduled) == 1


async def test_channel_belongs_to_one_salon(session: AsyncSession, salon: Salon, other_salon: Salon) -> None:
    repo = ChannelAccountsRepository(session)
    await repo.connect(salon.id, Channel.TELEGRAM, "bot-shared", {"token": TOKEN}, SECURITY)

    with pytest.raises(ValueError, match="другому салону"):
        await repo.connect(other_salon.id, Channel.TELEGRAM, "bot-shared", {"token": TOKEN}, SECURITY)
