"""Настройки салона: хранятся в salons.settings (JSONB), проверяются этой моделью при чтении.

Модель и версию промпта можно сначала включить одному салону, а потом всем.
"""

import re
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from beauty_agent.domain.enums import CrmKind

_ORIGIN = re.compile(r"^https://[a-z0-9.-]+(:\d{1,5})?$")


class SalonSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crm_kind: CrmKind = CrmKind.YCLIENTS
    llm_models: list[str] | None = None  # порядок моделей; None — LLM_DEFAULT_MODELS
    prompt_version: str = Field("v1", pattern=r"^v\d{1,3}$")
    daily_llm_budget_rub: Decimal = Field(Decimal(300), ge=0, le=100_000)
    # Сайты салона, на которых разрешён виджет чата (проверка Origin у WebSocket)
    allowed_origins: list[str] = Field(default_factory=list, max_length=10)
    admin_chat_id: str | None = Field(None, max_length=64)  # куда слать уведомления (админ-бот, ноябрь)

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        cleaned = [origin.rstrip("/").lower() for origin in value]
        for origin in cleaned:
            if not _ORIGIN.fullmatch(origin):
                raise ValueError(f"Origin должен быть https://домен без пути: {origin}")
        return cleaned
