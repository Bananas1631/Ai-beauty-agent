"""Kafka → ClickHouse без Python: событие доходит за секунды, дубль не удваивает сумму, мусор — в ошибки."""

import asyncio
import json
import time
import uuid
from decimal import Decimal
from typing import Any

from beauty_agent.analytics.clickhouse import ClickHouse
from beauty_agent.domain.events import DomainEvent, LlmCallFinished, LlmCallFinishedData, topic_of
from beauty_agent.events.producer import EventProducer

WAIT_S = 60.0


def llm_event(salon_id: uuid.UUID, *, cost: str = "0.4200", latency_ms: int = 1800) -> LlmCallFinished:
    return LlmCallFinished(
        salon_id=salon_id,
        data=LlmCallFinishedData(
            conversation_id=uuid.uuid4(),
            model="qwen3-235b",
            prompt_version="v2",
            prompt_tokens=1200,
            completion_tokens=150,
            cost_rub=Decimal(cost),
            latency_ms=latency_ms,
            tool_calls=2,
            offers_shown=3,
        ),
    )


async def publish(producer: EventProducer, event: DomainEvent) -> None:
    payload = event.model_dump(mode="json")
    body = json.dumps(payload, ensure_ascii=False).encode()
    await producer.send_raw(topic_of(event), str(event.salon_id).encode(), body, [])


async def wait_for(clickhouse: ClickHouse, sql: str, params: dict[str, Any], expected: Any) -> float:
    """Ждать, пока запрос не вернёт expected; сколько секунд это заняло."""
    started = time.monotonic()
    value = None
    while time.monotonic() - started < WAIT_S:
        value = await clickhouse.scalar(sql, params)
        if value == expected:
            return time.monotonic() - started
        await asyncio.sleep(0.5)
    raise AssertionError(f"за {WAIT_S} с получили {value!r}, ждали {expected!r}")


COUNT = "SELECT count() FROM analytics.events WHERE salon_id = {salon:UUID}"


async def test_event_from_kafka_appears_in_seconds(clickhouse: ClickHouse, producer: EventProducer) -> None:
    salon_id = uuid.uuid4()
    event = llm_event(salon_id)

    await publish(producer, event)
    elapsed = await wait_for(clickhouse, COUNT, {"salon": salon_id}, 1)

    (row,) = await clickhouse.rows(
        "SELECT event_id, event_type, model, cost_rub, latency_ms, offers_shown, conversation_id "
        "FROM analytics.events WHERE salon_id = {salon:UUID}",
        {"salon": salon_id},
    )
    assert row["event_id"] == event.event_id
    assert row["event_type"] == "llm.call.finished"
    assert row["model"] == "qwen3-235b"
    assert row["cost_rub"] == Decimal("0.4200")  # Decimal из строки, без float
    assert row["latency_ms"] == 1800
    assert row["offers_shown"] == 3
    assert row["conversation_id"] == event.data.conversation_id
    assert elapsed < 30  # обычно 1–3 с (kafka_flush_interval_ms = 1 с); первый старт потребителя дольше


async def test_duplicate_delivery_does_not_double_sum(clickhouse: ClickHouse, producer: EventProducer) -> None:
    salon_id = uuid.uuid4()
    event = llm_event(salon_id, cost="1.5000")
    sentinel = llm_event(salon_id, cost="0.0100")

    await publish(producer, event)
    await publish(producer, event)  # relay упал до отметки — событие ушло ещё раз
    await publish(producer, sentinel)  # тот же салон → та же партиция: дошёл он — дошли и дубли
    await wait_for(
        clickhouse,
        "SELECT count() FROM analytics.events WHERE salon_id = {salon:UUID} AND event_id = {sentinel:UUID}",
        {"salon": salon_id, "sentinel": sentinel.event_id},
        1,
    )

    raw = await clickhouse.scalar(COUNT, {"salon": salon_id})
    exact = await clickhouse.scalar(
        "SELECT sum(cost_rub) FROM analytics.events FINAL WHERE salon_id = {salon:UUID}", {"salon": salon_id}
    )
    assert raw == 3 or raw == 2  # без FINAL дубль виден, пока части не слились
    assert exact == Decimal("1.5100")


async def test_garbage_goes_to_errors_and_ingestion_continues(clickhouse: ClickHouse, producer: EventProducer) -> None:
    salon_id = uuid.uuid4()
    marker = f"garbage-{uuid.uuid4()}"

    await producer.send_raw("beauty.llm", b"bad", f"not json {marker}".encode(), [])
    await publish(producer, llm_event(salon_id))

    await wait_for(clickhouse, COUNT, {"salon": salon_id}, 1)
    await wait_for(
        clickhouse,
        "SELECT count() FROM analytics.events_errors WHERE position(raw, {marker:String}) > 0",
        {"marker": marker},
        1,
    )
