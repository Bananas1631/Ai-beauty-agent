"""Админ-бот и теневой режим на настоящих PostgreSQL и Redis; Bot API Telegram — на моке."""

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import (
    ADMIN_TOKEN,
    SECURITY,
    TOKEN,
    FakeBotApi,
    RecordingQueue,
    RecordingSender,
    telegram_update,
)

from beauty_agent.adapters.channels.admin_bot import AdminBot, AdminBotApi, AdminBotNotifier, issue_bind_code
from beauty_agent.adapters.channels.telegram import parse_update
from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.llm.fake import ScriptedLLM, call_tool, reply
from beauty_agent.agent.state import StateStore
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.debounce import Debouncer
from beauty_agent.conversation.handoff import Handoff
from beauty_agent.conversation.inbound import InboundGate, InboundProcessor
from beauty_agent.conversation.outbox import Outbox
from beauty_agent.conversation.privacy import PrivacyService
from beauty_agent.conversation.service import ConversationService
from beauty_agent.conversation.shadow_mode import ShadowMode
from beauty_agent.core.config import YclientsSettings
from beauty_agent.db.models import ChannelAccount, Conversation, Draft, Message, Salon
from beauty_agent.db.repositories.admins import AdminsRepository
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.enums import AdminRole, Channel, ConversationStatus, CrmKind, DraftStatus, MessageRole

ADMIN = 42
STRANGER = 999


@dataclass
class Env:
    salon: Salon
    account: ChannelAccount
    bot_api: FakeBotApi
    sender: RecordingSender
    gate: InboundGate
    processor: InboundProcessor
    queue: RecordingQueue
    bot: AdminBot
    shadow: ShadowMode

    async def client_writes(self, update_id: int, text: str) -> None:
        update = parse_update(telegram_update(update_id, text))
        assert update is not None
        await self.gate.accept(self.account.id, update)
        await self.processor.process(*self.queue.scheduled[-1])

    async def admin_says(self, text: str, *, user: int = ADMIN, reply_to: int | None = None) -> None:
        message: dict[str, Any] = {
            "message_id": 5000 + len(self.bot_api.calls),
            "from": {"id": user, "is_bot": False, "first_name": "Ольга"},
            "chat": {"id": user, "type": "private"},
            "text": text,
        }
        if reply_to is not None:
            message["reply_to_message"] = {"message_id": reply_to}
        await self.bot.handle({"update_id": 1, "message": message})

    async def admin_presses(self, data: str, *, message_id: int, user: int = ADMIN) -> str:
        await self.bot.handle(
            {
                "update_id": 2,
                "callback_query": {
                    "id": f"cb-{len(self.bot_api.calls)}",
                    "from": {"id": user},
                    "message": {"message_id": message_id, "chat": {"id": user}},
                    "data": data,
                },
            }
        )
        return str(self.bot_api.sent("answerCallbackQuery")[-1]["text"])


async def make_env(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    salon: Salon,
    llm: ScriptedLLM,
    *,
    shadow: bool,
) -> Env:
    salon.settings = {"crm_kind": CrmKind.FAKE.value, "reply_mode": "shadow" if shadow else "autopilot"}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    account = await ChannelAccountsRepository(session).connect(
        salon.id, Channel.TELEGRAM, f"bot-{uuid.uuid4().hex[:6]}", {"token": TOKEN}, SECURITY
    )
    await AdminsRepository(session).bind(salon.id, str(ADMIN), str(ADMIN), "Ольга", AdminRole.OWNER)
    await session.commit()

    bot_api = FakeBotApi()
    admin_api = AdminBotApi(bot_api.api, ADMIN_TOKEN)
    notifier = AdminBotNotifier(admin_api, session_factory, redis)
    sender = RecordingSender()
    outbox = Outbox(session_factory, SECURITY, {Channel.TELEGRAM: sender})
    states = StateStore(redis)
    shadow_mode = ShadowMode(session_factory, outbox, states, notifier)
    handoff = Handoff(session_factory, outbox, states)
    conversations = ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=ProviderRegistry(YclientsSettings()),
        llm_clients={"scripted": llm},
        default_models=["scripted"],
        notifier=notifier,
        privacy=PrivacyService(redis, public_url="https://agent.example"),
    )
    debouncer = Debouncer(redis)
    queue = RecordingQueue()
    bot = AdminBot(
        api=admin_api,
        session_factory=session_factory,
        redis=redis,
        security=SECURITY,
        shadow=shadow_mode,
        handoff=handoff,
    )
    return Env(
        salon=salon,
        account=account,
        bot_api=bot_api,
        sender=sender,
        gate=InboundGate(redis, debouncer, queue),
        processor=InboundProcessor(session_factory, debouncer, conversations, outbox, notifier, shadow_mode),
        queue=queue,
        bot=bot,
        shadow=shadow_mode,
    )


@pytest.fixture
async def shadow_env(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, salon: Salon
) -> Env:
    llm = ScriptedLLM([reply("Маникюр делаем каждый день."), reply("Запишу вас на пятницу.")])
    return await make_env(session, session_factory, redis, salon, llm, shadow=True)


async def draft_of(session: AsyncSession, salon: Salon) -> Draft:
    draft = await session.scalar(
        select(Draft).where(Draft.salon_id == salon.id).order_by(Draft.created_at.desc()).limit(1)
    )
    assert draft is not None
    await session.refresh(draft)
    return draft


# ---------------------------------------------------------------- привязка


async def test_bind_by_one_time_code(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, shadow_env: Env
) -> None:
    code = await issue_bind_code(redis, shadow_env.salon.id, AdminRole.ADMIN)

    await shadow_env.admin_says(f"/start {code}", user=77)
    await shadow_env.admin_says(f"/start {code}", user=78)  # код одноразовый
    await shadow_env.admin_says("/start not-a-valid-code!!", user=79)

    texts = [payload["text"] for payload in shadow_env.bot_api.sent()]
    assert texts[0].startswith(f"Готово: вы администратор салона «{shadow_env.salon.name}»")
    assert texts[1].startswith("Код привязки не найден")
    assert texts[2].startswith("Код привязки не найден")
    admins = AdminsRepository(session)
    assert await admins.is_admin(shadow_env.salon.id, "77")
    assert not await admins.is_admin(shadow_env.salon.id, "78")


# ---------------------------------------------------------------- теневой режим


async def test_draft_goes_to_admin_not_to_client(session: AsyncSession, shadow_env: Env) -> None:
    await shadow_env.client_writes(1, "Сколько стоит маникюр?")

    assert shadow_env.sender.sent == []  # клиенту агент сам не отвечает
    [shown] = shadow_env.bot_api.sent()
    assert shown["chat_id"] == str(ADMIN)
    assert "Сколько стоит маникюр?" in shown["text"]
    assert "Маникюр делаем каждый день." in shown["text"]
    assert "parse_mode" not in shown  # текст клиента не исполнится как разметка
    buttons = [button["text"] for button in shown["reply_markup"]["inline_keyboard"][0]]
    assert buttons == ["Отправить", "Исправить", "Отвечу сам"]
    draft = await draft_of(session, shadow_env.salon)
    assert draft.status is DraftStatus.PENDING
    assert draft.notified_at is not None


async def test_send_button_and_repeated_press(session: AsyncSession, shadow_env: Env) -> None:
    await shadow_env.client_writes(1, "Сколько стоит маникюр?")
    draft = await draft_of(session, shadow_env.salon)
    draft_message = shadow_env.bot_api.last_message_id()

    first = await shadow_env.admin_presses(f"d:s:{draft.id}", message_id=draft_message)
    again = await shadow_env.admin_presses(f"d:s:{draft.id}", message_id=draft_message)

    assert first == "Отправлено клиенту"
    assert again == "Решение по черновику уже принято"
    assert [delivery.text for delivery in shadow_env.sender.sent] == [draft.text]  # ровно один раз
    assert shadow_env.bot_api.sent("editMessageReplyMarkup")[0]["message_id"] == draft_message  # кнопки убраны
    draft = await draft_of(session, shadow_env.salon)
    assert (draft.status, draft.decided_by) == (DraftStatus.SENT, str(ADMIN))
    roles = (await session.scalars(select(Message.role).where(Message.conversation_id == draft.conversation_id))).all()
    assert list(roles) == [MessageRole.CLIENT, MessageRole.AGENT]


async def test_edit_button_sends_corrected_text(session: AsyncSession, shadow_env: Env) -> None:
    await shadow_env.client_writes(1, "Сколько стоит маникюр?")
    draft = await draft_of(session, shadow_env.salon)

    answer = await shadow_env.admin_presses(f"d:e:{draft.id}", message_id=shadow_env.bot_api.last_message_id())
    prompt = shadow_env.bot_api.sent()[-1]
    corrected = draft.text.replace("каждый день", "ежедневно с 10 до 21")
    await shadow_env.admin_says(corrected, reply_to=shadow_env.bot_api.last_message_id())

    assert answer == "Жду исправленный текст"
    assert prompt["reply_markup"]["force_reply"] is True
    assert [delivery.text for delivery in shadow_env.sender.sent] == [corrected]
    draft = await draft_of(session, shadow_env.salon)
    assert draft.status is DraftStatus.EDITED
    assert draft.final_text == corrected
    assert draft.similarity is not None
    assert 90 <= draft.similarity < 100  # пара слов в длинном ответе — малая правка
    last = await session.scalar(
        select(Message).where(Message.conversation_id == draft.conversation_id).order_by(Message.created_at.desc())
    )
    assert last is not None
    assert (last.role, last.content) == (MessageRole.ADMIN, corrected)


async def test_manual_button_sends_nothing(session: AsyncSession, shadow_env: Env) -> None:
    await shadow_env.client_writes(1, "Сколько стоит маникюр?")
    draft = await draft_of(session, shadow_env.salon)

    answer = await shadow_env.admin_presses(f"d:m:{draft.id}", message_id=shadow_env.bot_api.last_message_id())

    assert answer == "Отмечено: отвечаете сами"
    assert shadow_env.sender.sent == []
    assert (await draft_of(session, shadow_env.salon)).status is DraftStatus.MANUAL


async def test_stranger_cannot_decide(session: AsyncSession, shadow_env: Env) -> None:
    await shadow_env.client_writes(1, "Сколько стоит маникюр?")
    draft = await draft_of(session, shadow_env.salon)

    answer = await shadow_env.admin_presses(f"d:s:{draft.id}", message_id=1, user=STRANGER)
    await shadow_env.admin_says("Подделка", user=STRANGER, reply_to=shadow_env.bot_api.last_message_id())

    assert answer == "Нет доступа"
    assert shadow_env.sender.sent == []
    assert (await draft_of(session, shadow_env.salon)).status is DraftStatus.PENDING


async def test_accuracy_report_and_token(session: AsyncSession, shadow_env: Env) -> None:
    await shadow_env.client_writes(1, "Сколько стоит маникюр?")
    await shadow_env.admin_presses(
        f"d:s:{(await draft_of(session, shadow_env.salon)).id}", message_id=shadow_env.bot_api.last_message_id()
    )
    await shadow_env.client_writes(2, "А в пятницу можно?")

    await shadow_env.admin_says("/report")
    report = shadow_env.bot_api.sent()[-1]["text"]
    await shadow_env.admin_says("/token")
    token_text = shadow_env.bot_api.sent()[-1]["text"]

    assert "черновики за 7 дн.: 2" in report
    assert "Отправлены без правок: 1" in report
    assert "Ждут решения: 1" in report
    assert "Точность: 100%" in report
    access = token_text.split("\n\n")[1]
    claims = SECURITY.verify_admin_token(access)
    assert (claims.user_id, claims.salon_id, claims.role) == (str(ADMIN), shadow_env.salon.id, AdminRole.OWNER)
    assert len(token_text.split("\n\n")[2]) >= 40  # refresh-токен


async def test_repeated_task_does_not_duplicate_draft(session: AsyncSession, shadow_env: Env) -> None:
    await shadow_env.client_writes(1, "Сколько стоит маникюр?")
    account_id, chat_id, token = shadow_env.queue.scheduled[-1]

    await shadow_env.processor.process(account_id, chat_id, token)  # Celery повторил задачу

    drafts = (await session.scalars(select(Draft).where(Draft.salon_id == shadow_env.salon.id))).all()
    assert len(drafts) == 1
    assert len(shadow_env.bot_api.sent()) == 1  # администратор видит черновик один раз


# ---------------------------------------------------------------- передача диалога


async def test_handoff_admin_reply_and_return(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, salon: Salon
) -> None:
    llm = ScriptedLLM([call_tool("handoff_to_admin", {"reason": "Клиент просит человека"}), reply("Снова я!")])
    env = await make_env(session, session_factory, redis, salon, llm, shadow=False)

    await env.client_writes(1, "Позовите администратора")
    handoff_message = env.bot_api.last_message_id()
    await env.client_writes(2, "Ау?")  # агент молчит, сообщение уходит администратору
    await env.admin_says("Здравствуйте, это Ольга. Чем помочь?", reply_to=handoff_message)

    notices = [payload["text"] for payload in env.bot_api.sent()[:2]]
    assert notices[0].startswith("Диалог передан вам")
    assert notices[1].startswith("Сообщение клиента")
    assert "Ау?" in notices[1]
    assert env.bot_api.sent()[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "Вернуть агенту"
    assert len(env.sender.sent) == 2  # ответ о передаче и ответ администратора; на «Ау?» агент промолчал
    assert env.sender.sent[1].text == "Здравствуйте, это Ольга. Чем помочь?"
    conversation = await session.scalar(select(Conversation).where(Conversation.salon_id == salon.id))
    assert conversation is not None

    returned = await env.admin_presses(f"h:r:{conversation.id}", message_id=handoff_message)
    repeated = await env.admin_presses(f"h:r:{conversation.id}", message_id=handoff_message)
    await env.client_writes(3, "Спасибо")

    assert (returned, repeated) == ("Агент снова отвечает", "Диалог уже у агента")
    await session.refresh(conversation)
    assert conversation.status is ConversationStatus.ACTIVE
    assert env.sender.sent[-1].text.startswith("Снова я!")
    roles = (
        await session.scalars(
            select(Message.role).where(Message.conversation_id == conversation.id).order_by(Message.created_at)
        )
    ).all()
    assert MessageRole.ADMIN in roles


async def test_invite_is_owner_only(session: AsyncSession, redis: Redis, shadow_env: Env) -> None:
    await AdminsRepository(session).bind(shadow_env.salon.id, "77", "77", "Вера", AdminRole.ADMIN)
    await session.commit()

    await shadow_env.admin_says("/invite")  # владелец
    invite = shadow_env.bot_api.sent()[-1]["text"]
    await shadow_env.admin_says("/invite", user=77)  # администратор
    refused = shadow_env.bot_api.sent()[-1]["text"]
    code = invite.rsplit(" ", 1)[-1]
    await shadow_env.admin_says(f"/start {code}", user=78)

    assert "Код для нового администратора" in invite
    assert refused.startswith("Приглашать администраторов может только владелец")
    assert await AdminsRepository(session).role_of(shadow_env.salon.id, "78") is AdminRole.ADMIN
