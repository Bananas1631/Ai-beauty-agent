"""Настройки салона: хранятся в salons.settings (JSONB), проверяются этой моделью при чтении.

Модель и версию промпта можно сначала включить одному салону, а потом всем.
"""

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from beauty_agent.domain.enums import CrmKind, ReplyMode

_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(:\d{1,5})?$")


class PrivacyInfo(BaseModel):
    """Реквизиты оператора персональных данных для страницы политики /privacy/{slug}."""

    model_config = ConfigDict(extra="forbid")

    operator_name: str = Field(max_length=300)  # ИП Иванова А. А. или ООО «Лаванда»
    inn: str = Field(pattern=r"^\d{10}(\d{2})?$")
    address: str = Field(max_length=500)
    email: str = Field(max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SalonSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crm_kind: CrmKind = CrmKind.YCLIENTS
    llm_models: list[str] | None = None  # порядок моделей; None — LLM_DEFAULT_MODELS
    prompt_version: str = Field("v1", pattern=r"^v\d{1,3}$")
    daily_llm_budget_rub: Decimal = Field(Decimal(300), ge=0, le=100_000)
    # Сайты салона, на которых разрешён виджет чата (проверка Origin у WebSocket)
    allowed_origins: list[str] = Field(default_factory=list, max_length=10)
    # Пилот начинается в теневом режиме: агент пишет черновики, администратор отправляет их кнопкой
    reply_mode: ReplyMode = ReplyMode.AUTOPILOT
    retention_days: int = Field(365, ge=30, le=1825)  # переписка старше удаляется ночной задачей
    privacy: PrivacyInfo | None = None

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        cleaned = [origin.rstrip("/").lower() for origin in value]
        for origin in cleaned:
            if not _ORIGIN.fullmatch(origin):
                raise ValueError(f"Origin должен быть https://домен без пути: {origin}")
        return cleaned
