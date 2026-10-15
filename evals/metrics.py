"""Метрики evals: точность записи, точность вызова инструментов, эскалации, стоимость, p95 задержки."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from beauty_agent.domain.booking_engine.offers import PartOfDay

Outcome = Literal["booking", "handoff", "answer"]


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    service_id: str | None = None
    master_id: str | None = None
    part_of_day: PartOfDay | None = None
    weekday: int | None = Field(None, ge=0, le=6)  # 0 — понедельник
    must_contain: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tags: list[str] = Field(default_factory=list)
    messages: list[str] = Field(min_length=1)
    expect: Expect


@dataclass
class ScenarioResult:
    scenario: Scenario
    passed: bool
    outcome: Outcome
    answers: list[str]
    tools: list[str]
    tools_ok: bool
    cost_rub: Decimal
    turn_latencies_ms: list[int]
    guardrails: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    error: str | None = None


def normalize_numbers(text: str) -> str:
    """«2 500» и «2500» — одно и то же: пробелы внутри чисел не считаются."""
    return re.sub(r"(?<=\d)[\s  ](?=\d)", "", text)


def contains_all(text: str, needles: Sequence[str]) -> list[str]:
    haystack = normalize_numbers(text).lower()
    return [needle for needle in needles if normalize_numbers(needle).lower() not in haystack]


def percentile(values: Sequence[float], share: float) -> float:
    """Перцентиль методом ближайшего ранга: p95 — значение, которого не превышают 95% замеров."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return float(ordered[rank - 1])


def summarize(results: Sequence[ScenarioResult]) -> dict[str, Any]:
    bookings = [result for result in results if result.scenario.expect.outcome == "booking"]
    handoffs = [result for result in results if result.scenario.expect.outcome == "handoff"]
    latencies = [latency for result in results for latency in result.turn_latencies_ms]
    total_cost = sum((result.cost_rub for result in results), Decimal(0))

    def share(part: Sequence[ScenarioResult], predicate: Any) -> float:
        return round(sum(1 for item in part if predicate(item)) / len(part), 4) if part else 0.0

    return {
        "scenarios": len(results),
        "success_rate": share(results, lambda r: r.passed),
        "booking_accuracy": share(bookings, lambda r: r.passed),
        "handoff_accuracy": share(handoffs, lambda r: r.passed),
        "tool_accuracy": share(results, lambda r: r.tools_ok),
        "escalation_rate": share(results, lambda r: r.outcome == "handoff"),
        "guardrail_rate": share(results, lambda r: bool(r.guardrails)),
        "errors": sum(1 for result in results if result.error),
        "cost_per_dialog_rub": float(round(total_cost / len(results), 4)) if results else 0.0,
        "p95_latency_ms": percentile(latencies, 0.95),
        "p50_latency_ms": percentile(latencies, 0.50),
    }
