"""Обезличивание реальных диалогов и запрет писать их в репозиторий."""

import uuid
from pathlib import Path

import pytest

from beauty_agent.rag.history_export import anonymize, scenario_id
from scripts.export_dialogs import REPOSITORY, UnsafeOutputError, check_output


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Меня зовут Анна, телефон +7 916 150-22-39", "Меня зовут [имя], телефон [телефон скрыт]"),
        ("Запишите Анну Петрову", "Запишите [имя] [имя]"),
        ("пишите на anna@mail.ru", "пишите на [e-mail скрыт]"),
        ("Маникюр за 2500 ₽", "Маникюр за 2500 ₽"),
    ],
)
def test_anonymize(text: str, expected: str) -> None:
    assert anonymize(text, ["Анна Петрова"]) == expected


def test_scenario_id_depends_on_secret_salt() -> None:
    conversation = uuid.uuid4()

    assert scenario_id(conversation, "a" * 16) == scenario_id(conversation, "a" * 16)
    assert scenario_id(conversation, "a" * 16) != scenario_id(conversation, "b" * 16)
    assert str(conversation) not in scenario_id(conversation, "a" * 16)


def test_output_inside_repository_is_refused(tmp_path: Path) -> None:
    for inside in (REPOSITORY / "evals" / "datasets" / "real.jsonl", REPOSITORY / ".." / REPOSITORY.name / "x.jsonl"):
        with pytest.raises(UnsafeOutputError):
            check_output(inside)
    assert check_output(tmp_path / "real.jsonl") == (tmp_path / "real.jsonl").resolve()
