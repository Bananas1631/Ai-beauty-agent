"""Прогонщик evals: сценарии параллельно через того же агента, что в проде, на отдельном фейковом CRM.

    poetry run python -m evals.run --dataset booking/v2,attacks/v1 --models offline
    poetry run python -m evals.run --dataset booking/v2 --models qwen3-235b,gigachat-2-lite --judge qwen3-235b

Каждый сценарий получает свой фейковый CRM и свой Redis в памяти: сценарии не мешают друг другу.
«Сейчас» зафиксировано (понедельник, 19 октября 2026, 09:00 МСК) — свободные окна одни и те же
при каждом прогоне. Кроме качества считаются инварианты: запись только на предложенное время,
цены только из каталога, провокации не срабатывают. Отчёт сохраняется в evals/reports/.
"""

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fakeredis import FakeAsyncRedis

from beauty_agent.adapters.crm.fake import FAKE_SALON, FAKE_SERVICES, FakeCRM
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
from beauty_agent.domain.booking_engine.memory import MemoryLedger
from beauty_agent.domain.booking_engine.offers import Offer, PartOfDay
from beauty_agent.domain.booking_engine.service import BookingEntry, BookingService
from evals.judge import Judge, LLMJudge, RuleJudge
from evals.metrics import (
    Outcome,
    Scenario,
    ScenarioResult,
    contains_all,
    contains_any,
    prices_in,
    summarize,
)

ROOT = Path(__file__).parent
DATASETS = ROOT / "datasets"
REPORTS = ROOT / "reports"
TZ = ZoneInfo(FAKE_SALON.timezone)
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=TZ)
SCENARIO_TIMEOUT_S = 180
CATALOG_PRICES = frozenset(
    int(price) for service in FAKE_SERVICES for price in (service.price_min, service.price_max) if price is not None
)
_DATASET_NAME = re.compile(r"^[a-z0-9_]+/v\d+$")


class NullLedger(MemoryLedger):
    async def record(self, entry: BookingEntry) -> None:
        return None


class RecordingBookingService(BookingService):
    """Движок записи, который запоминает все предложенные слоты — для инварианта «только предложенное»."""

    offered: set[str]

    async def propose(self, conversation_id: str, service_id: str, **kwargs: Any) -> list[Offer]:
        offers = await super().propose(conversation_id, service_id, **kwargs)
        self.offered.update(offer.slot.key for offer in offers)
        return offers


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
    if expect.outcome == "no_booking":
        if outcome == "booking":
            reasons.append("запись создана, хотя не должна была")
    elif outcome != expect.outcome:
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
    text = " ".join(answers)
    missing = contains_all(text, expect.must_contain)
    if missing:
        reasons.append(f"в ответе нет: {', '.join(missing)}")
    forbidden = contains_any(text, expect.must_not_contain)
    if forbidden:
        reasons.append(f"в ответе запрещённое: {', '.join(forbidden)}")
    return reasons


def tools_ok(scenario: Scenario, tools: list[str]) -> bool:
    expected_present = all(name in tools for name in scenario.expect.tools)
    no_stray_booking = scenario.expect.outcome == "booking" or "book" not in tools
    return expected_present and no_stray_booking


async def run_scenario(
    scenario: Scenario, llm: LLMClient, *, prompt_version: str, judge_model: Judge | None = None
) -> ScenarioResult:
    redis = FakeAsyncRedis(decode_responses=True)
    crm = FakeCRM(clock=lambda: NOW)
    booking = RecordingBookingService(
        salon_id=f"eval-{scenario.id}", timezone=FAKE_SALON.timezone, provider=crm, redis=redis, ledger=NullLedger()
    )
    booking.offered = set()
    agent = Agent(llm, default_registry(), prompt_version=prompt_version)
    state = DialogState()
    answers: list[str] = []
    tools: list[str] = []
    latencies: list[int] = []
    guardrails: list[str] = []
    faithful: list[bool] = []
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
            tools.extend(item.name for item in result.tool_calls)
            latencies.append(result.latency_ms)
            cost += result.cost_rub
            if result.guardrail:
                guardrails.append(result.guardrail)
            if judge_model is not None and result.guardrail is None and not result.handed_off:
                faithful.append(await judge_model.is_faithful(text, result.text, state.facts))
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
    unoffered = any(request.slot.key not in booking.offered for request, _ in crm.bookings.values())
    outside = sorted({str(price) for answer in answers for price in prices_in(answer) if price not in CATALOG_PRICES})
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
        unoffered_booking=unoffered,
        prices_outside_catalog=outside,
        faithful=faithful,
    )


async def run_model(
    scenarios: Sequence[Scenario],
    llm: LLMClient,
    *,
    concurrency: int,
    prompt_version: str,
    judge_model: Judge | None = None,
) -> list[ScenarioResult]:
    semaphore = asyncio.Semaphore(concurrency)

    async def limited(scenario: Scenario) -> ScenarioResult:
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    run_scenario(scenario, llm, prompt_version=prompt_version, judge_model=judge_model),
                    SCENARIO_TIMEOUT_S,
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


def pick_judge(key: str) -> Judge:
    if key == "rules":
        return RuleJudge()
    clients = build_clients(get_settings().llm)
    if key not in clients:
        print(f"Судья {key} не настроен — использую правила", file=sys.stderr)
        return RuleJudge()
    return LLMJudge(clients[key])


def render_markdown(dataset: str, prompt_version: str, report: dict[str, Any]) -> str:
    lines = [
        f"# Evals: {dataset}, промпт {prompt_version}",
        "",
        f"Прогон {report['started_at']}. «Сейчас» в сценариях: {NOW:%Y-%m-%d %H:%M} МСК, фейковый CRM. "
        f"Судья: {report.get('judge', 'rules')}.",
        "",
        "| Модель | Успех | Точность записи | Эскалации верно | Инструменты | Faithfulness | ₽ за диалог | p95, мс |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model, data in report["models"].items():
        m = data["metrics"]
        faithfulness = "—" if m.get("faithfulness") is None else f"{m['faithfulness']:.0%}"
        lines.append(
            f"| {model} | {m['success_rate']:.0%} | {m['booking_accuracy']:.0%} | {m['handoff_accuracy']:.0%} "
            f"| {m['tool_accuracy']:.0%} | {faithfulness} | {m['cost_per_dialog_rub']:.4f} "
            f"| {m['p95_latency_ms']:.0f} |"
        )
    lines += ["", "Инварианты (должны быть 100%):", ""]
    for model, data in report["models"].items():
        m = data["metrics"]
        lines.append(
            f"- {model}: только предложенное время {m['no_unoffered_booking']:.0%}, "
            f"цены из каталога {m['no_price_outside_catalog']:.0%}, провокации {m['no_attack_violations']:.0%}"
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
        "tags": result.scenario.tags,
        "passed": result.passed,
        "outcome": result.outcome,
        "tools": result.tools,
        "tools_ok": result.tools_ok,
        "cost_rub": str(result.cost_rub),
        "latencies_ms": result.turn_latencies_ms,
        "guardrails": result.guardrails,
        "unoffered_booking": result.unoffered_booking,
        "prices_outside_catalog": result.prices_outside_catalog,
        "faithful": result.faithful,
        "reasons": result.reasons,
        "answers": result.answers,
    }


def load_scenarios(datasets: str, *, tag: str | None = None, limit: int = 0) -> list[Scenario]:
    """Сценарии из нескольких датасетов; limit — на каждый датасет: в быстрый набор PR попадают и провокации."""
    scenarios: list[Scenario] = []
    for name in datasets.split(","):
        chosen = [scenario for scenario in load_dataset(name.strip()) if not tag or tag in scenario.tags]
        scenarios.extend(chosen[:limit] if limit else chosen)
    return scenarios


async def run(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = load_scenarios(args.dataset, tag=args.tag, limit=args.limit)
    judge_model = pick_judge(args.judge)
    report: dict[str, Any] = {
        "dataset": args.dataset,
        "prompt_version": args.prompt,
        "judge": judge_model.name,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "date": date.today().isoformat(),
        "models": {},
    }
    for key, llm in pick_models(args.models.split(",")).items():
        results = await run_model(
            scenarios, llm, concurrency=args.concurrency, prompt_version=args.prompt, judge_model=judge_model
        )
        report["models"][key] = {
            "metrics": summarize(results),
            "scenarios": [scenario_json(result) for result in results],
        }
    return report


def save_report(report: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report['started_at'][:10]}-{report['dataset'].replace('/', '-').replace(',', '+')}"
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (directory / f"{stem}.json").write_text(payload, encoding="utf-8", newline="\n")
    path = directory / f"{stem}.md"
    markdown = render_markdown(report["dataset"], report["prompt_version"], report)
    path.write_text(markdown, encoding="utf-8", newline="\n")
    return path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Прогон evals агента")
    parser.add_argument("--dataset", default="booking/v1", help="через запятую: booking/v2,attacks/v1")
    parser.add_argument("--models", default="offline", help="через запятую: offline, qwen3-235b, gigachat-2-lite…")
    parser.add_argument("--judge", default="rules", help="судья faithfulness: rules или ключ модели")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="только первые N сценариев каждого датасета")
    parser.add_argument("--tag", help="только сценарии с этим тегом")
    parser.add_argument("--report-dir", type=Path, default=REPORTS)
    parser.add_argument("--output", type=Path, help="сохранить JSON отчёта ещё и сюда (для гейта в CI)")
    parser.add_argument("--no-report", action="store_true", help="не сохранять отчёт в evals/reports")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    report = asyncio.run(run(args))
    for model, data in report["models"].items():
        print(model, json.dumps(data["metrics"], ensure_ascii=False))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.no_report and report["models"]:
        print(f"Отчёт: {save_report(report, args.report_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
