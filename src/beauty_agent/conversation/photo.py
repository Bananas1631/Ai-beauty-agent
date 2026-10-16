"""Фото-референс клиента («хочу вот так»): разбор в фоновой задаче (workers/photo.py).

Пока задача идёт, клиент уже получил «Смотрю фото…» (conversation/inbound.py). Здесь:

1. Скачать фото из мессенджера: Telegram — getFile по file_id, VK и MAX — по https-ссылке через
   клиент, который не ходит во внутреннюю сеть. Не больше 10 МБ.
2. Проверить формат по содержимому, повернуть, уменьшить до 1024 px и перекодировать без EXIF
   (media/images.py): координаты съёмки и модель телефона никуда не уходят.
3. Мультимодальная модель описывает фото по схеме (adapters/llm/vision.py).
4. Не салонное фото или модель не уверена — диалог администратору с уведомлением. Иначе сообщение
   клиента с описанием фото идёт агенту; услугу и цену агент берёт инструментом photo_services
   из каталога, а не из фантазии модели.

Фото живёт только в памяти задачи — ни на диске, ни в БД, ни в логах. Повтор задачи безопасен:
сообщение с тем же ID (photo:<id сообщения>) второй ответ не создаст.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.channels.base import ChannelUnavailableError, ChannelUpdate
from beauty_agent.adapters.channels.telegram import TelegramBotApi
from beauty_agent.adapters.llm.vision import PhotoDescription, VisionClient
from beauty_agent.conversation.outbox import Outbox
from beauty_agent.conversation.service import ConversationService, IncomingMessage, ReplyRoute
from beauty_agent.conversation.shadow_mode import ShadowMode
from beauty_agent.core import metrics
from beauty_agent.core.security import Security
from beauty_agent.db.models import ChannelAccount, Salon
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.enums import Channel
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError
from beauty_agent.media.images import BadPhotoError, download, prepare_image

logger = structlog.get_logger(__name__)

MIN_CONFIDENCE = 0.6
PHOTO_MARKER = "[Фото]"
RETRYABLE = (LLMUnavailableError, LLMBadResponseError, ChannelUnavailableError, httpx.HTTPError)


class PhotoOutcome(StrEnum):
    ANSWERED = "answered"  # агент подобрал услугу
    TO_ADMIN = "to_admin"  # передано администратору
    SKIPPED = "skipped"  # канал отключён, пока фото ждало в очереди


def handoff_reason(description: PhotoDescription) -> str | None:
    """Почему фото не стоит угадывать; None — можно подбирать услугу."""
    if not description.is_beauty_reference or description.category == "other":
        return "На фото не услуга салона — посмотрите и ответьте клиенту"
    if description.confidence < MIN_CONFIDENCE:
        return f"Модель не уверена, что это «{description.service}» — посмотрите фото и ответьте клиенту"
    return None


@dataclass
class PhotoProcessor:
    session_factory: async_sessionmaker[AsyncSession]
    security: Security
    conversations: ConversationService
    outbox: Outbox
    telegram: TelegramBotApi
    vision: VisionClient | None
    http: httpx.AsyncClient  # make_safe_client: ссылки VK и MAX не уведут во внутреннюю сеть
    shadow: ShadowMode | None = None

    async def _download(self, account: ChannelAccount, update: ChannelUpdate) -> bytes:
        if not update.photo:
            raise BadPhotoError("В сообщении нет фото")
        if update.channel in (Channel.TELEGRAM, Channel.TELEGRAM_BUSINESS):
            token = str(self.security.decrypt(account.token_encrypted).get("token", ""))
            return await self.telegram.download_file(token, update.photo)
        return await download(self.http, update.photo)

    async def process(
        self, account_id: uuid.UUID, update: ChannelUpdate, text: str, *, final_attempt: bool = False
    ) -> PhotoOutcome:
        async with self.session_factory() as session:
            found = await ChannelAccountsRepository(session).get_with_salon(account_id)
        if found is None:
            return PhotoOutcome.SKIPPED
        account, salon = found
        reason: str | None
        description: PhotoDescription | None = None
        try:
            if self.vision is None:
                raise BadPhotoError("Модель для фото не настроена")
            jpeg = prepare_image(await self._download(account, update))
            result = await self.vision.describe(jpeg)
            del jpeg  # фото не переживает задачу
            metrics.llm_cost(str(salon.id), result.cost_rub)
            description = result.description
            reason = handoff_reason(description)
        except BadPhotoError as exc:
            reason = f"Фото не разобрано ({exc.message}) — посмотрите в чате"
        except RETRYABLE as exc:
            if not final_attempt:
                raise  # повтор задачи с паузой: модель или мессенджер временно недоступны
            reason = f"Фото не разобрано ({type(exc).__name__}) — посмотрите в чате"
        logger.info(
            "photo_processed",
            salon_id=str(salon.id),
            category=description.category if description else None,
            confidence=description.confidence if description else None,
            to_admin=reason is not None,
        )
        await self._reply(salon, account, update, text, description, reason)
        return PhotoOutcome.TO_ADMIN if reason is not None else PhotoOutcome.ANSWERED

    async def _reply(
        self,
        salon: Salon,
        account: ChannelAccount,
        update: ChannelUpdate,
        text: str,
        description: PhotoDescription | None,
        reason: str | None,
    ) -> None:
        marker = description.as_text() if description is not None and reason is None else PHOTO_MARKER
        reply = await self.conversations.handle(
            IncomingMessage(
                salon=salon,
                channel=update.channel,
                external_user_id=update.user_id,
                text=f"{text}\n{marker}".strip(),
                external_message_id=f"photo:{update.message_id}",
                client_name=update.user_name,
                reply_to=ReplyRoute(account.id, update.chat_id, dict(update.route)),
                photo=description if reason is None else None,
                photo_handoff_reason=reason,
            )
        )
        if reply.draft_id is not None and self.shadow is not None:
            await self.shadow.publish(reply.draft_id)
        if reply.outbox_key:
            await self.outbox.deliver(salon.id, reply.outbox_key)
