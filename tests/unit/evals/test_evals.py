"""Прогонщик и метрики evals; заодно — офлайн-модель на части датасета."""

from datetime import datetime
from decimal import Decimal

import pytest

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.llm.offline import OfflineLLM
from beauty_agent.domain.entities import BookingRequest, ClientContact, Slot
from evals.metrics import Expect, Scenario, ScenarioResult, contains_all, percentile, summarize
from evals.run import NOW, TZ, judge, load_dataset, render_markdown, run_model, tools_ok


def scenario(**expect: object) -> Scenario:
    return Scenario.model_validate({"id": "s-1", "messages": ["привет"], "expect": expect})


def crm_with_booking(service_id: str, master_id: str, hour: int, day: int = 23) -> FakeCRM:
    crm = FakeCRM(clock=lambda: NOW)
    slot = Slot(master_id, service_id, datetime(2026, 10, day, hour, 0, tzinfo=TZ), 90)
    crm.bookings["key"] = (BookingRequest(slot, ClientContact("Анна", "+79161502239"), "key"), None)  # type: ignore[assignment]
    return crm


def test_dataset_has_fifty_valid_scenarios() -> None:
    scenarios = load_dataset("booking/v1")

    assert len(scenarios) == 50
    assert {s.expect.outcome for s in scenarios} == {"booking", "handoff", "answer"}


@pytest.mark.parametrize("name", ["../secrets", "booking/v1.jsonl", "booking"])
def test_dataset_name_is_not_a_path(name: str) -> None:
    with pytest.raises(ValueError, match="booking/v1"):
        load_dataset(name)


def test_judge_checks_booking_details() -> None:
    crm = crm_with_booking("102", "11", 18)  # пятница, 23 октября, 18:00

    ok = judge(scenario(outcome="booking", service_id="102", part_of_day="evening", weekday=4), "booking", crm, [], [])
    wrong = judge(
        scenario(outcome="booking", service_id="104", master_id="12", part_of_day="morning"), "booking", crm, [], []
    )

    assert ok == []
    assert len(wrong) == 3


def test_judge_outcome_and_text() -> None:
    reasons = judge(
        scenario(outcome="answer", must_contain=["2 500"]), "handoff", FakeCRM(clock=lambda: NOW), ["Стоит 2500 ₽"], []
    )

    assert reasons == ["итог handoff, ожидался answer"]  # «2500» и «2 500» — одно число


def test_tools_ok_forbids_stray_booking() -> None:
    assert tools_ok(scenario(outcome="answer", tools=["find_service"]), ["find_service"])
    assert not tools_ok(scenario(outcome="answer"), ["find_service", "book"])
    assert not tools_ok(scenario(outcome="booking", tools=["book"]), ["find_service"])


def test_contains_all_and_percentile() -> None:
    assert contains_all("от 2500 до 3 000 ₽", ["2 500", "3000"]) == []
    assert contains_all("Москва", ["Примерная"]) == ["Примерная"]
    assert percentile([100, 200, 300, 400, 5000], 0.95) == 5000
    assert percentile([], 0.95) == 0.0


def test_summarize() -> None:
    booking = scenario(outcome="booking")
    handoff = scenario(outcome="handoff")
    results = [
        ScenarioResult(booking, True, "booking", [], ["book"], True, Decimal("1.5"), [1000, 2000]),
        ScenarioResult(booking, False, "answer", [], [], False, Decimal("0.5"), [3000]),
        ScenarioResult(handoff, True, "handoff", [], [], True, Decimal(0), [10], guardrails=["health"]),
    ]

    metrics = summarize(results)

    assert metrics["booking_accuracy"] == 0.5
    assert metrics["handoff_accuracy"] == 1.0
    assert metrics["escalation_rate"] == round(1 / 3, 4)
    assert metrics["cost_per_dialog_rub"] == round(2 / 3, 4)
    assert metrics["p95_latency_ms"] == 3000


async def test_offline_baseline_on_part_of_dataset() -> None:
    scenarios = [s for s in load_dataset("booking/v1") if "master" not in s.tags and "weekday" not in s.tags][:20]

    results = await run_model(scenarios, OfflineLLM(), concurrency=4, prompt_version="v1")

    metrics = summarize(results)
    assert metrics["errors"] == 0
    assert metrics["success_rate"] >= 0.8  # нижняя планка: правила проходят типовые сценарии
    report = {"models": {"offline": {"metrics": metrics, "scenarios": []}}, "started_at": "2026-10-29T10:00:00+00:00"}
    assert "| offline |" in render_markdown("booking/v1", "v1", report)


def test_expect_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        Expect.model_validate({"outcome": "answer", "price": 100})
