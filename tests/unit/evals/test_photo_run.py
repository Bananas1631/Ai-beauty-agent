"""Evals фото-референсов: манифест, подбор по эталону, решение о режиме guess, защита пути к снимкам."""

from pathlib import Path

from tests.photo_helpers import french_manicure, jpeg_with_metadata

from beauty_agent.adapters.llm.vision import ScriptedVision
from evals.photo_run import MIN_ACCURACY, load_cases, photo_path, run_case, summarize


def test_manifest_covers_catalog() -> None:
    cases = load_cases()

    assert len(cases) == 30
    assert len({case.id for case in cases}) == 30
    assert {case.expected_service_id for case in cases} >= {"101", "102", "104", "105", "201", "203", "301", "303"}


async def test_catalog_matching_on_reference_descriptions() -> None:
    """Если модель описала фото верно, подбор по каталогу не должен ошибаться."""
    results = [await run_case(case, None, None) for case in load_cases()]

    metrics = summarize(results)

    assert metrics["accuracy"] >= 0.9
    assert metrics["coverage"] == 1.0


async def test_vision_mistakes_and_unsure_answers(tmp_path: Path) -> None:
    cases = load_cases()[:3]  # три фото маникюра с покрытием (102)
    for case in cases:
        (tmp_path / case.file).write_bytes(jpeg_with_metadata(300, 300))
    vision = ScriptedVision(
        french_manicure(),  # верно
        french_manicure(service="педикюр с покрытием", technique=None),  # ошибка
        french_manicure(confidence=0.2),  # не уверена — администратору
    )

    results = [await run_case(case, vision, tmp_path) for case in cases]
    metrics = summarize(results)

    assert [(result.answered, result.correct) for result in results] == [(True, True), (True, False), (False, False)]
    assert metrics["accuracy"] == 0.5
    assert metrics["coverage"] == round(2 / 3, 4)
    assert not metrics["guess_recommended"]  # точность ниже 80% и мало ответов
    assert MIN_ACCURACY == 0.8


def test_manifest_cannot_escape_photos_dir(tmp_path: Path) -> None:
    assert photo_path(tmp_path, "photo-001.jpg") == (tmp_path / "photo-001.jpg").resolve()
    assert photo_path(tmp_path, "../secret.jpg") is None
