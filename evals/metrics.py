"""Метрики evals.

Качество (с допуском): точность записи, вызовы инструментов, доля эскалаций, faithfulness,
стоимость диалога, p95 задержки.
Инварианты (ровно 100%): нет записи на непредложенное время, нет цены вне каталога,
нет нарушений на провокациях — их гарантирует код, а не модель.
"""

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from beauty_agent.domain.booking_engine.offers import PartOfDay

Outcome = Literal["booking", "handoff", "answer"]
ExpectedOutcome = Literal["booking", "handoff", "answer", "no_booking"]  # no_booking — ответ или администратор


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: ExpectedOutcome
    service_id: str | None = None
    master_id: str | None = None
    part_of_day: PartOfDay | None = None
    weekday: int | None = Field(None, ge=0, le=6)  # 0 — понедельник
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
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
    unoffered_booking: bool = False  # записали на время, которого не предлагали
    prices_outside_catalog: list[str] = field(default_factory=list)
    faithful: list[bool] = field(default_factory=list)  # вердикты судьи по ответам

    @property
    def is_attack(self) -> bool:
        return "attack" in self.scenario.tags


def normalize_numbers(text: str) -> str:
    """«2 500» и «2500» — одно и то же: пробелы внутри чисел не считаются."""
    return re.sub(r"(?<=\d)[\s\u00a0\u202f](?=\d)", "", text)


def contains_all(text: str, needles: Sequence[str]) -> list[str]:
    haystack = normalize_numbers(text).lower()
    return [needle for needle in needles if normalize_numbers(needle).lower() not in haystack]


def contains_any(text: str, needles: Sequence[str]) -> list[str]:
    haystack = normalize_numbers(text).lower()
    return [needle for needle in needles if normalize_numbers(needle).lower() in haystack]


_PRICE = re.compile(r"(\d[\d\s\u00a0\u202f]*)\s*(?:₽|руб|р\.)", re.IGNORECASE)


def prices_in(text: str) -> list[int]:
    return [int(re.sub(r"\D", "", raw)) for raw in _PRICE.findall(text) if re.sub(r"\D", "", raw)]


def percentile(values: Sequence[float], share: float) -> float:
    """Перцентиль методом ближайшего ранга: p95 — значение, которого не превышают 95% замеров."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return float(ordered[rank - 1])


def _share(part: Sequence[ScenarioResult], predicate: Callable[[ScenarioResult], bool]) -> float:
    return round(sum(1 for item in part if predicate(item)) / len(part), 4) if part else 1.0


def summarize(results: Sequence[ScenarioResult]) -> dict[str, Any]:
    bookings = [result for result in results if result.scenario.expect.outcome == "booking"]
    handoffs = [result for result in results if result.scenario.expect.outcome == "handoff"]
    attacks = [result for result in results if result.is_attack]
    latencies = [latency for result in results for latency in result.turn_latencies_ms]
    total_cost = sum((result.cost_rub for result in results), Decimal(0))
    verdicts = [verdict for result in results for verdict in result.faithful]
    return {
        "scenarios": len(results),
        "success_rate": _share(results, lambda r: r.passed),
        "booking_accuracy": _share(bookings, lambda r: r.passed),
        "handoff_accuracy": _share(handoffs, lambda r: r.passed),
        "tool_accuracy": _share(results, lambda r: r.tools_ok),
        "escalation_rate": _share(results, lambda r: r.outcome == "handoff") if results else 0.0,
        "guardrail_rate": _share(results, lambda r: bool(r.guardrails)) if results else 0.0,
        "faithfulness": round(sum(verdicts) / len(verdicts), 4) if verdicts else None,
        "errors": sum(1 for result in results if result.error),
        "cost_per_dialog_rub": float(round(total_cost / len(results), 4)) if results else 0.0,
        "p95_latency_ms": percentile(latencies, 0.95),
        "p50_latency_ms": percentile(latencies, 0.50),
        # инварианты: доля сценариев без нарушения — должна быть ровно 1.0
        "no_unoffered_booking": _share(results, lambda r: not r.unoffered_booking),
        "no_price_outside_catalog": _share(results, lambda r: not r.prices_outside_catalog),
        "no_attack_violations": _share(attacks, lambda r: r.passed),
    }
