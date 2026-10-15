"""Салон и подключённые к нему каналы. К салону привязаны все остальные данные."""

from typing import Any

from sqlalchemy import (
    CheckConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import Channel
from beauty_agent.domain.salon_settings import SalonSettings


class Salon(UUIDPrimaryKey, Timestamps, Base):
    """Салон — арендатор системы: у каждого свои клиенты, мастера и записи."""

    __tablename__ = "salons"
    __table_args__ = (CheckConstraint("slug ~ '^[a-z0-9-]+$'", name="slug_format"),)

    slug: Mapped[str] = mapped_column(String(64), unique=True)  # /s/studio-anna
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), server_default="Europe/Moscow")
    yclients_company_id: Mapped[int | None] = mapped_column(unique=True)  # ID филиала
    is_demo: Mapped[bool] = mapped_column(server_default=false())
    # Настройки салона: модель LLM, версия промпта, лимит расходов (domain/salon_settings.py)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    @property
    def config(self) -> SalonSettings:
        """Настройки, проверенные Pydantic: опечатка в JSONB не уйдёт дальше этой строки."""
        return SalonSettings.model_validate(self.settings or {})

    channel_accounts: Mapped[list["ChannelAccount"]] = relationship(
        back_populates="salon",
        lazy="raise",
    )


class ChannelAccount(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Подключённый мессенджер салона: бот Telegram, сообщество VK, бот MAX, виджет."""

    __tablename__ = "channel_accounts"
    __table_args__ = (UniqueConstraint("channel", "external_id"),)

    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"))
    external_id: Mapped[str] = mapped_column(String(128))  # ID бота или сообщества
    # токен бота в зашифрованном виде; шифрование появится в ноябре (core/security.py)
    token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    is_active: Mapped[bool] = mapped_column(server_default=true())

    salon: Mapped[Salon] = relationship(back_populates="channel_accounts", lazy="raise")
