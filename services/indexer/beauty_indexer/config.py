"""Настройки индексатора: свои (INDEXER_*) и общие с основным сервисом секции окружения.

PostgreSQL, Kafka и ключи эмбеддингов читаются теми же переменными (POSTGRES_*, KAFKA_*, LLM_*):
на сервере у сервисов один .env, но в проде у индексатора своя роль PostgreSQL с правами только на
схему knowledge (docs/adr/014-indexer-service.md).
"""

from functools import lru_cache

from pydantic import BaseModel, Field
from pydantic_settings import SettingsConfigDict

from beauty_agent.core.config import (
    AppSettings,
    EnvSection,
    KafkaSettings,
    LLMSettings,
    PostgresSettings,
    SentrySettings,
)


class IndexerSettings(EnvSection):
    model_config = SettingsConfigDict(env_prefix="INDEXER_")

    # Открытые ключи основного сервиса: им подписаны токены, которые приходят в API индексатора
    jwks_url: str = "http://localhost:8000/.well-known/jwks.json"
    issuer: str = "beauty-agent"
    consumer_group: str = "indexer"
    max_pages: int = Field(30, ge=1, le=200)
    pause_s: float = Field(0.5, ge=0, le=10)  # вежливый обход: пауза между страницами сайта
    # Задача «running» дольше этого — воркер упал посреди обхода: задачу берёт другой
    stale_job_s: int = Field(30 * 60, ge=60)
    job_poll_s: float = Field(1.0, gt=0, le=60)


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    indexer: IndexerSettings = Field(default_factory=IndexerSettings)


@lru_cache
def get_settings() -> Settings:
    return Settings()
