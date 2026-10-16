"""Правила напоминаний, биллинга по тарифу «Результат» и готовности салона к автопилоту."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from beauty_agent.conversation.shadow_mode import AccuracyReport, critical_edit
from beauty_agent.domain.billing import BillingPeriod, BookingFacts, invoice_total, is_billable
from beauty_agent.domain.enums import BookingStatus
from beauty_agent.domain.reminders import ReminderKind, due_times, reminder_text

MSK = ZoneInfo("Europe/Moscow")
TZ = "Europe/Moscow"
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=MSK)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=MSK)


# ---------------------------------------------------------------- напоминания


def test_both_reminders_for_visit_tomorrow() -> None:
    assert due_times(at(24, 15), NOW, TZ) == {ReminderKind.DAY_BEFORE: at(23, 15), ReminderKind.TWO_HOURS: at(24, 13)}


def test_booked_three_hours_before_gets_only_two_hour_reminder() -> None:
    assert due_times(at(23, 12), NOW, TZ) == {ReminderKind.TWO_HOURS: at(23, 10)}


def test_quiet_hours() -> None:
    early = due_times(at(25, 8, 30), NOW, TZ)  # визит в 08:30: за 24 часа — 08:30, за 2 часа — 06:30
    late = due_times(at(25, 23), NOW, TZ)  # визит в 23:00: за 24 часа — 23:00 накануне

    assert early == {ReminderKind.DAY_BEFORE: at(24, 9)}  # утром — не раньше 09:00, «за 2 часа» не будим
    assert late[ReminderKind.DAY_BEFORE] == at(24, 21)  # вечером — не позже 21:00
    assert late[ReminderKind.TWO_HOURS] == at(25, 21)


def test_past_visit_has_no_reminders() -> None:
    assert due_times(NOW - timedelta(hours=1), NOW, TZ) == {}


def test_reminder_text() -> None:
    text = reminder_text(
        ReminderKind.DAY_BEFORE, salon_name="Лаванда", service="Маникюр", master="Ольга", local_start=at(24, 15)
    )

    assert "24.11 в 15:00" in text
    assert "Маникюр" in text
    assert "Ольга" in text
    assert "перенесу или отменю" in text


# ---------------------------------------------------------------- биллинг


NOVEMBER = BillingPeriod.month(2026, 11, TZ)
END_OF_MONTH = at(30, 23)


@pytest.mark.parametrize(
    ("facts", "billable"),
    [
        (BookingFacts(True, BookingStatus.COMPLETED, at(10, 12)), True),
        (BookingFacts(True, BookingStatus.CONFIRMED, at(10, 12)), True),  # визит прошёл, не отменена
        (BookingFacts(True, BookingStatus.NO_SHOW, at(10, 12)), True),  # неявка — запись была настоящей
        (BookingFacts(True, BookingStatus.CANCELLED, at(10, 12)), False),  # отменена до визита
        (BookingFacts(True, BookingStatus.PENDING, at(10, 12)), False),  # не подтверждена администратором
        (BookingFacts(False, BookingStatus.COMPLETED, at(10, 12)), False),  # записал администратор
        (BookingFacts(True, BookingStatus.CONFIRMED, datetime(2026, 12, 1, 0, 30, tzinfo=MSK)), False),  # декабрь
        (BookingFacts(True, BookingStatus.CONFIRMED, datetime(2026, 11, 1, 0, 0, tzinfo=MSK)), True),  # полночь
    ],
)
def test_billing_rule(facts: BookingFacts, billable: bool) -> None:
    assert is_billable(facts, NOVEMBER, now=datetime(2026, 12, 5, tzinfo=MSK)) is billable


def test_future_visit_is_not_billed_yet() -> None:
    assert not is_billable(BookingFacts(True, BookingStatus.CONFIRMED, END_OF_MONTH), NOVEMBER, now=at(29, 12))


def test_month_boundaries_in_salon_timezone() -> None:
    december = BillingPeriod.month(2026, 12, TZ)

    assert december.start == datetime(2026, 12, 1, tzinfo=MSK)
    assert december.end == datetime(2027, 1, 1, tzinfo=MSK)
    assert invoice_total(3) == Decimal(177)


# ---------------------------------------------------------------- автопилот


def test_critical_edit_is_price_or_time() -> None:
    assert critical_edit("Маникюр — 1 800 ₽, в 15:00", "Маникюр — 1 900 ₽, в 15:00")
    assert critical_edit("Жду вас в 15:00", "Жду вас в 16:00")
    assert not critical_edit("Маникюр — 1 800 ₽", "Маникюр стоит 1 800 ₽!")
    assert not critical_edit("Ссылка: https://a.example/privacy/test-b606", "Ссылка: https://a.example/privacy/x")


def test_autopilot_needs_volume_accuracy_and_no_critical_errors() -> None:
    ready = AccuracyReport(total=120, pending=10, as_is=95, small_edits=6, big_edits=5, manual=4, critical=0)
    few = AccuracyReport(total=50, pending=0, as_is=50, small_edits=0, big_edits=0, manual=0, critical=0)
    wrong_price = AccuracyReport(total=120, pending=0, as_is=110, small_edits=5, big_edits=5, manual=0, critical=1)

    assert ready.accuracy == pytest.approx(101 / 110)
    assert ready.autopilot_ready
    assert not few.autopilot_ready
    assert not wrong_price.autopilot_ready
    assert "Можно на автопилот: да" in ready.text("Лаванда", 7)
