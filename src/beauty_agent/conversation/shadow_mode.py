"""Теневой режим: агент не отвечает клиенту сам, а присылает администратору черновик.

Кнопки в админ-боте: «Отправить» — черновик уходит клиенту через его канал; «Исправить» —
администратор присылает исправленный текст, он уходит клиенту; «Отвечу сам» — черновик
не пригодился. Решение — атомарный переход из pending: второе нажатие или второй администратор
ничего не отправят повторно. Отчёт точности — доля черновиков, отправленных без правок или с малыми
правками (похожесть rapidfuzz ≥ 90), — решает, когда переводить салон на автопилот:
не меньше 100 решённых черновиков за неделю, точность от 90% и ни одной критичной правки —
когда администратор исправил цену или время (числа в черновике и в отправленном тексте разные).
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import structlog
from rapidfuzz import fuzz
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.agent.state import StateStore
from beauty_agent.conversation.outbox import DeliveryStatus, Outbox, enqueue_outbound
from beauty_agent.db.models import Draft
from beauty_agent.db.repositories.admins import DraftsRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.enums import Channel, DraftStatus, MessageRole

logger = structlog.get_logger(__name__)

SMALL_EDIT = 90  # похожесть от 90 из 100 — правка мелкая: опечатка, слово, знак препинания
AUTOPILOT_MIN_DECIDED = 100
AUTOPILOT_MIN_ACCURACY = 0.9
# Число отдельным словом: цифры внутри ссылки или ID («test-b606dbcb») ценой не считаются
_NUMBER = re.compile(r"(?<![\w/-])\d+(?:[ :.,]\d+)*(?![\w/])")
MAX_ADMIN_TEXT = 4000


class DraftAction(StrEnum):
    SEND = "s"
    EDIT = "e"
    MANUAL = "m"


class DecisionStatus(StrEnum):
    DONE = "done"
    ALREADY = "already"  # решение уже принято
    NOT_FOUND = "not_found"
    NO_ROUTE = "no_route"  # диалог удалён (отзыв согласия) или не из мессенджера


@dataclass(frozen=True, slots=True)
class Decision:
    status: DecisionStatus
    delivery: DeliveryStatus | None = None


class DraftPublisher(Protocol):
    async def publish(self, draft: Draft) -> None:
        """Показать черновик администраторам салона (админ-бот)."""
        ...


class LogDraftPublisher:
    """Админ-бот не настроен: черновики только в логе (локальная разработка)."""

    async def publish(self, draft: Draft) -> None:
        logger.warning("draft_not_published_admin_bot_disabled", salon_id=str(draft.salon_id))


def draft_outbox_key(draft_id: uuid.UUID) -> str:
    return f"draft:{draft_id}"


def similarity(draft: str, final: str) -> int:
    return round(fuzz.ratio(draft, final))


def numbers(text: str) -> set[str]:
    """Цены, время, даты из текста: «1 800», «15:00», «20.10» — без пробелов внутри."""
    return {match.replace(" ", "") for match in _NUMBER.findall(text)}


def critical_edit(draft: str, final: str) -> bool:
    """Администратор исправил цену, время или дату — ошибка агента, которая стоила бы клиента."""
    return numbers(draft) != numbers(final)


@dataclass(frozen=True, slots=True)
class AccuracyReport:
    total: int
    pending: int
    as_is: int
    small_edits: int
    big_edits: int
    manual: int
    critical: int = 0  # правки цены или времени

    @property
    def decided(self) -> int:
        return self.total - self.pending

    @property
    def accuracy(self) -> float:
        """Доля черновиков, ушедших без правок или с малыми правками, среди решённых."""
        return (self.as_is + self.small_edits) / self.decided if self.decided else 0.0

    @property
    def autopilot_ready(self) -> bool:
        return self.decided >= AUTOPILOT_MIN_DECIDED and self.accuracy >= AUTOPILOT_MIN_ACCURACY and self.critical == 0

    def text(self, salon_name: str, days: int) -> str:
        if not self.total:
            return f"{salon_name}: за {days} дн. черновиков не было."
        return (
            f"{salon_name}, черновики за {days} дн.: {self.total}\n"
            f"Отправлены без правок: {self.as_is}\n"
            f"С малыми правками: {self.small_edits}\n"
            f"С большими правками: {self.big_edits}\n"
            f"Ответили сами: {self.manual}\n"
            f"Ждут решения: {self.pending}\n"
            f"Исправлены цена или время: {self.critical}\n"
            f"Точность: {self.accuracy:.0%}\n"
            f"Можно на автопилот: {'да' if self.autopilot_ready else 'нет'} "
            f"(нужно от {AUTOPILOT_MIN_DECIDED} решений, от {AUTOPILOT_MIN_ACCURACY:.0%} без правок, "
            "без ошибок в цене и времени)"
        )


class ShadowMode:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        outbox: Outbox,
        states: StateStore,
        publisher: DraftPublisher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.outbox = outbox
        self.states = states
        self.publisher: DraftPublisher = publisher or LogDraftPublisher()

    async def publish(self, draft_id: uuid.UUID) -> None:
        """Показать черновик администраторам ровно один раз; при сбое — вернуть отметку для повтора."""
        async with self.session_factory() as session:
            draft = await DraftsRepository(session).claim_notification(draft_id)
            await session.commit()
        if draft is None:
            return
        try:
            await self.publisher.publish(draft)
        except Exception:
            async with self.session_factory() as session:
                await DraftsRepository(session).release_notification(draft_id)
                await session.commit()
            raise

    async def decide(
        self,
        salon_id: uuid.UUID,
        draft_id: uuid.UUID,
        action: DraftAction,
        admin_id: str,
        text: str | None = None,
    ) -> Decision:
        target = {
            DraftAction.SEND: DraftStatus.SENT,
            DraftAction.EDIT: DraftStatus.EDITED,
            DraftAction.MANUAL: DraftStatus.MANUAL,
        }[action]
        final = (text or "").strip()[:MAX_ADMIN_TEXT] if action is DraftAction.EDIT else None
        async with self.session_factory() as session:
            drafts = DraftsRepository(session)
            existing = await drafts.get(draft_id)
            if existing is None or existing.salon_id != salon_id or (action is DraftAction.EDIT and not final):
                return Decision(DecisionStatus.NOT_FOUND)
            current, conversation_id, draft_text = existing.status, existing.conversation_id, existing.text
            usage = (existing.tokens_in, existing.tokens_out, existing.cost_rub)
            decided = await drafts.decide(
                draft_id,
                salon_id,
                status=target,
                admin_id=admin_id,
                final_text=final,
                similarity=similarity(draft_text, final) if final else None,
            )
            if not decided:
                await session.rollback()
                # Решение уже принято. Если ответ должен был уйти, но задача упала до отправки, — дослать
                if current in (DraftStatus.SENT, DraftStatus.EDITED):
                    delivery = await self.outbox.deliver(salon_id, draft_outbox_key(draft_id))
                    return Decision(DecisionStatus.ALREADY, delivery)
                return Decision(DecisionStatus.ALREADY)
            queued = target is not DraftStatus.MANUAL and await self._queue_reply(
                session, salon_id, conversation_id, draft_id, final or draft_text, edited=final is not None, usage=usage
            )
            await session.commit()
        if target is not DraftStatus.SENT:
            # агент помнит свой черновик; пусть следующий ход восстановит из БД то, что увидел клиент
            await self.states.forget(str(salon_id), str(conversation_id))
        if target is DraftStatus.MANUAL:
            return Decision(DecisionStatus.DONE)
        if not queued:
            return Decision(DecisionStatus.NO_ROUTE)
        return Decision(DecisionStatus.DONE, await self.outbox.deliver(salon_id, draft_outbox_key(draft_id)))

    @staticmethod
    async def _queue_reply(
        session: AsyncSession,
        salon_id: uuid.UUID,
        conversation_id: uuid.UUID,
        draft_id: uuid.UUID,
        text: str,
        *,
        edited: bool,
        usage: tuple[int | None, int | None, Decimal | None],
    ) -> bool:
        conversations = ConversationsRepository(session, salon_id)
        conversation = await conversations.get(conversation_id)
        route = conversation.reply_route if conversation else None
        if conversation is None or not route:
            return False
        key = draft_outbox_key(draft_id)
        tokens_in, tokens_out, cost_rub = usage
        await conversations.add_message(
            conversation_id,
            MessageRole.ADMIN if edited else MessageRole.AGENT,
            text,
            external_id=key,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_rub=cost_rub,
        )
        await enqueue_outbound(
            session,
            salon_id=salon_id,
            account_id=uuid.UUID(str(route["account_id"])),
            conversation_id=conversation_id,
            channel=Channel(conversation.channel),
            chat_id=str(route["chat_id"]),
            route=dict(route.get("route") or {}),
            text=text,
            dedupe_key=key,
        )
        return True

    async def report(self, salon_id: uuid.UUID, since: datetime) -> AccuracyReport:
        async with self.session_factory() as session:
            drafts = DraftsRepository(session)
            stats = await drafts.stats(salon_id, since)
            edits = await drafts.edits(salon_id, since)
        edited = [value or 0 for value in stats[DraftStatus.EDITED]]
        small = sum(1 for value in edited if value >= SMALL_EDIT)
        return AccuracyReport(
            total=sum(len(values) for values in stats.values()),
            pending=len(stats[DraftStatus.PENDING]),
            as_is=len(stats[DraftStatus.SENT]),
            small_edits=small,
            big_edits=len(edited) - small,
            manual=len(stats[DraftStatus.MANUAL]),
            critical=sum(critical_edit(draft, final) for draft, final in edits),
        )
