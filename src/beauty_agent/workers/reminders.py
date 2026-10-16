"""Напоминания клиенту за 24 и за 2 часа — в тот мессенджер, откуда он пришёл.

Периодическая задача раз в 5 минут берёт напоминания, которым пора уйти. Задачи Celery с ETA
не используются: RabbitMQ 4 отключает потребителя, если тот держит неподтверждённое сообщение
дольше 30 минут, а напоминание ждёт сутки.

Перед отправкой запись перепроверяется в CRM: администратор мог перенести или отменить её прямо
в YCLIENTS. Отменена — напоминание снимается; перенесена — запись и напоминания переставляются.
CRM временно недоступна — повтор в следующий запуск. Без токена сотрудника проверить запись нельзя —
напоминание уходит по нашим данным. Строка напоминания блокируется (FOR UPDATE SKIP LOCKED):
два воркера не отправят одно напоминание дважды, а повтор отправки отсекает outbox.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.conversation.outbox import Outbox, enqueue_outbound
from beauty_agent.db.models import Booking, Conversation, Master, Reminder, Salon, Service
from beauty_agent.db.repositories.bookings import SqlBookingLedger
from beauty_agent.domain.entities import CrmBooking, Slot
from beauty_agent.domain.enums import BookingStatus, Channel, ReminderStatus
from beauty_agent.domain.errors import ProviderAuthError, ProviderUnavailableError
from beauty_agent.domain.reminders import ReminderKind, reminder_text
from beauty_agent.workers.celery_app import celery, run, runtime

logger = structlog.get_logger(__name__)

BATCH = 100
TOO_LATE = timedelta(minutes=30)  # визит меньше чем через полчаса — напоминать уже поздно


@dataclass
class ReminderReport:
    sent: int = 0
    skipped: int = 0
    cancelled: int = 0
    retry: int = 0
    keys: list[tuple[uuid.UUID, str]] = field(default_factory=list)  # что доставить через outbox


class ReminderSender:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        providers: ProviderRegistry,
        outbox: Outbox,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.providers = providers
        self.outbox = outbox
        self.clock = clock or (lambda: datetime.now(UTC))

    async def send_due(self) -> ReminderReport:
        now = self.clock()
        async with self.session_factory() as session:
            due = list(
                await session.scalars(
                    select(Reminder.id)
                    .where(Reminder.status == ReminderStatus.PENDING, Reminder.due_at <= now)
                    .order_by(Reminder.due_at)
                    .limit(BATCH)
                )
            )
        report = ReminderReport()
        for reminder_id in due:
            await self._one(reminder_id, now, report)
        for salon_id, dedupe_key in report.keys:
            try:
                await self.outbox.deliver(salon_id, dedupe_key)
            except Exception:  # сеть мессенджера: строку outbox дошлёт периодическая досылка
                logger.warning("reminder_delivery_deferred", salon_id=str(salon_id))
        return report

    async def _one(self, reminder_id: uuid.UUID, now: datetime, report: ReminderReport) -> None:
        async with self.session_factory() as session:
            reminder = await session.scalar(
                select(Reminder)
                .where(Reminder.id == reminder_id, Reminder.status == ReminderStatus.PENDING)
                .with_for_update(skip_locked=True)
            )
            if reminder is None:
                return  # другой воркер уже взял или статус изменился
            row = (
                await session.execute(
                    select(Booking, Salon, Conversation, Master, Service)
                    .join(Salon, Salon.id == Booking.salon_id)
                    .join(Master, Master.id == Booking.master_id)
                    .join(Service, Service.id == Booking.service_id)
                    .outerjoin(Conversation, Conversation.id == Booking.conversation_id)
                    .where(Booking.id == reminder.booking_id)
                )
            ).first()
            if row is None:
                reminder.status = ReminderStatus.SKIPPED
                await session.commit()
                report.skipped += 1
                return
            booking, salon, conversation, master, service = row
            route = conversation.reply_route if conversation is not None else None
            if booking.status is not BookingStatus.CONFIRMED or booking.starts_at - now < TOO_LATE or not route:
                reminder.status = ReminderStatus.SKIPPED  # отменена, уже скоро или клиент писал с сайта
                await session.commit()
                report.skipped += 1
                return
            ledger = SqlBookingLedger(
                session, salon.id, timezone=salon.timezone, reminders=salon.config.reminders_enabled, clock=self.clock
            )
            if booking.external_id is not None:
                try:
                    in_crm: CrmBooking | None = await self.providers.for_salon(salon).get_booking(booking.external_id)
                except ProviderAuthError:
                    in_crm = CrmBooking(booking.external_id, _slot_of(booking, master, service))  # проверить нечем
                except ProviderUnavailableError:
                    await session.rollback()  # строка снова pending — повтор в следующий запуск
                    report.retry += 1
                    return
                if in_crm is None or not in_crm.active:
                    await ledger.cancel(booking.external_id)  # снимет и это напоминание
                    report.cancelled += 1
                    return
                if in_crm.slot.starts_at != booking.starts_at or in_crm.slot.master_id != master.external_id:
                    await ledger.move(booking.external_id, in_crm.slot)  # напоминания переставятся
                    report.cancelled += 1
                    return
            tz = ZoneInfo(salon.timezone)
            text = reminder_text(
                ReminderKind(reminder.kind),
                salon_name=salon.name,
                service=service.name,
                master=master.name,
                local_start=booking.starts_at.astimezone(tz),
            )
            dedupe_key = f"reminder:{reminder.id}"
            await enqueue_outbound(
                session,
                salon_id=salon.id,
                account_id=uuid.UUID(str(route["account_id"])),
                conversation_id=booking.conversation_id,
                channel=Channel(conversation.channel) if conversation else Channel.TELEGRAM,
                chat_id=str(route["chat_id"]),
                route=dict(route.get("route") or {}),
                text=text,
                dedupe_key=dedupe_key,
            )
            reminder.status = ReminderStatus.SENT
            reminder.sent_at = now
            await session.commit()
            report.sent += 1
            report.keys.append((salon.id, dedupe_key))


def _slot_of(booking: Booking, master: Master, service: Service) -> Slot:
    minutes = max(int((booking.ends_at - booking.starts_at).total_seconds() // 60), 1)
    return Slot(master.external_id, service.external_id, booking.starts_at, minutes)


@celery.task(name="periodic.reminders")
def reminders_task() -> int:
    current = runtime()
    sender = ReminderSender(current.session_factory, current.providers, current.outbox)
    return run(sender.send_due()).sent


async def refresh_booking(
    session_factory: async_sessionmaker[AsyncSession],
    providers: ProviderRegistry,
    salon_id: uuid.UUID,
    external_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Сверить нашу копию записи с YCLIENTS: cancelled | moved | unchanged | unknown."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Booking, Salon, Master)
                .join(Salon, Salon.id == Booking.salon_id)
                .join(Master, Master.id == Booking.master_id)
                .where(Booking.salon_id == salon_id, Booking.external_id == external_id)
            )
        ).first()
        if row is None:
            return "unknown"  # запись сделана не через ИИ-администратора — нам её вести не нужно
        booking, salon, master = row
        ledger = SqlBookingLedger(
            session, salon.id, timezone=salon.timezone, reminders=salon.config.reminders_enabled, clock=clock
        )
        in_crm = await providers.for_salon(salon).get_booking(external_id)
        if in_crm is None or not in_crm.active:
            await ledger.cancel(external_id)
            return "cancelled"
        if in_crm.slot.starts_at != booking.starts_at or in_crm.slot.master_id != master.external_id:
            await ledger.move(external_id, in_crm.slot)
            return "moved"
        return "unchanged"


@celery.task(bind=True, name="messages.sync_booking", max_retries=3)
def sync_booking_task(self: Any, salon_id: str, external_id: str) -> str:
    current = runtime()
    try:
        return run(refresh_booking(current.session_factory, current.providers, uuid.UUID(salon_id), external_id))
    except ProviderUnavailableError as exc:
        raise self.retry(exc=exc, countdown=30 * (1 << self.request.retries)) from exc
