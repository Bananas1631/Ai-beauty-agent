"""Ключи REST API салона: сайт, мобильное приложение, партнёрская CRM."""

from datetime import datetime

from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey


class ApiKey(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """В БД — только SHA-256 ключа: ключ случайный (256 бит), подбор по хешу невозможен,
    а медленный хеш (argon2) тут не нужен и замедлил бы каждый запрос. Сам ключ показывается один раз."""

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(100))  # «сайт», «приложение», «CRM партнёра»
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # начало ключа — узнать его в списке, не раскрывая
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    last_used_at: Mapped[datetime | None]
