"""Правила согласия по 152-ФЗ: распознавание согласия и отзыва, вырезание телефона и e-mail."""

import pytest

from beauty_agent.domain.consent import (
    ConsentIntent,
    ConsentState,
    allows_personal_data,
    consent_request,
    detect_intent,
    strip_personal_data,
)


@pytest.mark.parametrize("text", ["Согласна", "согласен!", "Да, согласен.", "Даю согласие", "/agree", "Я согласна"])
def test_grant(text: str) -> None:
    assert detect_intent(text) is ConsentIntent.GRANT


@pytest.mark.parametrize(
    "text",
    [
        "/forget",
        "Отзываю согласие",
        "отзываю своё согласие",
        "удалите, пожалуйста, мои данные",
        "Удалите мою переписку",
    ],
)
def test_withdraw(text: str) -> None:
    assert detect_intent(text) is ConsentIntent.WITHDRAW


@pytest.mark.parametrize(
    "text",
    ["согласна на 15:00", "Согласна, но лучше в пятницу", "хочу удалить запись", "отмените мою запись", "да"],
)
def test_not_consent(text: str) -> None:
    # «согласна на 15:00» — выбор времени, а не согласие на обработку данных
    assert detect_intent(text) is ConsentIntent.NONE


def test_strip_personal_data() -> None:
    text = "Анна, +7 (916) 150-22-39, 8 916 1502239, anna.k@mail.ru. Цены 1500 2000, в 15:00"

    stripped = strip_personal_data(text)

    assert "916" not in stripped
    assert "anna.k@mail.ru" not in stripped
    assert stripped.count("[телефон скрыт]") == 2
    assert "1500 2000" in stripped  # цены и время — не телефон
    assert "15:00" in stripped


def test_only_granted_allows_personal_data() -> None:
    assert allows_personal_data(ConsentState.GRANTED)
    assert not allows_personal_data(ConsentState.NONE)
    assert not allows_personal_data(ConsentState.WITHDRAWN)


def test_consent_request_has_policy_link() -> None:
    text = consent_request("https://agent.example/privacy/lavanda")

    assert "https://agent.example/privacy/lavanda" in text
    assert "Согласен" in text
    assert "/forget" in text
