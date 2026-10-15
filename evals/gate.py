"""Гейт качества: отчёт evals против порогов evals/thresholds.toml и отчёта ветки main.

    poetry run python -m evals.gate --report evals/out/report.json --baseline evals/reports/baseline.json

Нарушение — код выхода 1: в CI это блокирует слияние PR. Итог пишется и в консоль,
и в $GITHUB_STEP_SUMMARY — его видно прямо на странице прогона.
"""

import argparse
import json
import os
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

THRESHOLDS = Path(__file__).parent / "thresholds.toml"


@dataclass(frozen=True, slots=True)
class Violation:
    model: str
    metric: str
    value: float | None
    limit: float
    kind: str  # invariant | quality | regression

    def text(self) -> str:
        value = "нет данных" if self.value is None else f"{self.value:.4g}"
        return f"{self.model}: {self.metric} = {value}, порог {self.limit:.4g} ({self.kind})"


def load_thresholds(path: Path = THRESHOLDS) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def _quality_for(thresholds: dict[str, Any], model: str) -> dict[str, float]:
    quality = thresholds.get("quality", {})
    base = {name: value for name, value in quality.items() if not isinstance(value, dict)}
    return {**base, **quality.get(model, {})}


def check(
    report: dict[str, Any], thresholds: dict[str, Any], baseline: dict[str, Any] | None = None
) -> list[Violation]:
    violations: list[Violation] = []
    if not report.get("models"):
        return [Violation("—", "models", None, 1, "invariant")]
    for model, data in report["models"].items():
        metrics: dict[str, Any] = data["metrics"]
        for name, required in thresholds.get("invariants", {}).items():
            if metrics.get(name, 0) < required:
                violations.append(Violation(model, name, metrics.get(name), required, "invariant"))
        quality = _quality_for(thresholds, model)
        for name in ("booking_accuracy", "tool_accuracy", "faithfulness"):
            limit = quality.get(f"{name}_min")
            value = metrics.get(name)
            if limit is not None and value is not None and value < limit:
                violations.append(Violation(model, name, value, limit, "quality"))
        for name in ("cost_per_dialog_rub", "p95_latency_ms"):
            limit = quality.get(f"{name}_max")
            value = metrics.get(name)
            if limit is not None and value is not None and value > limit:
                violations.append(Violation(model, name, value, limit, "quality"))
        reference = (baseline or {}).get("models", {}).get(model)
        max_drop = quality.get("booking_accuracy_max_drop")
        if reference and max_drop is not None:
            before = reference["metrics"]["booking_accuracy"]
            after = metrics["booking_accuracy"]
            if before - after > max_drop + 1e-9:
                violations.append(Violation(model, "booking_accuracy (к main)", after, before - max_drop, "regression"))
    return violations


def summary(report: dict[str, Any], violations: Sequence[Violation]) -> str:
    lines = ["## Гейт качества evals", ""]
    for model, data in report.get("models", {}).items():
        m = data["metrics"]
        lines.append(
            f"- **{model}**: запись {m['booking_accuracy']:.0%}, инструменты {m['tool_accuracy']:.0%}, "
            f"инварианты {min(m['no_unoffered_booking'], m['no_price_outside_catalog'], m['no_attack_violations']):.0%}"
        )
    lines.append("")
    lines += [f"- ❌ {violation.text()}" for violation in violations] or ["✅ Все пороги соблюдены"]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Гейт качества evals")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS)
    args = parser.parse_args(argv)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline and args.baseline.exists() else None
    )
    violations = check(report, load_thresholds(args.thresholds), baseline)
    text = summary(report, violations)
    print(text)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as file:
            file.write(text)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
