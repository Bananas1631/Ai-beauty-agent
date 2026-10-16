"""Напоминания о записи: за 24 и за 2 часа до визита, в тот мессенджер, откуда пришёл клиент.

Чистые правила без ввода-вывода: когда напоминать и что написать.
- Напоминание, время которого уже прошло к моменту записи, не ставится: записались за 3 часа —
  придёт только «за 2 часа».
- Ночью не будим (22:00–09:00 по времени салона): «за 24 часа» поздним вечером уходит в 21:00,
  ранним утром — в 09:00 того же дня; «за 2 часа» ночью пропускается — визит ранним утром.
- Напоминания включаются в настройках салона: многие салоны уже шлют SMS из YCLIENTS.
"""

from datetime import datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

QUIET_FROM = time(22)
QUIET_TO = time(9)
EVENING = time(21)
MORNING = time(9)


class ReminderKind(StrEnum):
    DAY_BEFORE = "24h"
    TWO_HOURS = "2h"


OFFSETS = {ReminderKind.DAY_BEFORE: timedelta(hours=24), ReminderKind.TWO_HOURS: timedelta(hours=2)}


def _quiet(moment: time) -> bool:
    return moment >= QUIET_FROM or moment < QUIET_TO


def due_times(starts_at: datetime, now: datetime, timezone: str) -> dict[ReminderKind, datetime]:
    """Когда отправить напоминания о визите в starts_at; уже прошедшие и ночные — не ставятся."""
    tz = ZoneInfo(timezone)
    result: dict[ReminderKind, datetime] = {}
    for kind, offset in OFFSETS.items():
        due = (starts_at - offset).astimezone(tz)
        if _quiet(due.time()):
            if kind is not ReminderKind.DAY_BEFORE:
                continue
            due = datetime.combine(due.date(), EVENING if due.time() >= QUIET_FROM else MORNING, tzinfo=tz)
        if due > now:
            result[kind] = due
    return result


def reminder_text(kind: ReminderKind, *, salon_name: str, service: str, master: str, local_start: datetime) -> str:
    when = local_start.strftime("%d.%m в %H:%M")
    # «за 24 часа» в тихие часы сдвигается, поэтому «завтра» не пишем — дата всегда явная
    lead = "Напоминаем о записи" if kind is ReminderKind.DAY_BEFORE else "Через 2 часа ждём вас"
    return (
        f"{lead} в «{salon_name}»: {service}, мастер {master}, {when}.\n"
        "Если планы изменились, напишите сюда — перенесу или отменю запись."
    )
