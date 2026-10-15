"""Расчёт свободного времени мастера чистыми функциями: без БД, HTTP и текущего времени внутри.

1. Занятые интервалы (записи и перерывы) сортируются и сливаются.
2. Слитые занятые интервалы вычитаются из рабочих проходом двумя указателями.
3. В каждом свободном окне начало выравнивается вверх по сетке (15 минут), и старты выдаются,
   пока услуга помещается в окно. Всё раньше «сейчас + минимальное время до записи» отбрасывается.

Все моменты — datetime с часовым поясом. Сетка считается от начала эпохи UTC: для поясов
со смещением, кратным 15 минутам (все пояса РФ), это совпадает с сеткой местного времени.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

GRID = timedelta(minutes=15)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """Полуоткрытый интервал [start, end): записи 10:00–11:00 и 11:00–12:00 не пересекаются."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Interval: нужно время с часовым поясом")
        if self.start > self.end:
            raise ValueError("Interval: начало позже конца")

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Сортирует и сливает пересекающиеся и смежные интервалы. Пустые выбрасывает."""
    merged: list[Interval] = []
    for current in sorted(item for item in intervals if item.start < item.end):
        if merged and current.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Interval(last.start, max(last.end, current.end))
        else:
            merged.append(current)
    return merged


def subtract(working: Iterable[Interval], busy: Iterable[Interval]) -> list[Interval]:
    """Рабочее время минус занятое: двумя указателями по двум отсортированным спискам, O(n + m)."""
    free: list[Interval] = []
    busy_sorted = merge(busy)
    index = 0
    for shift in merge(working):
        cursor = shift.start
        # занятые интервалы, целиком закончившиеся до начала смены, больше не нужны
        while index < len(busy_sorted) and busy_sorted[index].end <= shift.start:
            index += 1
        position = index
        while position < len(busy_sorted) and busy_sorted[position].start < shift.end:
            block = busy_sorted[position]
            if block.start > cursor:
                free.append(Interval(cursor, block.start))
            cursor = max(cursor, block.end)
            position += 1
        if cursor < shift.end:
            free.append(Interval(cursor, shift.end))
    return free


def align_up(moment: datetime, step: timedelta = GRID) -> datetime:
    """Ближайший момент сетки не раньше moment: 10:07 → 10:15, 10:15 → 10:15."""
    steps, remainder = divmod(moment - _EPOCH, step)
    if remainder:
        steps += 1
    return (_EPOCH + steps * step).astimezone(moment.tzinfo)


def free_starts(
    working: Sequence[Interval],
    busy: Sequence[Interval],
    duration: timedelta,
    *,
    not_before: datetime,
    step: timedelta = GRID,
) -> list[datetime]:
    """Все моменты, с которых услуга длиной duration помещается в свободное время.

    not_before — «сейчас + минимальное время до записи»: более ранние старты не предлагаются.
    """
    if duration <= timedelta(0):
        raise ValueError("Длительность услуги должна быть положительной")
    starts: list[datetime] = []
    for window in subtract(working, busy):
        start = align_up(max(window.start, not_before), step)
        while start + duration <= window.end:
            starts.append(start)
            start += step
    return starts
