import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from beauty_agent.core.config import PostgresSettings, get_settings

PREFIXES = ("APP_", "POSTGRES_", "REDIS_", "LLM_", "YCLIENTS_")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Тест не видит ни переменных компьютера, ни файла .env проекта."""
    for name in list(os.environ):
        if name.startswith(PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)  # .env ищется в текущей папке, а здесь его нет
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_without_environment() -> None:
    settings = get_settings()

    assert settings.app.env == "local"
    assert settings.postgres.host == "localhost"
    assert settings.postgres.port == 5432
    assert settings.yclients.base_url == "https://api.yclients.ru/api/v1"


def test_reads_prefixed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("LLM_API_KEY", "llm-key")

    settings = get_settings()

    assert settings.postgres.host == "db"
    assert settings.postgres.port == 6543  # строка из окружения превратилась в int
    assert settings.llm.api_key.get_secret_value() == "llm-key"


def test_reads_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("REDIS_HOST=cache\nPOSTGRES_PORT=\n", encoding="utf-8")

    settings = get_settings()

    assert settings.redis.host == "cache"
    assert settings.postgres.port == 5432  # пустое значение — берётся значение по умолчанию


def test_environment_beats_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("REDIS_HOST=from-file\n", encoding="utf-8")
    monkeypatch.setenv("REDIS_HOST", "from-env")

    assert get_settings().redis.host == "from-env"


def test_secrets_are_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YCLIENTS_PARTNER_TOKEN", "partner-secret")
    monkeypatch.setenv("POSTGRES_PASSWORD", "db-secret")

    settings = get_settings()

    assert "partner-secret" not in repr(settings)
    assert "db-secret" not in repr(settings)
    assert str(settings.yclients.partner_token) == "**********"


def test_database_url_escapes_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd")

    url = PostgresSettings().url

    assert url.password == "p@ss:w/rd"
    assert "p%40ss%3Aw%2Frd" in url.render_as_string(hide_password=False)
    assert "p@ss" not in str(url)  # при печати пароль скрыт: ***


def test_invalid_value_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError):
        get_settings()


def test_settings_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    monkeypatch.setenv("REDIS_HOST", "changed")

    assert get_settings() is first  # кэш: переменная окружения ещё не прочитана
    get_settings.cache_clear()
    assert get_settings().redis.host == "changed"
