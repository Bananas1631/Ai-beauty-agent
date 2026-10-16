"""Еженедельный отчёт, выгрузка для счёта по правилу domain/billing.py и ночная синхронизация каталога."""

import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import RecordingNotifier

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.notifications import AdminEventKind
from beauty_agent.core.config import YclientsSettings
from beauty_agent.db.models import Booking, Client, Conversation, Draft, Master, Message, Salon, Service
from beauty_agent.domain.billing import BillingPeriod, BookingFacts, is_billable
from beauty_agent.domain.entities import ServiceInfo
from beauty_agent.domain.enums import BookingStatus, Channel, CrmKind, DraftStatus, MessageRole
from beauty_agent.domain.errors import ProviderUnavailableError
from beauty_agent.workers.catalog_sync import sync_catalogs
from beauty_agent.workers.reports import billable_bookings, invoice_csv, send_weekly_reports, weekly_report

MSK = ZoneInfo("Europe/Moscow")
MONDAY = datetime(2026, 11, 30, 10, 0, tzinfo=MSK)  # отчёт за 23–29 ноября


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MSK)


@pytest.fixture
async def catalog_salon(session: AsyncSession, salon: Salon) -> Salon:
    salon.settings = {"crm_kind": CrmKind.FAKE.value, "average_check_rub": "2000"}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    return salon


async def add_booking(
    session: AsyncSession,
    salon: Salon,
    *,
    starts_at: datetime,
    status: BookingStatus = BookingStatus.CONFIRMED,
    by_ai: bool = True,
    created_at: datetime | None = None,
    hour_offset: int = 0,
) -> Booking:
    master = await session.scalar(select(Master).where(Master.salon_id == salon.id, Master.external_id == "11"))
    service = await session.scalar(select(Service).where(Service.salon_id == salon.id, Service.external_id == "101"))
    assert master is not None
    assert service is not None
    booking = Booking(
        salon_id=salon.id,
        master_id=master.id,
        service_id=service.id,
        starts_at=starts_at + timedelta(hours=hour_offset),
        ends_at=starts_at + timedelta(hours=hour_offset, minutes=50),
        status=status,
        price=Decimal(1500),
        external_id=uuid.uuid4().hex[:12],
        idempotency_key=uuid.uuid4().hex,
        created_by_ai=by_ai,
    )
    if created_at is not None:
        booking.created_at = created_at
    session.add(booking)
    await session.flush()
    return booking


async def test_weekly_report(session: AsyncSession, catalog_salon: Salon) -> None:
    salon = catalog_salon
    await add_booking(session, salon, starts_at=at(25, 11), created_at=at(24, 23, 40))
    await add_booking(session, salon, starts_at=at(26, 11), created_at=at(25, 12))
    await add_booking(session, salon, starts_at=at(27, 11), created_at=at(25, 13), status=BookingStatus.CANCELLED)
    await add_booking(session, salon, starts_at=at(28, 11), created_at=at(25, 14), by_ai=False)
    await add_booking(session, salon, starts_at=at(29, 11), created_at=at(20, 14))  # прошлая неделя
    client = Client(salon_id=salon.id)
    session.add(client)
    await session.flush()
    conversation = Conversation(salon_id=salon.id, client_id=client.id, channel=Channel.TELEGRAM)
    session.add(conversation)
    await session.flush()
    for moment, cost in ((at(24, 23, 40), "0.50"), (at(25, 12), "0.25"), (at(26, 7, 15), "0.25")):
        session.add(
            Message(
                salon_id=salon.id,
                conversation_id=conversation.id,
                role=MessageRole.AGENT,
                content="ответ",
                cost_rub=Decimal(cost),
                created_at=moment,
            )
        )
    for status in (DraftStatus.PENDING, DraftStatus.SENT):
        session.add(
            Draft(
                salon_id=salon.id,
                conversation_id=conversation.id,
                dedupe_key=uuid.uuid4().hex,
                question="?",
                text="черновик",
                status=status,
                cost_rub=Decimal("0.10"),
                created_at=at(26, 12),
            )
        )
    await session.commit()

    report = await weekly_report(session, salon, MONDAY)

    assert report.first_day == at(23, 0)
    assert report.ai_bookings == 2  # отменённая, ручная и прошлонедельная не считаются
    assert report.ai_revenue_rub == Decimal(4000)
    assert (report.answers, report.off_hours_answers) == (3, 2)  # 23:40 и 07:15
    assert report.llm_cost_rub == Decimal("1.10")  # отправленный черновик уже учтён в сообщении
    text = report.text()
    assert "неделя 23.11–29.11" in text
    assert "Записей через ИИ: 2" in text
    assert "4 000 ₽" in text


async def test_weekly_reports_go_to_admins(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], catalog_salon: Salon
) -> None:
    notifier = RecordingNotifier()

    await send_weekly_reports(session_factory, notifier, now=MONDAY)

    mine = [event for event in notifier.events if event.salon_id == str(catalog_salon.id)]
    assert [event.kind for event in mine] == [AdminEventKind.WEEKLY_REPORT]


async def test_invoice_follows_billing_rule(session: AsyncSession, catalog_salon: Salon) -> None:
    salon = catalog_salon
    variants = [
        (at(10, 12), BookingStatus.COMPLETED, True),
        (at(11, 12), BookingStatus.CONFIRMED, True),
        (at(12, 12), BookingStatus.NO_SHOW, True),
        (at(13, 12), BookingStatus.CANCELLED, True),
        (at(14, 12), BookingStatus.PENDING, True),
        (at(15, 12), BookingStatus.COMPLETED, False),
        (datetime(2026, 11, 1, 0, 0, tzinfo=MSK), BookingStatus.COMPLETED, True),  # полночь — ноябрь
        (datetime(2026, 10, 31, 23, 0, tzinfo=MSK), BookingStatus.COMPLETED, True),  # октябрь
        (at(29, 18), BookingStatus.CONFIRMED, True),  # визит ещё не наступил
    ]
    bookings = [
        await add_booking(session, salon, starts_at=starts_at, status=status, by_ai=by_ai)
        for starts_at, status, by_ai in variants
    ]
    await session.commit()
    period = BillingPeriod.month(2026, 11, salon.timezone)
    now = at(29, 12)

    lines = await billable_bookings(session, salon.id, period, now)

    expected = [
        booking.external_id
        for booking in sorted(bookings, key=lambda item: item.starts_at)
        if is_billable(BookingFacts(booking.created_by_ai, booking.status, booking.starts_at), period, now)
    ]
    assert [line.external_id for line in lines] == expected  # SQL и правило из договора совпадают
    assert len(lines) == 4
    csv = invoice_csv(lines, salon.timezone)
    assert csv.splitlines()[0].startswith("Дата визита;Услуга;Мастер")
    assert csv.splitlines()[-1] == "Итого;;;;4 записей;236"


# ---------------------------------------------------------------- синхронизация каталога


class RepricedCRM(FakeCRM):
    """CRM, где салон поднял цену маникюра."""

    async def list_services(self) -> list[ServiceInfo]:
        services = await super().list_services()
        return [
            replace(service, price_min=Decimal(1700), price_max=Decimal(1700))
            if service.external_id == "101"
            else service
            for service in services
        ]


class DownCRM(FakeCRM):
    async def get_salon_info(self):  # type: ignore[no-untyped-def]
        raise ProviderUnavailableError


async def test_nightly_catalog_sync(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon, other_salon: Salon
) -> None:
    registry = ProviderRegistry(YclientsSettings())
    for index, target in enumerate((salon, other_salon)):
        target.yclients_company_id = 800_000 + int(uuid.uuid4().int % 100_000) * 2 + index
        target.settings = {"crm_kind": CrmKind.YCLIENTS.value}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug, company_id=salon.yclients_company_id)
    registry.override(salon, RepricedCRM())
    registry.override(other_salon, DownCRM())

    report = await sync_catalogs(session_factory, registry)

    assert salon.id in report.synced
    assert other_salon.id in report.failed  # сбой одного салона не останавливает остальные
    price = await session.scalar(
        select(Service.price_min).where(Service.salon_id == salon.id, Service.external_id == "101")
    )
    assert price == Decimal(1700)
