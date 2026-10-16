"""Настройки приложения из переменных окружения.

Каждая секция читает переменные со своим префиксом: POSTGRES_HOST, LLM_API_KEY и т. д.
Значения берутся из окружения, затем из файла .env; если нет ни там, ни там — значение по умолчанию.
"""

from decimal import Decimal
from functools import lru_cache
from typing import Literal
from urllib.parse import quote

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
    # адрес сервиса снаружи: с него открывается страница салона, и его Origin разрешён веб-чату
    public_url: str = "http://localhost:8000"
    # Только вне прода: средняя задержка фейковых LLM и CRM для нагрузочного теста (adapters/latency.py)
    simulated_latency_ms: int = 0

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
    # Пул на процесс: соединений всего = процессы × (pool_size + max_overflow) — держать ниже
    # max_connections PostgreSQL (100) с запасом на миграции и psql (docs/performance.md)
    pool_size: int = 10
    max_overflow: int = 5
    pool_timeout_s: float = 10.0  # нет свободного соединения 10 с — ошибка, а не бесконечное ожидание

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


def _default_prices() -> dict[str, tuple[Decimal, Decimal]]:
    """₽ за 1000 токенов (вход, выход) — ориентиры; сверять с прайсами провайдеров перед evals."""
    return {
        "qwen3-235b": (Decimal("0.05"), Decimal("0.20")),
        "yandexgpt-lite": (Decimal("0.20"), Decimal("0.20")),
        "yandexgpt-pro": (Decimal("1.20"), Decimal("1.20")),
        "gigachat-2-lite": (Decimal("0.20"), Decimal("0.20")),
        "gigachat-2-max": (Decimal("1.95"), Decimal("1.95")),
        "bge-m3": (Decimal("0.01"), Decimal(0)),
    }


class LLMSettings(EnvSection):
    """Ключи и модели LLM. Клиент создаётся только для провайдера, у которого задан ключ."""

    model_config = SettingsConfigDict(env_prefix="LLM_")

    timeout_s: float = 30.0
    # Свой OpenAI-совместимый сервер (vLLM, Ollama): модель «custom»
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    model: str = ""
    # Cloud.ru Foundation Models: Qwen3-235B
    cloudru_api_key: SecretStr | None = None
    cloudru_base_url: str = "https://foundation-models.api.cloud.ru/v1"
    qwen_model: str = "Qwen/Qwen3-235B-A22B-Instruct-2507"
    # Yandex AI Studio в OpenAI-совместимом режиме
    yandex_api_key: SecretStr | None = None
    yandex_folder_id: str | None = None
    yandex_base_url: str = "https://llm.api.cloud.yandex.net/v1"
    yandex_lite_model: str = "yandexgpt-lite/latest"
    yandex_pro_model: str = "yandexgpt/latest"
    # GigaChat: ключ авторизации (Basic) и корневой сертификат НУЦ Минцифры
    gigachat_auth_key: SecretStr | None = None
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_ca_bundle: str | None = None
    gigachat_lite_model: str = "GigaChat-2"
    gigachat_max_model: str = "GigaChat-2-Max"
    # Эмбеддинги для RAG — в Cloud.ru (OpenAI-совместимый /embeddings); без ключа — офлайн-хеширование.
    # Смена модели требует переиндексации: векторы разных моделей несравнимы
    embeddings_model: str = "BAAI/bge-m3"
    # Цены переопределяются JSON-ом: LLM_PRICES='{"qwen3-235b": [0.05, 0.2]}'
    prices: dict[str, tuple[Decimal, Decimal]] = Field(default_factory=_default_prices)
    # Порядок по умолчанию: основная модель, затем резервные. Салон может задать свой
    default_models: list[str] = Field(default_factory=lambda: ["qwen3-235b", "gigachat-2-lite"])


class YclientsSettings(EnvSection):
    model_config = SettingsConfigDict(env_prefix="YCLIENTS_")

    base_url: str = "https://api.yclients.ru/api/v1"
    partner_token: SecretStr | None = None  # публичные методы онлайн-записи
    user_token: SecretStr | None = None  # закрытые методы: поиск записи после таймаута
    accept: str = "application/vnd.yclients.v2+json"
    timeout_s: float = 10.0
    requests_per_second: float = 5.0  # лимит YCLIENTS с одного IP
    max_search_days: int = 3  # дней с окнами на один поиск: каждый день × мастер — отдельный запрос


class SentrySettings(EnvSection):
    """Sentry или self-hosted GlitchTip на сервере в РФ. Без DSN отправка ошибок выключена."""

    model_config = SettingsConfigDict(env_prefix="SENTRY_")

    dsn: SecretStr | None = None
    environment: str = "local"
    traces_sample_rate: float = 0.0


class LangfuseSettings(EnvSection):
    """Self-hosted Langfuse на сервере в РФ (deploy/docker-compose.langfuse.yml)."""

    model_config = SettingsConfigDict(env_prefix="LANGFUSE_")

    public_key: str | None = None
    secret_key: SecretStr | None = None
    host: str = "http://localhost:3000"
    environment: str = "local"
    sample_rate: float = 1.0

    @property
    def enabled(self) -> bool:
        return bool(self.public_key and self.secret_key)


class RabbitMQSettings(EnvSection):
    """Брокер очереди Celery: входящие сообщения мессенджеров, напоминания, ночные задачи."""

    model_config = SettingsConfigDict(env_prefix="RABBITMQ_")

    host: str = "localhost"
    port: int = 5672
    user: str = "beauty"
    password: SecretStr = SecretStr("beauty")
    vhost: str = "/"

    @property
    def url(self) -> str:
        """amqp://… — логин, пароль и vhost экранируются: «p@ss/word» не ломает адрес."""
        user = quote(self.user, safe="")
        password = quote(self.password.get_secret_value(), safe="")
        return f"amqp://{user}:{password}@{self.host}:{self.port}/{quote(self.vhost, safe='')}"


class SecuritySettings(EnvSection):
    """Ключи шифрования токенов каналов, подписи вебхуков и JWT для API инбокса.

    В проде обязательны (проверяет create_app). Локально, если не заданы, используются
    известные dev-ключи: ими нельзя шифровать настоящие токены.
    """

    model_config = SettingsConfigDict(env_prefix="SECURITY_")

    # Ключи Fernet через запятую: первый шифрует, остальные только расшифровывают (ротация ключа)
    fernet_keys: SecretStr | None = None
    webhook_key: SecretStr | None = None  # из него выводится secret_token вебхука каждого канала
    jwt_secret: SecretStr | None = None
    jwt_ttl_s: int = 15 * 60  # access-токен короткий: утёкший живёт 15 минут
    refresh_ttl_s: int = 30 * 24 * 3600  # refresh-токен меняется при каждом обновлении

    @property
    def configured(self) -> bool:
        return bool(self.fernet_keys and self.webhook_key and self.jwt_secret)


class MaxSettings(EnvSection):
    """Мессенджер MAX: адрес Bot API и корневой сертификат Минцифры, на котором работает сервер API."""

    model_config = SettingsConfigDict(env_prefix="MAX_")

    api_url: str = "https://platform-api2.max.ru"
    ca_bundle: str | None = None  # путь к russian_trusted_root_ca.pem (docs/integrations/max.md)


class AdminBotSettings(EnvSection):
    """Telegram-бот для администраторов салонов: один на весь сервис, салон выбирается привязкой."""

    model_config = SettingsConfigDict(env_prefix="ADMIN_BOT_")

    token: SecretStr | None = None
    username: str | None = None  # для ссылки привязки t.me/<username>?start=<код>

    @property
    def enabled(self) -> bool:
        return self.token is not None


class Settings(BaseModel):
    """Все секции вместе. Каждая секция читает свои переменные при создании."""

    app: AppSettings = Field(default_factory=AppSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    rabbitmq: RabbitMQSettings = Field(default_factory=RabbitMQSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    yclients: YclientsSettings = Field(default_factory=YclientsSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    max_bot: MaxSettings = Field(default_factory=MaxSettings)
    admin_bot: AdminBotSettings = Field(default_factory=AdminBotSettings)


@lru_cache
def get_settings() -> Settings:
    """Настройки читаются один раз; тесты сбрасывают кэш через get_settings.cache_clear()."""
    return Settings()
