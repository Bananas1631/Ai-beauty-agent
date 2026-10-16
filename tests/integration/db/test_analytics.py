"""Аналитика на чистом SQL. Набор данных маленький, ответы посчитаны вручную (в комментариях).

Салон в Москве (UTC+3), период — неделя 16–22 ноября 2026, рабочие часы 10–21.

    B1  пн 16.11 10:30–12:00  Ольга   маникюр 1500  состоялась   Анна  ИИ     создана пн 22:30
    B2  пн 16.11 11:00–12:00  Марина  стрижка 2000  подтверждена Вера  админ  создана пн 12:00
    B3  вт 17.11 10:00–11:00  Ольга   маникюр 1500  ОТМЕНЕНА     Ира   ИИ     создана вт 09:00
    B4  пн 23.11 00:30–01:20  Ольга   брови   1000  состоялась   Анна  ИИ     создана пт 08:00
        (по UTC это воскресенье 22.11 21:30 — неделя по Москве уже следующая)
    B5  ср 25.11 12:00–13:00  Марина  стрижка 2000  подтверждена Вера  ИИ     создана сб 15:00
    B6  чт 10.12 12:00–13:00  Марина  стрижка 2000  подтверждена Вера  админ  создана 01.12
    B7  ср 18.11 14:00–15:00  Марина  брови   1000  состоялась   Ира   админ  создана 10.11
    Алина — мастер без записей.
"""

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Booking, Client, Master, Salon, Service
from beauty_agent.db.repositories.analytics import AnalyticsRepository, load_query
from beauty_agent.domain.enums import BookingStatus

MSK = ZoneInfo("Europe/Moscow")
TZ = "Europe/Moscow"
WEEK_START = datetime(2026, 11, 16, tzinfo=MSK)
WEEK_END = datetime(2026, 11, 23, tzinfo=MSK)
TWO_WEEKS_END = datetime(2026, 11, 30, tzinfo=MSK)


def msk(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


@pytest.fixture
async def data(session: AsyncSession, salon: Salon) -> dict[str, uuid.UUID]:
    masters = {name: Master(salon_id=salon.id, external_id=name, name=name) for name in ("Ольга", "Марина", "Алина")}
    services = {
        name: Service(salon_id=salon.id, external_id=name, name=name, price_min=price, price_max=price, duration_min=60)
        for name, price in (("Маникюр", Decimal(1500)), ("Стрижка", Decimal(2000)), ("Брови", Decimal(1000)))
    }
    clients = {name: Client(salon_id=salon.id, name=name) for name in ("Анна", "Вера", "Ира")}
    session.add_all([*masters.values(), *services.values(), *clients.values()])
    await session.flush()
    rows = [
        # мастер, услуга, клиент, начало, минут, статус, ИИ, создана
        ("Ольга", "Маникюр", "Анна", msk(11, 16, 10, 30), 90, BookingStatus.COMPLETED, True, msk(11, 16, 22, 30)),
        ("Марина", "Стрижка", "Вера", msk(11, 16, 11), 60, BookingStatus.CONFIRMED, False, msk(11, 16, 12)),
        ("Ольга", "Маникюр", "Ира", msk(11, 17, 10), 60, BookingStatus.CANCELLED, True, msk(11, 17, 9)),
        ("Ольга", "Брови", "Анна", msk(11, 23, 0, 30), 50, BookingStatus.COMPLETED, True, msk(11, 20, 8)),
        ("Марина", "Стрижка", "Вера", msk(11, 25, 12), 60, BookingStatus.CONFIRMED, True, msk(11, 21, 15)),
        ("Марина", "Стрижка", "Вера", msk(12, 10, 12), 60, BookingStatus.CONFIRMED, False, msk(12, 1, 12)),
        ("Марина", "Брови", "Ира", msk(11, 18, 14), 60, BookingStatus.COMPLETED, False, msk(11, 10, 12)),
    ]
    for master, service, client, starts_at, minutes, status, by_ai, created_at in rows:
        session.add(
            Booking(
                salon_id=salon.id,
                master_id=masters[master].id,
                service_id=services[service].id,
                client_id=clients[client].id,
                starts_at=starts_at,
                ends_at=starts_at + timedelta(minutes=minutes),
                status=status,
                price=services[service].price_min,
                idempotency_key=uuid.uuid4().hex,
                created_by_ai=by_ai,
                created_at=created_at,
            )
        )
    await session.commit()
    return {
        **{f"master:{name}": master.id for name, master in masters.items()},
        **{f"service:{name}": service.id for name, service in services.items()},
        **{f"client:{name}": client.id for name, client in clients.items()},
    }


async def test_master_load(session: AsyncSession, salon: Salon, data: dict[str, uuid.UUID]) -> None:
    rows = await AnalyticsRepository(session, salon.id).master_load(WEEK_START, WEEK_END, timezone=TZ)
    load = {(row.master_name, row.dow, row.hour): row.load for row in rows}

    assert len(rows) == 3 * 7 * 11  # три мастера × 7 дней × часы 10–20: мастер без записей тоже в отчёте
    assert load[("Ольга", 1, 10)] == Decimal("0.500")  # 10:30–11:00 — полчаса
    assert load[("Ольга", 1, 11)] == Decimal("1.000")
    assert load[("Ольга", 2, 10)] == Decimal("0")  # отменённая запись время не занимает
    assert load[("Марина", 3, 14)] == Decimal("1.000")
    assert all(value == 0 for (name, _, _), value in load.items() if name == "Алина")
    assert {row.capacity_minutes for row in rows} == {60}  # каждый день недели — один раз


async def test_revenue_by_week_in_salon_timezone(
    session: AsyncSession, salon: Salon, data: dict[str, uuid.UUID]
) -> None:
    weeks = await AnalyticsRepository(session, salon.id).revenue_by_week(WEEK_START, TWO_WEEKS_END, timezone=TZ)

    assert [(w.week.isoformat(), w.visits, w.revenue, w.average_check) for w in weeks] == [
        ("2026-11-16", 3, Decimal(4500), Decimal("1500.00")),  # B1 + B2 + B7
        ("2026-11-23", 2, Decimal(3000), Decimal("1500.00")),  # B4 (00:30 по Москве) + B5
    ]


async def test_ai_share_with_filter(session: AsyncSession, salon: Salon, data: dict[str, uuid.UUID]) -> None:
    share = await AnalyticsRepository(session, salon.id).ai_share(
        WEEK_START, WEEK_END, timezone=TZ, work_start=time(10), work_end=time(21)
    )

    # создано за неделю: B1, B2, B4, B5 (B3 отменена, B7 и B6 — в другие недели)
    assert (share.bookings, share.ai_bookings, share.ai_share) == (4, 3, Decimal("0.750"))
    assert (share.off_hours_bookings, share.off_hours_ai_bookings) == (2, 2)  # B1 в 22:30 и B4 в 08:00


async def test_returning_clients(session: AsyncSession, salon: Salon, data: dict[str, uuid.UUID]) -> None:
    rows = await AnalyticsRepository(session, salon.id).returning_clients(msk(12, 20, 0))

    # Анна: визиты 16.11 10:30 и 23.11 00:30 — интервал 6,6 дня, не была 27 дней > 2 × 6,6.
    # Вера: интервалы 9 и 15 дней, не была 10 дней — рано. Ира: один визит (отмена не считается).
    assert [(row.client_name, row.visits, row.usual_interval_days, row.days_since_last) for row in rows] == [
        ("Анна", 2, Decimal("6.6"), Decimal("27.0"))
    ]


async def test_top_services_with_running_total(session: AsyncSession, salon: Salon, data: dict[str, uuid.UUID]) -> None:
    rows = await AnalyticsRepository(session, salon.id).top_services(WEEK_START, TWO_WEEKS_END, limit=2)

    assert [(r.service_name, r.revenue, r.share, r.cumulative_revenue, r.cumulative_share) for r in rows] == [
        ("Стрижка", Decimal(4000), Decimal("0.5333"), Decimal(4000), Decimal("0.5333")),
        ("Брови", Decimal(2000), Decimal("0.2667"), Decimal(6000), Decimal("0.8000")),  # доля — от всех 7 500 ₽
    ]


async def test_other_salon_sees_nothing(
    session: AsyncSession, salon: Salon, other_salon: Salon, data: dict[str, uuid.UUID]
) -> None:
    repo = AnalyticsRepository(session, other_salon.id)

    assert await repo.master_load(WEEK_START, WEEK_END, timezone=TZ) == []
    assert (
        await repo.ai_share(WEEK_START, WEEK_END, timezone=TZ, work_start=time(10), work_end=time(21))
    ).bookings == 0
    assert await repo.top_services(WEEK_START, TWO_WEEKS_END) == []


def test_query_names_are_not_paths() -> None:
    with pytest.raises(ValueError, match="Некорректное имя"):
        load_query("../../secrets")
    assert ":salon_id" in str(load_query("ai_share"))  # именованный параметр, а не подставленное значение
