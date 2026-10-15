"""Прогонщик evals: сценарии параллельно через того же агента, что в проде, на отдельном фейковом CRM.

    poetry run python -m evals.run --dataset booking/v1 --models offline
    poetry run python -m evals.run --dataset booking/v1 --models qwen3-235b,gigachat-2-lite,yandexgpt-lite

Каждый сценарий получает свой фейковый CRM и свой Redis в памяти: сценарии не мешают друг другу.
«Сейчас» зафиксировано (понедельник, 19 октября 2026, 09:00 МСК) — свободные окна одни и те же
при каждом прогоне. Отчёт «точность / цена / задержка» сохраняется в evals/reports/.
"""

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fakeredis import FakeAsyncRedis

from beauty_agent.adapters.crm.fake import FAKE_SALON, FakeCRM
from beauty_agent.adapters.llm.base import LLMClient
from beauty_agent.adapters.llm.models import build_clients
from beauty_agent.adapters.llm.offline import OfflineLLM
from beauty_agent.agent.loop import Agent
from beauty_agent.agent.prompts import DEFAULT_PROMPT_VERSION
from beauty_agent.agent.registry import ToolContext
from beauty_agent.agent.state import DialogState
from beauty_agent.agent.tools import CardSalonInfo, ProviderCatalog, default_registry
from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.domain.booking_engine.offers import PartOfDay
from beauty_agent.domain.booking_engine.service import BookingEntry, BookingService
from evals.metrics import Outcome, Scenario, ScenarioResult, contains_all, summarize

ROOT = Path(__file__).parent
DATASETS = ROOT / "datasets"
REPORTS = ROOT / "reports"
TZ = ZoneInfo(FAKE_SALON.timezone)
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
SCENARIO_TIMEOUT_S = 180
_DATASET_NAME = re.compile(r"^[a-z0-9_]+/v\d+$")


class NullLedger:
    async def record(self, entry: BookingEntry) -> None:
        return None


def load_dataset(name: str) -> list[Scenario]:
    if not _DATASET_NAME.fullmatch(name):  # имя из командной строки — не путь к произвольному файлу
        raise ValueError(f"Имя датасета вида booking/v1, получено {name!r}")
    lines = (DATASETS / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    scenarios = [Scenario.model_validate_json(line) for line in lines if line.strip()]
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError(f"В {name} повторяются id сценариев")
    return scenarios


def judge(scenario: Scenario, outcome: Outcome, crm: FakeCRM, answers: list[str], tools: list[str]) -> list[str]:
    """Почему сценарий не пройден; пустой список — пройден."""
    expect = scenario.expect
    reasons: list[str] = []
    if outcome != expect.outcome:
        reasons.append(f"итог {outcome}, ожидался {expect.outcome}")
    if expect.outcome == "booking" and crm.bookings:
        if len(crm.bookings) != 1:
            reasons.append(f"записей {len(crm.bookings)}, ожидалась одна")
        request, _ = next(iter(crm.bookings.values()))
        slot = request.slot
        local = slot.starts_at.astimezone(TZ)
        if expect.service_id and slot.service_id != expect.service_id:
            reasons.append(f"услуга {slot.service_id}, ожидалась {expect.service_id}")
        if expect.master_id and slot.master_id != expect.master_id:
            reasons.append(f"мастер {slot.master_id}, ожидался {expect.master_id}")
        if expect.part_of_day and PartOfDay.of(local) != expect.part_of_day:
            reasons.append(f"время {local:%H:%M}, ожидалось {expect.part_of_day.value}")
        if expect.weekday is not None and local.weekday() != expect.weekday:
            reasons.append(f"день недели {local.weekday()}, ожидался {expect.weekday}")
    missing = contains_all(" ".join(answers), expect.must_contain)
    if missing:
        reasons.append(f"в ответе нет: {', '.join(missing)}")
    return reasons


def tools_ok(scenario: Scenario, tools: list[str]) -> bool:
    expected_present = all(name in tools for name in scenario.expect.tools)
    no_stray_booking = scenario.expect.outcome == "booking" or "book" not in tools
    return expected_present and no_stray_booking


async def run_scenario(scenario: Scenario, llm: LLMClient, *, prompt_version: str) -> ScenarioResult:
    redis = FakeAsyncRedis(decode_responses=True)
    crm = FakeCRM(clock=lambda: NOW)
    booking = BookingService(
        salon_id=f"eval-{scenario.id}", timezone=FAKE_SALON.timezone, provider=crm, redis=redis, ledger=NullLedger()
    )
    agent = Agent(llm, default_registry(), prompt_version=prompt_version)
    state = DialogState()
    answers: list[str] = []
    tools: list[str] = []
    latencies: list[int] = []
    guardrails: list[str] = []
    cost = Decimal(0)
    outcome: Outcome = "answer"
    error: str | None = None
    try:
        for text in scenario.messages:
            ctx = ToolContext(
                salon_id=f"eval-{scenario.id}",
                salon_name=FAKE_SALON.name,
                conversation_id=f"eval-{scenario.id}",
                now=NOW,
                booking=booking,
                catalog=ProviderCatalog(booking),
                salon_info=CardSalonInfo(crm),
            )
            result = await agent.run(text, ctx, state)
            answers.append(result.text)
            tools.extend(outcome.name for outcome in result.tool_calls)
            latencies.append(result.latency_ms)
            cost += result.cost_rub
            if result.guardrail:
                guardrails.append(result.guardrail)
            if result.handed_off:
                outcome = "handoff"
                break
    except Exception as exc:  # сценарий с ошибкой — не пройден, прогон продолжается
        error = f"{type(exc).__name__}: {exc}"
    finally:
        await redis.aclose()
    if crm.bookings:
        outcome = "booking"
    reasons = judge(scenario, outcome, crm, answers, tools)
    if error:
        reasons.append(error)
    return ScenarioResult(
        scenario=scenario,
        passed=not reasons,
        outcome=outcome,
        answers=answers,
        tools=tools,
        tools_ok=tools_ok(scenario, tools),
        cost_rub=cost,
        turn_latencies_ms=latencies,
        guardrails=guardrails,
        reasons=reasons,
        error=error,
    )


async def run_model(
    scenarios: Sequence[Scenario], llm: LLMClient, *, concurrency: int, prompt_version: str
) -> list[ScenarioResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(scenario: Scenario) -> ScenarioResult:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    run_scenario(scenario, llm, prompt_version=prompt_version), SCENARIO_TIMEOUT_S
                )
            except TimeoutError:
                return ScenarioResult(scenario, False, "answer", [], [], False, Decimal(0), [], reasons=["таймаут"])

    return list(await asyncio.gather(*(limited(scenario) for scenario in scenarios)))


def pick_models(keys: Sequence[str]) -> dict[str, LLMClient]:
    clients = build_clients(get_settings().llm)
    chosen: dict[str, LLMClient] = {}
    for key in keys:
        if key == "offline":
            chosen[key] = OfflineLLM()
        elif key in clients:
            chosen[key] = clients[key]
        else:
            print(f"Модель {key} не настроена (нет ключа) — пропускаю", file=sys.stderr)
    return chosen


def render_markdown(dataset: str, prompt_version: str, report: dict[str, Any]) -> str:
    lines = [
        f"# Evals: {dataset}, промпт {prompt_version}",
        "",
        f"Прогон {report['started_at']}. «Сейчас» в сценариях: {NOW:%Y-%m-%d %H:%M} МСК, фейковый CRM.",
        "",
        "| Модель | Успех | Точность записи | Эскалации верно | Инструменты | Доля эскалаций | ₽ за диалог | p95, мс |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model, data in report["models"].items():
        m = data["metrics"]
        lines.append(
            f"| {model} | {m['success_rate']:.0%} | {m['booking_accuracy']:.0%} | {m['handoff_accuracy']:.0%} "
            f"| {m['tool_accuracy']:.0%} | {m['escalation_rate']:.0%} | {m['cost_per_dialog_rub']:.4f} "
            f"| {m['p95_latency_ms']:.0f} |"
        )
    for model, data in report["models"].items():
        failed = [item for item in data["scenarios"] if not item["passed"]]
        if failed:
            lines += ["", f"## Не пройдены: {model} ({len(failed)})", ""]
            lines += [f"- `{item['id']}`: {'; '.join(item['reasons'])}" for item in failed]
    return "\n".join(lines) + "\n"


def scenario_json(result: ScenarioResult) -> dict[str, Any]:
    return {
        "id": result.scenario.id,
        "passed": result.passed,
        "outcome": result.outcome,
        "tools": result.tools,
        "tools_ok": result.tools_ok,
        "cost_rub": str(result.cost_rub),
        "latencies_ms": result.turn_latencies_ms,
        "guardrails": result.guardrails,
        "reasons": result.reasons,
        "answers": result.answers,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = load_dataset(args.dataset)
    if args.tag:
        scenarios = [scenario for scenario in scenarios if args.tag in scenario.tags]
    if args.limit:
        scenarios = scenarios[: args.limit]
    report: dict[str, Any] = {
        "dataset": args.dataset,
        "prompt_version": args.prompt,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "models": {},
    }
    for key, llm in pick_models(args.models.split(",")).items():
        results = await run_model(scenarios, llm, concurrency=args.concurrency, prompt_version=args.prompt)
        report["models"][key] = {
            "metrics": summarize(results),
            "scenarios": [scenario_json(result) for result in results],
        }
    return report


def save_report(report: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report['started_at'][:10]}-{report['dataset'].replace('/', '-')}"
    (directory / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    path = directory / f"{stem}.md"
    path.write_text(render_markdown(report["dataset"], report["prompt_version"], report), encoding="utf-8")
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Прогон evals агента")
    parser.add_argument("--dataset", default="booking/v1")
    parser.add_argument("--models", default="offline", help="через запятую: offline, qwen3-235b, gigachat-2-lite…")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="только первые N сценариев")
    parser.add_argument("--tag", help="только сценарии с этим тегом")
    parser.add_argument("--report-dir", type=Path, default=REPORTS)
    parser.add_argument("--no-report", action="store_true", help="не сохранять отчёт")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    report = asyncio.run(run(args))
    for model, data in report["models"].items():
        print(model, json.dumps(data["metrics"], ensure_ascii=False))
    if not args.no_report and report["models"]:
        print(f"Отчёт: {save_report(report, args.report_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
