import time
import uuid

from beauty_agent.db.base import uuid7


def test_uuid7_version_and_variant() -> None:
    value = uuid7()

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_starts_with_current_time() -> None:
    now_ms = time.time_ns() // 1_000_000

    assert abs((uuid7().int >> 80) - now_ms) < 1000  # первые 48 бит — миллисекунды


def test_uuid7_grows_with_time() -> None:
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()

    assert first < second  # поэтому новые строки дописываются в конец индекса
