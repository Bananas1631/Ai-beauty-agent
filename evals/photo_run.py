"""Evals фото-референсов: угадывает ли агент услугу по фото «хочу вот так».

    poetry run python -m evals.photo_run --photos-dir D:/private-evals/photos     # модель зрения
    poetry run python -m evals.photo_run --offline                                # только подбор по каталогу

Манифест evals/datasets/photos/v1.jsonl: 30 случаев по каталогу фейкового салона — что должно быть
на фото, ожидаемая услуга и эталонное описание. Сами фото в репозиторий не кладутся (права на
снимки, лица): папка со снимками передаётся --photos-dir (как собрать — README.md рядом с манифестом).

Конвейер тот же, что в проде: подготовка фото (media/images.py) → модель зрения → решение «угадывать
или администратору» (conversation/photo.py) → подбор услуги по каталогу (agent/tools.py: match_photo).

- accuracy — доля верной первой услуги среди фото, на которые агент ответил бы сам;
- coverage — доля фото, на которые агент ответил бы сам (остальные — администратору);
- --offline — вместо модели эталонные описания: проверка подбора по каталогу без сети.

Режим салона photo_mode=guess включают, только если accuracy ≥ 80% (ADR 012); отчёт пишет рекомендацию.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from beauty_agent.adapters.crm.fake import FAKE_SERVICES
from beauty_agent.adapters.llm.models import build_vision
from beauty_agent.adapters.llm.vision import PhotoDescription, VisionClient
from beauty_agent.agent.tools import match_photo
from beauty_agent.conversation.photo import handoff_reason
from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.domain.errors import DomainError
from beauty_agent.media.images import prepare_image

ROOT = Path(__file__).parent
MANIFEST = ROOT / "datasets" / "photos" / "v1.jsonl"
REPORTS = ROOT / "reports"
MIN_ACCURACY = 0.8
MIN_ANSWERED = 10  # меньше — выборка слишком мала, чтобы доверять точности


class PhotoCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    file: str
    expected_service_id: str
    shows: str
    reference: PhotoDescription


@dataclass
class PhotoResult:
    case: PhotoCase
    answered: bool  # агент ответил бы сам, а не передал администратору
    correct: bool
    top: list[str]
    reason: str | None = None
    cost_rub: Decimal = Decimal(0)


def load_cases(path: Path = MANIFEST) -> list[PhotoCase]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [PhotoCase.model_validate_json(line) for line in lines if line.strip()]


def judge(case: PhotoCase, description: PhotoDescription, cost_rub: Decimal = Decimal(0)) -> PhotoResult:
    reason = handoff_reason(description)
    if reason is not None:
        return PhotoResult(case, answered=False, correct=False, top=[], reason=reason, cost_rub=cost_rub)
    top = [service.external_id for service in match_photo(description, list(FAKE_SERVICES))]
    correct = bool(top) and top[0] == case.expected_service_id
    return PhotoResult(case, answered=bool(top), correct=correct, top=top, cost_rub=cost_rub)


def photo_path(photos_dir: Path, file: str) -> Path | None:
    """Путь к снимку; None — имя из манифеста выводит за папку снимков (../)."""
    path = (photos_dir / file).resolve()
    return path if photos_dir.resolve() in path.parents else None


async def run_case(case: PhotoCase, vision: VisionClient | None, photos_dir: Path | None) -> PhotoResult:
    if vision is None or photos_dir is None:
        return judge(case, case.reference)
    path = photo_path(photos_dir, case.file)
    if path is None:
        return PhotoResult(case, False, False, [], reason="файл вне папки снимков")
    try:
        result = await vision.describe(prepare_image(await asyncio.to_thread(path.read_bytes)))
    except (OSError, DomainError) as exc:
        return PhotoResult(case, False, False, [], reason=f"{type(exc).__name__}: {exc}")
    return judge(case, result.description, result.cost_rub)


def summarize(results: Sequence[PhotoResult]) -> dict[str, Any]:
    answered = [result for result in results if result.answered]
    accuracy = round(sum(result.correct for result in answered) / len(answered), 4) if answered else 0.0
    return {
        "photos": len(results),
        "answered": len(answered),
        "coverage": round(len(answered) / len(results), 4) if results else 0.0,
        "accuracy": accuracy,
        "top3_accuracy": round(
            sum(result.case.expected_service_id in result.top for result in answered) / len(answered), 4
        )
        if answered
        else 0.0,
        "cost_rub": float(sum((result.cost_rub for result in results), Decimal(0))),
        "guess_recommended": accuracy >= MIN_ACCURACY and len(answered) >= MIN_ANSWERED,
    }


def render(metrics: dict[str, Any], results: Sequence[PhotoResult], mode: str) -> str:
    verdict = (
        "можно включать photo_mode=guess"
        if metrics["guess_recommended"]
        else "оставить photo_mode=admin (точность ниже 80% или мало ответов)"
    )
    lines = [
        f"# Evals фото-референсов ({mode})",
        "",
        f"Фото: {metrics['photos']}, агент ответил бы сам: {metrics['answered']} ({metrics['coverage']:.0%}).",
        f"Точность первой услуги: {metrics['accuracy']:.0%}, в первых трёх: {metrics['top3_accuracy']:.0%}.",
        f"Решение: {verdict}.",
        "",
        "| Фото | Ожидалась | Подобрано | Итог |",
        "|---|---|---|---|",
    ]
    for result in results:
        outcome = "верно" if result.correct else ("администратору" if not result.answered else "ошибка")
        detail = result.reason or ", ".join(result.top) or "—"
        lines.append(
            f"| {result.case.id} ({result.case.shows}) | {result.case.expected_service_id} | {detail} | {outcome} |"
        )
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], list[PhotoResult]]:
    cases = load_cases()
    vision = None if args.offline else build_vision(get_settings().llm)
    if not args.offline and (vision is None or args.photos_dir is None):
        raise SystemExit(
            "Нужны --photos-dir и ключ модели зрения (LLM_CLOUDRU_API_KEY или LLM_VISION_*); или --offline"
        )
    results = [await run_case(case, vision, args.photos_dir) for case in cases]
    return summarize(results), results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evals фото-референсов")
    parser.add_argument("--photos-dir", type=Path, help="папка со снимками вне репозитория")
    parser.add_argument("--offline", action="store_true", help="эталонные описания вместо модели зрения")
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    metrics, results = asyncio.run(run(args))
    print(json.dumps(metrics, ensure_ascii=False))
    if not args.no_report:
        mode = "офлайн: эталонные описания" if args.offline else "модель зрения"
        stem = f"{datetime.now(UTC):%Y-%m-%d}-photos-v1{'-offline' if args.offline else ''}"
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / f"{stem}.md").write_text(render(metrics, results, mode), encoding="utf-8", newline="\n")
        print(f"Отчёт: {REPORTS / f'{stem}.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
