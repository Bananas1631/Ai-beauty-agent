"""Настройки приложения из переменных окружения.

Каждая секция читает переменные со своим префиксом: POSTGRES_HOST, LLM_API_KEY и т. д.
Значения берутся из окружения, затем из файла .env; если нет ни там, ни там — значение по умолчанию.
"""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

ENV_FILE = ".env"


class EnvSection(BaseSettings):
    """Общие правила для всех секций; префикс переменных задаёт каждая секция сама."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )


class AppSettings(EnvSection):
    model_config = SettingsConfigDict(env_prefix="APP_")

    env: Literal["local", "test", "prod"] = "local"
    log_level: str = "INFO"
    log_json: bool = True
    # сколько /ready ждёт ответа одной зависимости, прежде чем признать её недоступной
    ready_timeout_s: float = 2.0

    @property
    def docs_enabled(self) -> bool:
        """Swagger на /docs — только вне прода: схема API не подсказывает атакующему, куда стучаться."""
        return self.env != "prod"


class PostgresSettings(EnvSection):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_")

    host: str = "localhost"
    port: int = 5432
    user: str = "beauty"
    password: SecretStr = SecretStr("beauty")
    db: str = "beauty"

    @property
    def url(self) -> URL:
        """Адрес БД для SQLAlchemy. URL.create сам экранирует спецсимволы в пароле."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.user,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.db,
        )


class RedisSettings(EnvSection):
    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: SecretStr | None = None


class LLMSettings(EnvSection):
    model_config = SettingsConfigDict(env_prefix="LLM_")

    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    model: str = ""
    timeout_s: float = 30.0


class YclientsSettings(EnvSection):
    model_config = SettingsConfigDict(env_prefix="YCLIENTS_")

    base_url: str = "https://api.yclients.ru/api/v1"
    partner_token: SecretStr | None = None  # публичные методы онлайн-записи
    user_token: SecretStr | None = None  # закрытые методы: поиск записи после таймаута
    accept: str = "application/vnd.yclients.v2+json"
    timeout_s: float = 10.0
    requests_per_second: float = 5.0  # лимит YCLIENTS с одного IP
    max_search_days: int = 3  # дней с окнами на один поиск: каждый день × мастер — отдельный запрос


class Settings(BaseModel):
    """Все секции вместе. Каждая секция читает свои переменные при создании."""

    app: AppSettings = Field(default_factory=AppSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    yclients: YclientsSettings = Field(default_factory=YclientsSettings)


@lru_cache
def get_settings() -> Settings:
    """Настройки читаются один раз; тесты сбрасывают кэш через get_settings.cache_clear()."""
    return Settings()
