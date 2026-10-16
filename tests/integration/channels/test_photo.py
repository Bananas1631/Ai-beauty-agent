"""Фото-референс от начала до конца: режимы салона, разбор в воркере, передача администратору."""

import io
import uuid
from typing import Any

import httpx
import pytest
from PIL import Image
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import SECURITY, TOKEN, RecordingNotifier, RecordingSender
from tests.integration.channels.test_inbound import pipeline
from tests.photo_helpers import GPS_IFD, french_manicure, jpeg_with_metadata

from beauty_agent.adapters.channels.telegram import TelegramBotApi, parse_update
from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.llm.fake import ScriptedLLM
from beauty_agent.adapters.llm.offline import OfflineLLM
from beauty_agent.adapters.llm.vision import ScriptedVision, VisionClient
from beauty_agent.conversation.inbound import PHOTO_ACK_ANSWER
from beauty_agent.conversation.notifications import AdminEventKind
from beauty_agent.conversation.outbox import Outbox
from beauty_agent.conversation.photo import PhotoOutcome, PhotoProcessor
from beauty_agent.conversation.service import PHOTO_HANDOFF_ANSWER, ConversationService
from beauty_agent.core.config import YclientsSettings
from beauty_agent.db.models import ChannelAccount, Conversation, Message, Salon
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole
from beauty_agent.domain.errors import LLMUnavailableError
from beauty_agent.rag.safe_http import make_safe_client


def photo_update(update_id: int = 1, caption: str | None = "Хочу вот так") -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "from": {"id": 555, "is_bot": False, "first_name": "Анна"},
        "chat": {"id": 555, "type": "private"},
        "photo": [{"file_id": f"photo-{update_id}", "width": 1280, "height": 960, "file_size": 150_000}],
    }
    if caption:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def telegram_files(photo: bytes) -> TelegramBotApi:
    """Bot API на mock-транспорте: getFile и сам файл — как отдаёт api.telegram.org."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            result = {"file_id": "x", "file_size": len(photo), "file_path": "photos/file_1.jpg"}
            return httpx.Response(200, json={"ok": True, "result": result})
        if request.url.path == f"/file/bot{TOKEN}/photos/file_1.jpg":
            return httpx.Response(200, content=photo)
        return httpx.Response(404)

    return TelegramBotApi(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def set_photo_mode(session: AsyncSession, salon: Salon, mode: str) -> None:
    salon.settings = {**salon.settings, "photo_mode": mode}
    await session.commit()


def photo_processor(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    vision: VisionClient | None,
    sender: RecordingSender,
    notifier: RecordingNotifier,
    *,
    photo: bytes | None = None,
) -> PhotoProcessor:
    conversations = ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=ProviderRegistry(YclientsSettings()),
        llm_clients={"offline": OfflineLLM()},
        default_models=["offline"],
        notifier=notifier,
    )
    return PhotoProcessor(
        session_factory=session_factory,
        security=SECURITY,
        conversations=conversations,
        outbox=Outbox(session_factory, SECURITY, {Channel.TELEGRAM: sender}),
        telegram=telegram_files(photo if photo is not None else jpeg_with_metadata()),
        vision=vision,
        http=make_safe_client(),
    )


async def conversation_of(session: AsyncSession, salon: Salon) -> Conversation:
    found = await session.scalar(
        select(Conversation).where(Conversation.salon_id == salon.id).execution_options(populate_existing=True)
    )
    assert found is not None
    return found


async def test_admin_mode_hands_photo_to_admin(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    account: ChannelAccount,
) -> None:
    llm = ScriptedLLM()  # модель не должна вызываться
    sender = RecordingSender()
    gate, processor, queue, notifier = pipeline(session_factory, redis, llm, sender)
    update = parse_update(photo_update())
    assert update is not None

    await gate.accept(account.id, update)
    for account_id, chat_id, token in queue.scheduled:
        await processor.process(account_id, chat_id, token)
    conversation = await conversation_of(session, fake_salon)
    stored = await session.scalar(select(Message.content).where(Message.role == MessageRole.CLIENT))

    assert llm.calls == []
    assert [delivery.text.split("\n\n")[0] for delivery in sender.sent] == [PHOTO_HANDOFF_ANSWER]
    assert [event.kind for event in notifier.events] == [AdminEventKind.PHOTO_REFERENCE]
    assert notifier.events[0].conversation_id == str(conversation.id)  # администратор ответит из бота
    assert conversation.status is ConversationStatus.HANDED_OFF
    assert stored == "Хочу вот так\n[Фото]"  # в истории — подпись и отметка, самого фото нет


async def test_guess_mode_acknowledges_and_queues(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    account: ChannelAccount,
) -> None:
    await set_photo_mode(session, fake_salon, "guess")
    llm = ScriptedLLM()
    sender = RecordingSender()
    gate, processor, queue, _ = pipeline(session_factory, redis, llm, sender)
    processor.queue = queue
    update = parse_update(photo_update())
    assert update is not None

    await gate.accept(account.id, update)
    for account_id, chat_id, token in queue.scheduled:
        await processor.process(account_id, chat_id, token)

    assert [delivery.text for delivery in sender.sent] == [PHOTO_ACK_ANSWER]
    assert len(queue.photos) == 1
    queued_account, queued_update, text = queue.photos[0]
    assert (queued_account, queued_update["photo"], text) == (account.id, "photo-1", "Хочу вот так")
    assert llm.calls == []


async def test_worker_matches_catalog_service(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    account: ChannelAccount,
) -> None:
    await set_photo_mode(session, fake_salon, "guess")
    vision = ScriptedVision(french_manicure())
    sender, notifier = RecordingSender(), RecordingNotifier()
    worker = photo_processor(session_factory, redis, vision, sender, notifier)
    update = parse_update(photo_update())
    assert update is not None

    outcome = await worker.process(account.id, update, "Хочу вот так")
    seen = Image.open(io.BytesIO(vision.images[0]))

    assert outcome is PhotoOutcome.ANSWERED
    assert len(sender.sent) == 1
    answer = sender.sent[0].text
    assert "Маникюр с покрытием гель-лак" in answer
    assert "2 500" in answer or "2500" in answer  # цена — из каталога салона
    assert notifier.events == []
    assert max(seen.size) <= 1024
    assert not seen.getexif().get_ifd(GPS_IFD)  # координаты съёмки модели не ушли


async def test_unsure_model_hands_off(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    account: ChannelAccount,
) -> None:
    await set_photo_mode(session, fake_salon, "guess")
    sender, notifier = RecordingSender(), RecordingNotifier()
    worker = photo_processor(session_factory, redis, ScriptedVision(french_manicure(confidence=0.3)), sender, notifier)
    update = parse_update(photo_update())
    assert update is not None

    outcome = await worker.process(account.id, update, "")

    assert outcome is PhotoOutcome.TO_ADMIN
    assert sender.sent[0].text.startswith(PHOTO_HANDOFF_ANSWER)
    assert [event.kind for event in notifier.events] == [AdminEventKind.PHOTO_REFERENCE]
    assert "не уверена" in notifier.events[0].text


@pytest.mark.parametrize("final_attempt", [False, True])
async def test_vision_outage_retries_then_hands_off(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    account: ChannelAccount,
    final_attempt: bool,
) -> None:
    sender, notifier = RecordingSender(), RecordingNotifier()
    worker = photo_processor(session_factory, redis, ScriptedVision(LLMUnavailableError("vl down")), sender, notifier)
    update = parse_update(photo_update())
    assert update is not None

    if not final_attempt:
        with pytest.raises(LLMUnavailableError):  # Celery повторит задачу
            await worker.process(account.id, update, "")
        assert sender.sent == []
    else:
        assert await worker.process(account.id, update, "", final_attempt=True) is PhotoOutcome.TO_ADMIN
        assert sender.sent[0].text.startswith(PHOTO_HANDOFF_ANSWER)


async def test_broken_file_goes_to_admin_without_retry(
    session_factory: async_sessionmaker[AsyncSession],
    redis: Redis,
    fake_salon: Salon,
    account: ChannelAccount,
) -> None:
    sender, notifier = RecordingSender(), RecordingNotifier()
    vision = ScriptedVision(french_manicure())
    worker = photo_processor(session_factory, redis, vision, sender, notifier, photo=b"<html>not a photo</html>")
    update = parse_update(photo_update())
    assert update is not None

    assert await worker.process(account.id, update, "") is PhotoOutcome.TO_ADMIN
    assert vision.images == []  # до модели не дошло
    assert "не разобрано" in notifier.events[0].text


async def test_disconnected_channel_is_skipped(session_factory: async_sessionmaker[AsyncSession], redis: Redis) -> None:
    worker = photo_processor(session_factory, redis, ScriptedVision(), RecordingSender(), RecordingNotifier())
    update = parse_update(photo_update())
    assert update is not None

    assert await worker.process(uuid.uuid4(), update, "") is PhotoOutcome.SKIPPED
