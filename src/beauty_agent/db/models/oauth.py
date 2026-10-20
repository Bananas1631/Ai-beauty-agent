"""Клиенты OAuth2 (client credentials): партнёрские интеграции и внутренние сервисы."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, Timestamps, UUIDPrimaryKey


class OAuthClient(UUIDPrimaryKey, Timestamps, Base):
    """Секрет — только хеш argon2id: украденная база не даёт войти от имени партнёра.

    salon_id есть у партнёра (CRM, сайт, приложение салона): его токен работает только с этим
    салоном. У внутренних сервисов (админка, биллинг) салона нет — их права ограничены scopes.
    webhook_url — куда слать подписанные события о записях этого салона (adapters/webhooks_out.py).
    """

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    secret_hash: Mapped[str] = mapped_column(String(200))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    salon_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("salons.id", ondelete="RESTRICT"), index=True)
    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    last_used_at: Mapped[datetime | None]
