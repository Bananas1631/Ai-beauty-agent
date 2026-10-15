"""Гейт качества и судья faithfulness."""

import json
from pathlib import Path
from typing import Any

import pytest

from beauty_agent.adapters.llm.fake import ScriptedLLM, reply
from beauty_agent.domain.errors import LLMBadResponseError
from evals import gate
from evals.judge import LLMJudge, RuleJudge, agreement, load_calibration
from evals.run import load_scenarios, run_model

GOOD: dict[str, Any] = {
    "booking_accuracy": 0.9,
    "tool_accuracy": 0.97,
    "faithfulness": 0.95,
    "cost_per_dialog_rub": 0.8,
    "p95_latency_ms": 9000,
    "no_unoffered_booking": 1.0,
    "no_price_outside_catalog": 1.0,
    "no_attack_violations": 1.0,
}


def report(model: str = "qwen3-235b", /, **overrides: Any) -> dict[str, Any]:
    return {"models": {model: {"metrics": {**GOOD, **overrides}, "scenarios": []}}}


THRESHOLDS = gate.load_thresholds()


def test_good_report_passes() -> None:
    assert gate.check(report(), THRESHOLDS) == []


def test_invariant_is_exactly_one_hundred_percent() -> None:
    violations = gate.check(report(no_price_outside_catalog=0.99), THRESHOLDS)

    assert [(v.metric, v.kind) for v in violations] == [("no_price_outside_catalog", "invariant")]


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("booking_accuracy", 0.8),
        ("tool_accuracy", 0.9),
        ("faithfulness", 0.85),
        ("cost_per_dialog_rub", 2.5),
        ("p95_latency_ms", 16000),
    ],
)
def test_quality_thresholds(metric: str, value: float) -> None:
    violations = gate.check(report(**{metric: value}), THRESHOLDS)

    assert [(v.metric, v.kind) for v in violations] == [(metric, "quality")]


def test_offline_model_has_its_own_quality_floor() -> None:
    assert gate.check(report("offline", booking_accuracy=0.75), THRESHOLDS) == []
    assert gate.check(report("offline", booking_accuracy=0.65), THRESHOLDS)[0].metric == "booking_accuracy"


def test_missing_faithfulness_is_not_a_violation() -> None:
    assert gate.check(report(faithfulness=None), THRESHOLDS) == []


def test_regression_against_main() -> None:
    baseline = report(booking_accuracy=0.93)

    assert gate.check(report(booking_accuracy=0.91), THRESHOLDS, baseline) == []  # −2 п. п. — допустимо
    violations = gate.check(report(booking_accuracy=0.90), THRESHOLDS, baseline)  # −3 п. п.
    assert [v.kind for v in violations] == ["regression"]


def test_empty_report_fails() -> None:
    assert gate.check({"models": {}}, THRESHOLDS)


def test_cli_exit_code_and_step_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good, bad, summary = tmp_path / "good.json", tmp_path / "bad.json", tmp_path / "summary.md"
    good.write_text(json.dumps(report()), encoding="utf-8")
    bad.write_text(json.dumps(report(no_attack_violations=0.9)), encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert gate.main(["--report", str(good)]) == 0
    assert gate.main(["--report", str(bad), "--baseline", str(tmp_path / "missing.json")]) == 1
    text = summary.read_text(encoding="utf-8")
    assert "Все пороги соблюдены" in text
    assert "no_attack_violations" in text


# ---------------------------------------------------------------- судья


async def test_rule_judge_agreement_is_documented() -> None:
    share, mismatched = await agreement(RuleJudge(), load_calibration())

    # Правила видят только цены и время: выдуманные скидки, правила и адреса — нет.
    # Поэтому faithfulness в гейте должен считать LLM-судья (порог согласия 85%)
    assert share == pytest.approx(0.7)
    assert len(mismatched) == 15


async def test_llm_judge_parses_wrapped_json() -> None:
    llm = ScriptedLLM([reply('Вердикт: {"faithful": false, "reason": "придумана скидка"} — вот так')])

    verdict = await LLMJudge(llm).verdict("Есть скидки?", "Скидка 10%!", [])

    assert verdict.faithful is False
    assert verdict.reason == "придумана скидка"
    _messages, _ = llm.calls[0]
    assert "faithfulness" in (_messages[0].content or "")


async def test_llm_judge_retries_once_then_fails() -> None:
    llm = ScriptedLLM([reply("не JSON"), reply('{"faithful": true, "reason": "ok"}')])
    assert await LLMJudge(llm).is_faithful("q", "a", [])

    broken = ScriptedLLM([reply("не JSON"), reply("{}")])
    with pytest.raises(LLMBadResponseError):
        await LLMJudge(broken).verdict("q", "a", [])


async def test_runner_collects_judge_verdicts() -> None:
    from beauty_agent.adapters.llm.offline import OfflineLLM

    scenarios = load_scenarios("booking/v2", tag="price", limit=3)
    results = await run_model(scenarios, OfflineLLM(), concurrency=2, prompt_version="v1", judge_model=RuleJudge())

    assert all(result.faithful for result in results)
    assert all(not result.prices_outside_catalog for result in results)


def test_datasets_sizes() -> None:
    assert len(load_scenarios("booking/v2")) == 100
    attacks = load_scenarios("attacks/v1")
    assert len(attacks) == 30
    assert all("attack" in scenario.tags for scenario in attacks)
    assert len(load_calibration()) == 50
