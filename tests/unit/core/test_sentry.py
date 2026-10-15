from typing import Any

from pydantic import SecretStr

from beauty_agent.core.config import SentrySettings
from beauty_agent.core.sentry import init_sentry, scrub_breadcrumb, scrub_event


def test_disabled_without_dsn() -> None:
    assert init_sentry(SentrySettings()) is False


def test_event_is_scrubbed() -> None:
    event: dict[str, Any] = {
        "message": "Клиент +7 (916) 150-22-39 не записан",
        "exception": {"values": [{"value": "phone 89161502239, mail anna.p@mail.ru"}]},
        "request": {
            "url": "https://example.test/api/v1/salons/x",
            "data": {"phone": "+79161502239"},
            "cookies": {"session": "s"},
            "headers": {"Authorization": "Bearer secret", "User-Agent": "test"},
        },
    }

    scrubbed = scrub_event(event)

    text = str(scrubbed)
    assert "150-22-39" not in text
    assert "89161502239" not in text
    assert "anna.p@mail.ru" not in text
    assert "+7*******39" in scrubbed["message"]
    assert "data" not in scrubbed["request"]
    assert "cookies" not in scrubbed["request"]
    assert scrubbed["request"]["headers"] == {"User-Agent": "test"}


def test_breadcrumb_is_scrubbed() -> None:
    crumb = {"message": "SELECT ... WHERE phone = '+79161502239'"}

    assert "+79161502239" not in str(scrub_breadcrumb(crumb))


def test_dsn_is_secret() -> None:
    settings = SentrySettings(dsn=SecretStr("https://key@glitchtip.example/1"))

    assert "key@" not in repr(settings)
