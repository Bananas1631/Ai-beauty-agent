"""Telegram-бот для администраторов салона — вместо веб-админки: он уже у всех в телефоне.

- Привязка: владелец получает в онбординге одноразовый код (ссылка t.me/<бот>?start=<код>),
  код действует 7 дней и сгорает после первого использования (GETDEL в Redis).
- Теневой режим: на каждое сообщение клиента — вопрос и черновик агента с кнопками «Отправить»,
  «Исправить», «Отвечу сам» (conversation/shadow_mode.py).
- Уведомления: передача диалога, сообщения клиента в диалоге у администратора, запись
  «подтвердить вручную», бюджет, голосовые. Ответ на уведомление о диалоге уходит клиенту.
- Команды: /report — отчёт точности черновиков за неделю, /token — пара токенов для API
  инбокса (access на 15 минут и refresh), /invite — код привязки администратора (только владелец).

Права проверяются на каждое действие: кнопка или ответ работают, только если нажавший — активный
администратор салона, которому принадлежит черновик или диалог. Текст клиента отправляется
без parse_mode: разметка из сообщения клиента не исполнится в чате администратора.
"""

import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.channels.telegram import TelegramBotApi
from beauty_agent.conversation.handoff import Handoff
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind
from beauty_agent.conversation.shadow_mode import Decision, DecisionStatus, DraftAction, ShadowMode
from beauty_agent.core.redis import key
from beauty_agent.core.security import RefreshTokens, Security
from beauty_agent.db.models import Conversation, Draft, Salon
from beauty_agent.db.repositories.admins import AdminsRepository
from beauty_agent.domain.enums import AdminRole

logger = structlog.get_logger(__name__)

ADMIN_BOT_ACCOUNT = uuid.UUID(int=0)  # «ID канала» для секрета вебхука админ-бота
BIND_TTL_S = 7 * 24 * 3600
REPLY_TTL_S = 7 * 24 * 3600
REPORT_DAYS = 7
MAX_PREVIEW = 1500
_CODE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_DRAFT_CALLBACK = re.compile(r"^d:([sem]):([0-9a-f-]{36})$")
_RETURN_CALLBACK = re.compile(r"^h:r:([0-9a-f-]{36})$")
_CONVERSATION_EVENTS = {
    AdminEventKind.HANDOFF,
    AdminEventKind.CLIENT_MESSAGE,
    AdminEventKind.BUDGET_EXCEEDED,
    AdminEventKind.PHOTO_REFERENCE,
}

HELP = (
    "Это бот администратора салона.\n"
    "Сюда приходят черновики ответов клиентам и уведомления.\n"
    "/report — точность черновиков за неделю\n"
    "/token — доступ к API инбокса\n"
    "/invite — код привязки для нового администратора (только владелец)"
)


async def issue_bind_code(redis: Redis, salon_id: uuid.UUID, role: AdminRole = AdminRole.OWNER) -> str:
    """Одноразовый код привязки к салону: 128 бит, 7 дней."""
    code = secrets.token_urlsafe(16)
    payload = json.dumps({"salon_id": str(salon_id), "role": role.value})
    await redis.set(key("admin", "bind", code), payload, ex=BIND_TTL_S)
    return code


def bind_link(username: str | None, code: str) -> str | None:
    return f"https://t.me/{username}?start={code}" if username else None


def _clip(text: str, limit: int = MAX_PREVIEW) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AdminBotApi:
    """Отправка в админ-бот: тот же клиент Bot API, что у ботов салонов, но с токеном сервиса."""

    def __init__(self, telegram: TelegramBotApi, token: str) -> None:
        self.telegram = telegram
        self.token = token

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        force_reply: bool = False,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _clip(text, 4000),
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
            }
        elif force_reply:
            payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": "Текст для клиента"}
        result = await self.telegram.call(self.token, "sendMessage", payload)
        return int(result["message_id"]) if isinstance(result, dict) and "message_id" in result else None

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await self.telegram.call(self.token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def clear_buttons(self, chat_id: str, message_id: int) -> None:
        await self.telegram.call(
            self.token,
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """На что администратор отвечает: черновик (исправление) или диалог (ответ клиенту)."""

    kind: str  # draft | conversation
    salon_id: uuid.UUID
    target_id: uuid.UUID


class ReplyMap:
    """Сообщение бота → черновик или диалог: ответ администратора на него знает, куда идти."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def remember(self, chat_id: str, message_id: int | None, target: ReplyTarget) -> None:
        if message_id is None:
            return
        payload = json.dumps({"kind": target.kind, "salon_id": str(target.salon_id), "id": str(target.target_id)})
        await self.redis.set(key("admin", "reply", chat_id, message_id), payload, ex=REPLY_TTL_S)

    async def recall(self, chat_id: str, message_id: int) -> ReplyTarget | None:
        raw = await self.redis.get(key("admin", "reply", chat_id, message_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return ReplyTarget(str(data["kind"]), uuid.UUID(data["salon_id"]), uuid.UUID(data["id"]))


class AdminBotNotifier:
    """Notifier и DraftPublisher: уведомления и черновики — всем активным администраторам салона."""

    def __init__(self, api: AdminBotApi, session_factory: async_sessionmaker[AsyncSession], redis: Redis) -> None:
        self.api = api
        self.session_factory = session_factory
        self.replies = ReplyMap(redis)

    async def _audience(self, salon_id: uuid.UUID) -> tuple[str, list[str]]:
        async with self.session_factory() as session:
            salon = await session.get(Salon, salon_id)
            chats = await AdminsRepository(session).chats_of_salon(salon_id)
        return (salon.name if salon else "Салон"), chats

    async def notify(self, event: AdminEvent) -> None:
        """Уведомление не должно ронять обработку сообщения клиента: ошибки — только в лог."""
        try:
            await self._notify(event)
        except Exception:
            logger.exception("admin_notification_failed", kind=event.kind.value, salon_id=event.salon_id)

    async def _notify(self, event: AdminEvent) -> None:
        salon_id = uuid.UUID(event.salon_id)
        salon_name, chats = await self._audience(salon_id)
        if not chats:
            logger.warning("admin_notification_no_admins", kind=event.kind.value, salon_id=event.salon_id)
            return
        titles = {
            AdminEventKind.HANDOFF: "Диалог передан вам",
            AdminEventKind.CLIENT_MESSAGE: "Сообщение клиента",
            AdminEventKind.BUDGET_EXCEEDED: "Лимит расходов на ИИ исчерпан",
            AdminEventKind.BOOKING_PENDING: "Подтвердите запись вручную",
            AdminEventKind.UNSUPPORTED_MESSAGE: "Голосовое или файл от клиента",
            AdminEventKind.PHOTO_REFERENCE: "Клиент прислал фото — посмотрите в чате",
            AdminEventKind.DEAD_LETTER: "Сообщение клиента не обработано",
            AdminEventKind.WEEKLY_REPORT: "Отчёт за неделю",
        }
        text = f"{titles.get(event.kind, 'Уведомление')} · {salon_name}\n{_clip(event.text)}"
        conversation_id = uuid.UUID(event.conversation_id) if event.conversation_id else None
        buttons = None
        if conversation_id and event.kind in _CONVERSATION_EVENTS:
            text += "\n\nОтветьте на это сообщение — текст уйдёт клиенту."
            buttons = [("Вернуть агенту", f"h:r:{conversation_id}")]
        for chat_id in chats:
            message_id = await self.api.send(chat_id, text, buttons=buttons)
            if conversation_id and event.kind in _CONVERSATION_EVENTS:
                await self.replies.remember(chat_id, message_id, ReplyTarget("conversation", salon_id, conversation_id))

    async def publish(self, draft: Draft) -> None:
        salon_name, chats = await self._audience(draft.salon_id)
        if not chats:
            logger.warning("draft_no_admins", salon_id=str(draft.salon_id))
            return
        text = f"Черновик · {salon_name}\n\nКлиент:\n{_clip(draft.question)}\n\nОтвет агента:\n{_clip(draft.text)}"
        buttons = [
            ("Отправить", f"d:s:{draft.id}"),
            ("Исправить", f"d:e:{draft.id}"),
            ("Отвечу сам", f"d:m:{draft.id}"),
        ]
        for chat_id in chats:
            message_id = await self.api.send(chat_id, text, buttons=buttons)
            # ответ на сам черновик тоже считается исправлением
            await self.replies.remember(chat_id, message_id, ReplyTarget("draft", draft.salon_id, draft.id))


class AdminBot:
    """Обработка обновлений админ-бота (в воркере, как и сообщения клиентов)."""

    def __init__(
        self,
        *,
        api: AdminBotApi,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        security: Security,
        shadow: ShadowMode,
        handoff: Handoff,
        username: str | None = None,
    ) -> None:
        self.api = api
        self.username = username
        self.session_factory = session_factory
        self.redis = redis
        self.security = security
        self.shadow = shadow
        self.handoff = handoff
        self.replies = ReplyMap(redis)

    async def handle(self, payload: dict[str, Any]) -> None:
        if isinstance(payload.get("callback_query"), dict):
            await self._on_callback(payload["callback_query"])
        elif isinstance(payload.get("message"), dict):
            await self._on_message(payload["message"])

    # ------------------------------------------------------------- права

    async def _is_admin(self, salon_id: uuid.UUID, user_id: str) -> bool:
        async with self.session_factory() as session:
            return await AdminsRepository(session).is_admin(salon_id, user_id)

    # ------------------------------------------------------------- сообщения

    async def _on_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot") or "id" not in sender:
            return
        chat_id, user_id = str(chat["id"]), str(sender["id"])
        text = str(message.get("text") or "").strip()
        replied = message.get("reply_to_message")
        if isinstance(replied, dict) and text and not text.startswith("/"):
            await self._on_reply(chat_id, user_id, int(replied.get("message_id", 0)), int(message["message_id"]), text)
        elif text.startswith("/start"):
            await self._bind(chat_id, user_id, sender.get("first_name"), text.removeprefix("/start").strip())
        elif text.startswith("/report"):
            await self._report(chat_id, user_id)
        elif text.startswith("/token"):
            await self._token(chat_id, user_id)
        elif text.startswith("/invite"):
            await self._invite(chat_id, user_id)
        else:
            await self.api.send(chat_id, HELP)

    async def _bind(self, chat_id: str, user_id: str, name: str | None, code: str) -> None:
        raw = await self.redis.getdel(key("admin", "bind", code)) if _CODE.fullmatch(code) else None
        if raw is None:
            await self.api.send(
                chat_id, "Код привязки не найден или уже использован. Попросите новый у владельца салона."
            )
            return
        data = json.loads(raw)
        salon_id = uuid.UUID(data["salon_id"])
        async with self.session_factory() as session:
            salon = await session.get(Salon, salon_id)
            if salon is None:
                return
            await AdminsRepository(session).bind(salon_id, user_id, chat_id, name, AdminRole(data["role"]))
            await session.commit()
            salon_name = salon.name
        logger.info("admin_bound", salon_id=str(salon_id))
        await self.api.send(chat_id, f"Готово: вы администратор салона «{salon_name}».\n\n{HELP}")

    async def _salons(self, user_id: str) -> list[Salon]:
        async with self.session_factory() as session:
            return list(await AdminsRepository(session).salons_of(user_id))

    async def _report(self, chat_id: str, user_id: str) -> None:
        salons = await self._salons(user_id)
        if not salons:
            await self.api.send(chat_id, "Вы не привязаны ни к одному салону.")
            return
        since = datetime.now(UTC) - timedelta(days=REPORT_DAYS)
        for salon in salons:
            report = await self.shadow.report(salon.id, since)
            await self.api.send(chat_id, report.text(salon.name, REPORT_DAYS))

    async def _memberships(self, user_id: str) -> list[tuple[Salon, AdminRole]]:
        async with self.session_factory() as session:
            return await AdminsRepository(session).memberships(user_id)

    async def _token(self, chat_id: str, user_id: str) -> None:
        memberships = await self._memberships(user_id)
        if not memberships:
            await self.api.send(chat_id, "Вы не привязаны ни к одному салону.")
            return
        refresh = RefreshTokens(self.redis, ttl=self.security.refresh_ttl)
        minutes = int(self.security.jwt_ttl.total_seconds() // 60)
        days = int(self.security.refresh_ttl.total_seconds() // 86400)
        for salon, role in memberships:
            access = self.security.issue_admin_token(user_id=user_id, salon_id=salon.id, role=role)
            refresh_token = await refresh.issue(user_id, salon.id)
            await self.api.send(
                chat_id,
                f"Доступ к API инбокса «{salon.name}». Никому не пересылайте.\n"
                f"Access-токен на {minutes} мин и refresh-токен на {days} дн. "
                "(обновление — POST /api/v1/admin/token/refresh):\n\n"
                f"{access}\n\n{refresh_token}",
            )

    async def _invite(self, chat_id: str, user_id: str) -> None:
        owned = [salon for salon, role in await self._memberships(user_id) if role is AdminRole.OWNER]
        if not owned:
            await self.api.send(chat_id, "Приглашать администраторов может только владелец салона.")
            return
        for salon in owned:
            code = await issue_bind_code(self.redis, salon.id, AdminRole.ADMIN)
            link = bind_link(self.username, code)
            await self.api.send(
                chat_id,
                f"Код для нового администратора «{salon.name}» (одноразовый, 7 дней):\n{link or '/start ' + code}",
            )

    async def _on_reply(self, chat_id: str, user_id: str, replied_id: int, message_id: int, text: str) -> None:
        target = await self.replies.recall(chat_id, replied_id)
        if target is None:
            await self.api.send(chat_id, "Не понял, к какому диалогу этот ответ. Ответьте на черновик или уведомление.")
            return
        if not await self._is_admin(target.salon_id, user_id):
            return
        if target.kind == "draft":
            decision = await self.shadow.decide(target.salon_id, target.target_id, DraftAction.EDIT, user_id, text)
            await self.api.send(chat_id, self._decision_text(decision, DraftAction.EDIT))
            return
        sent = await self.handoff.reply(
            target.salon_id, target.target_id, text, dedupe_key=f"admin:{chat_id}:{message_id}"
        )
        await self.api.send(
            chat_id, "Отправлено клиенту." if sent else "Диалог удалён или клиент писал не из мессенджера."
        )

    # ------------------------------------------------------------- кнопки

    async def _on_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        user_id = str((callback.get("from") or {}).get("id", ""))
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        message_id = message.get("message_id")
        data = str(callback.get("data") or "")
        if match := _DRAFT_CALLBACK.fullmatch(data):
            answer = await self._draft_button(chat_id, user_id, DraftAction(match[1]), uuid.UUID(match[2]))
        elif match := _RETURN_CALLBACK.fullmatch(data):
            answer = await self._return_button(user_id, uuid.UUID(match[1]))
        else:
            answer = "Кнопка устарела"
        await self.api.answer_callback(callback_id, answer)
        if answer in {"Отправлено клиенту", "Отмечено: отвечаете сами", "Агент снова отвечает"} and message_id:
            await self.api.clear_buttons(chat_id, int(message_id))

    async def _draft_button(self, chat_id: str, user_id: str, action: DraftAction, draft_id: uuid.UUID) -> str:
        async with self.session_factory() as session:
            draft = await session.get(Draft, draft_id)
            salon_id = draft.salon_id if draft else None
        if salon_id is None or not await self._is_admin(salon_id, user_id):
            return "Нет доступа"
        if action is DraftAction.EDIT:
            prompt_id = await self.api.send(
                chat_id, "Пришлите исправленный текст ответом на это сообщение — он уйдёт клиенту.", force_reply=True
            )
            await self.replies.remember(chat_id, prompt_id, ReplyTarget("draft", salon_id, draft_id))
            return "Жду исправленный текст"
        return self._decision_text(await self.shadow.decide(salon_id, draft_id, action, user_id), action)

    async def _return_button(self, user_id: str, conversation_id: uuid.UUID) -> str:
        async with self.session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            salon_id = conversation.salon_id if conversation else None
        if salon_id is None or not await self._is_admin(salon_id, user_id):
            return "Нет доступа"
        returned = await self.handoff.return_to_agent(salon_id, conversation_id)
        return "Агент снова отвечает" if returned else "Диалог уже у агента"

    @staticmethod
    def _decision_text(decision: Decision, action: DraftAction) -> str:
        if decision.status is DecisionStatus.ALREADY:
            return "Решение по черновику уже принято"
        if decision.status is DecisionStatus.NOT_FOUND:
            return "Черновик не найден"
        if decision.status is DecisionStatus.NO_ROUTE:
            return "Диалог удалён — отправлять некуда"
        if action is DraftAction.MANUAL:
            return "Отмечено: отвечаете сами"
        return "Отправлено клиенту"
