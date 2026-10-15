import json
import logging
from typing import Any

import pytest
import structlog

from beauty_agent.core.logging import configure_logging, mask_text


def read_json_line(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    line = capsys.readouterr().out.strip()
    data: dict[str, Any] = json.loads(line)
    return data


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+7 (916) 150-22-39", "+7*******39"),
        ("89161502239", "+7*******39"),
        ("7 916 150 22 39", "+7*******39"),
        ("пишите на anna.p@mail.ru", "пишите на a***@mail.ru"),
        ("запись 2820023", "запись 2820023"),
        ("trace a3f81234567890bc4d5e6f7081928374", "trace a3f81234567890bc4d5e6f7081928374"),
        ("2026-10-05T12:00:00+03:00", "2026-10-05T12:00:00+03:00"),
    ],
)
def test_mask_text(text: str, expected: str) -> None:
    assert mask_text(text) == expected


def test_json_log_with_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    structlog.contextvars.bind_contextvars(trace_id="t-1", salon_id="s-1")

    structlog.get_logger("booking").info("booking_created", service="Стрижка")

    data = read_json_line(capsys)
    assert data["event"] == "booking_created"
    assert data["trace_id"] == "t-1"
    assert data["salon_id"] == "s-1"
    assert data["level"] == "info"
    assert data["logger"] == "booking"
    assert data["service"] == "Стрижка"
    assert "timestamp" in data


def test_personal_data_is_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    structlog.get_logger().info(
        "client_message",
        text="Мой номер +7 (916) 150-22-39, почта anna.p@mail.ru",
        client={"phones": ["89161502239"]},
        record_id=2820023,
    )

    data = read_json_line(capsys)
    assert data["text"] == "Мой номер +7*******39, почта a***@mail.ru"
    assert data["client"] == {"phones": ["+7*******39"]}
    assert data["record_id"] == 2820023  # числа не трогаем


def test_stdlib_loggers_use_same_format(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()

    logging.getLogger("uvicorn.error").info("Started server process [%s]", 1234)

    data = read_json_line(capsys)
    assert data["event"] == "Started server process [1234]"
    assert data["logger"] == "uvicorn.error"


def test_exception_text_is_masked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    log = structlog.get_logger()

    try:
        raise ValueError("bad phone 89161502239")
    except ValueError:
        log.exception("booking_failed")

    data = read_json_line(capsys)
    assert "ValueError: bad phone +7*******39" in data["exception"]


def test_console_format_for_local_runs(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(json_logs=False)

    structlog.get_logger().info("console_view")

    out = capsys.readouterr().out
    assert "console_view" in out
    assert not out.startswith("{")
