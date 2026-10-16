"""Метрики Prometheus и сквозной trace_id: API → задача Celery → логи и Sentry воркера."""

from decimal import Decimal
from types import SimpleNamespace

import structlog
from prometheus_client import REGISTRY

from beauty_agent.api.metrics import queue_lengths
from beauty_agent.core import metrics
from beauty_agent.core.sentry import scrub_event
from beauty_agent.workers.celery_app import _attach_trace_id, _bind_trace_id, _unbind_trace_id


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_provider_errors_and_llm_cost() -> None:
    before = sample("beauty_agent_provider_errors_total", provider="yclients", kind="timeout")
    cost_before = sample("beauty_agent_llm_cost_rub_total", salon_id="s-1")

    metrics.provider_error("yclients", "timeout")
    metrics.llm_cost("s-1", Decimal("0.25"))
    metrics.llm_cost("s-1", Decimal(0))  # нулевой расход не создаёт шума

    assert sample("beauty_agent_provider_errors_total", provider="yclients", kind="timeout") == before + 1
    assert sample("beauty_agent_llm_cost_rub_total", salon_id="s-1") == cost_before + 0.25


def test_reply_timer_observes_even_on_error() -> None:
    before = sample("beauty_agent_reply_seconds_count", channel="vk")

    try:
        with metrics.reply_timer("vk"):
            raise RuntimeError("сбой агента")
    except RuntimeError:
        pass

    assert sample("beauty_agent_reply_seconds_count", channel="vk") == before + 1


def test_render_contains_metrics() -> None:
    body, content_type = metrics.render()

    assert content_type.startswith("text/plain")
    assert b"beauty_agent_reply_seconds" in body


def test_queue_lengths_when_broker_is_down() -> None:
    lengths = queue_lengths("amqp://guest:guest@127.0.0.1:1//")

    assert lengths == {"messages": -1, "periodic": -1, "dead": -1}  # алерт «очередь стоит» не молчит


def test_trace_id_goes_from_api_to_worker() -> None:
    structlog.contextvars.bind_contextvars(trace_id="trace-from-webhook")
    headers: dict[str, object] = {}
    _attach_trace_id(headers=headers)
    structlog.contextvars.clear_contextvars()

    task = SimpleNamespace(name="messages.process_batch", request=SimpleNamespace(trace_id=headers["trace_id"]))
    _bind_trace_id(task=task)
    in_worker = structlog.contextvars.get_contextvars()
    event = scrub_event({"message": "ошибка"})
    _unbind_trace_id()

    assert headers == {"trace_id": "trace-from-webhook"}
    assert in_worker["trace_id"] == "trace-from-webhook"
    assert event["tags"]["trace_id"] == "trace-from-webhook"  # событие Sentry находится по ID в логах
    assert structlog.contextvars.get_contextvars() == {}


def test_task_without_trace_gets_new_one() -> None:
    _bind_trace_id(task=SimpleNamespace(name="periodic.reminders", request=SimpleNamespace()))
    trace_id = structlog.contextvars.get_contextvars()["trace_id"]
    _unbind_trace_id()

    assert len(trace_id) == 32
