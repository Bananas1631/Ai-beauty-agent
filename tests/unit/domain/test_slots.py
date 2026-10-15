from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beauty_agent.domain.booking_engine.slots import GRID, Interval, align_up, free_starts, merge, subtract

MSK = ZoneInfo("Europe/Moscow")


def at(hour: int, minute: int = 0, second: int = 0, *, day: int = 20) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=MSK)


def iv(start: datetime, end: datetime) -> Interval:
    return Interval(start, end)


SHIFT = [iv(at(10), at(20))]
LONG_AGO = at(0, day=1)
HOUR = timedelta(hours=1)


def test_interval_requires_timezone() -> None:
    with pytest.raises(ValueError, match="часовым поясом"):
        Interval(datetime(2026, 10, 20, 10), at(11))


def test_interval_start_after_end_is_rejected() -> None:
    with pytest.raises(ValueError, match="позже"):
        Interval(at(12), at(11))


def test_merge_joins_overlapping_and_adjacent() -> None:
    merged = merge([iv(at(13), at(14)), iv(at(10), at(11)), iv(at(11), at(12)), iv(at(13, 30), at(15))])

    assert merged == [iv(at(10), at(12)), iv(at(13), at(15))]


def test_subtract_two_pointers() -> None:
    free = subtract(SHIFT, [iv(at(12), at(13)), iv(at(9), at(10, 30)), iv(at(19), at(21))])

    assert free == [iv(at(10, 30), at(12)), iv(at(13), at(19))]


def test_busy_interval_spanning_two_shifts_is_subtracted_from_both() -> None:
    shifts = [iv(at(10), at(14)), iv(at(15), at(20))]

    free = subtract(shifts, [iv(at(13), at(16))])

    assert free == [iv(at(10), at(13)), iv(at(16), at(20))]


@pytest.mark.parametrize(
    ("moment", "expected"),
    [(at(10, 7), at(10, 15)), (at(10, 15), at(10, 15)), (at(10, 59, 59), at(11))],
    ids=["round-up", "on-grid", "seconds"],
)
def test_align_up(moment: datetime, expected: datetime) -> None:
    assert align_up(moment) == expected


def test_service_longer_than_window_gives_nothing() -> None:
    assert free_starts([iv(at(10), at(11))], [], timedelta(minutes=90), not_before=LONG_AGO) == []


def test_window_exactly_for_service() -> None:
    assert free_starts([iv(at(10), at(11, 30))], [], timedelta(minutes=90), not_before=LONG_AGO) == [at(10)]


def test_back_to_back_bookings_leave_no_gap() -> None:
    busy = [iv(at(10), at(11)), iv(at(11), at(12))]

    starts = free_starts([iv(at(10), at(13))], busy, HOUR, not_before=LONG_AGO)

    assert starts == [at(12)]


def test_overlapping_bookings() -> None:
    busy = [iv(at(10), at(11, 30)), iv(at(11), at(12))]

    starts = free_starts([iv(at(10), at(13))], busy, HOUR, not_before=LONG_AGO)

    assert starts == [at(12)]


def test_break_at_shift_edge() -> None:
    busy = [iv(at(19), at(20))]  # перерыв в конце смены

    starts = free_starts(SHIFT, busy, HOUR, not_before=LONG_AGO)

    assert starts[-1] == at(18)


def test_shift_over_midnight() -> None:
    night = [iv(at(22), at(2, day=21))]

    starts = free_starts(night, [], HOUR, not_before=LONG_AGO)

    assert starts[0] == at(22)
    assert starts[-1] == at(1, day=21)
    assert at(0, day=21) in starts


def test_empty_schedule() -> None:
    assert free_starts([], [], HOUR, not_before=LONG_AGO) == []


def test_lead_time_and_grid() -> None:
    starts = free_starts(SHIFT, [], HOUR, not_before=at(13, 2))

    assert starts[0] == at(13, 15)  # «сейчас + запас» выровнено вверх по сетке


def test_free_window_start_off_grid_is_aligned() -> None:
    starts = free_starts(SHIFT, [iv(at(10), at(10, 50))], HOUR, not_before=LONG_AGO)

    assert starts[0] == at(11)


def test_zero_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="положительной"):
        free_starts(SHIFT, [], timedelta(0), not_before=LONG_AGO)


# ---------------------------------------------------------------- свойства (hypothesis)

BASE = datetime(2026, 10, 20, tzinfo=UTC)


@st.composite
def intervals(draw: st.DrawFn) -> Interval:
    start = draw(st.integers(min_value=0, max_value=48 * 60))
    length = draw(st.integers(min_value=0, max_value=6 * 60))
    return Interval(BASE + timedelta(minutes=start), BASE + timedelta(minutes=start + length))


@given(
    working=st.lists(intervals(), max_size=4),
    busy=st.lists(intervals(), max_size=8),
    duration_min=st.integers(min_value=15, max_value=240),
    lead_min=st.integers(min_value=0, max_value=48 * 60),
)
def test_slot_properties(working: list[Interval], busy: list[Interval], duration_min: int, lead_min: int) -> None:
    duration = timedelta(minutes=duration_min)
    not_before = BASE + timedelta(minutes=lead_min)

    starts = free_starts(working, busy, duration, not_before=not_before)

    for start in starts:
        slot = Interval(start, start + duration)
        assert not any(slot.overlaps(block) for block in busy)  # не пересекается с занятым
        # внутри смены; смежные смены (10–14 и 14–20) — одно непрерывное рабочее время
        assert any(shift.start <= slot.start and slot.end <= shift.end for shift in merge(working))
        assert (start - BASE) % GRID == timedelta(0)  # кратно шагу сетки
        assert start >= not_before
    assert starts == sorted(set(starts))  # по возрастанию, без повторов
