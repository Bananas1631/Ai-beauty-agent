"""Провайдер CRM для салона по его настройкам: YCLIENTS, демо (имитация записи) или фейковый CRM.

Провайдер создаётся один раз на салон и переиспользуется: у адаптера YCLIENTS свой пул
соединений, ограничитель частоты и circuit breaker — они должны жить дольше одного сообщения.
"""

import uuid

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.adapters.crm.demo import DemoProvider
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.crm.yclients import YclientsProvider
from beauty_agent.core.config import YclientsSettings
from beauty_agent.core.resilience import RateLimiter
from beauty_agent.db.models import Salon
from beauty_agent.domain.enums import CrmKind
from beauty_agent.domain.errors import DomainValidationError


class ProviderRegistry:
    def __init__(self, settings: YclientsSettings, *, limiter: RateLimiter | None = None) -> None:
        self.settings = settings
        self.limiter = limiter  # общий лимит YCLIENTS на процесс (с ноября — в Redis на все воркеры)
        self._providers: dict[tuple[uuid.UUID, CrmKind], BookingProvider] = {}
        self._yclients: list[YclientsProvider] = []

    def for_salon(self, salon: Salon) -> BookingProvider:
        kind = salon.config.crm_kind
        cached = self._providers.get((salon.id, kind))
        if cached is not None:
            return cached
        provider = self._create(salon, kind)
        self._providers[(salon.id, kind)] = provider
        return provider

    def _create(self, salon: Salon, kind: CrmKind) -> BookingProvider:
        if kind is CrmKind.FAKE:
            return FakeCRM()
        if salon.yclients_company_id is None:
            raise DomainValidationError("У салона не указан ID филиала YCLIENTS")
        yclients = YclientsProvider(
            salon.yclients_company_id, self.settings, timezone=salon.timezone, limiter=self.limiter
        )
        self._yclients.append(yclients)
        return DemoProvider(yclients) if kind is CrmKind.DEMO else yclients

    async def aclose(self) -> None:
        for provider in self._yclients:
            await provider.aclose()
        self._providers.clear()
        self._yclients.clear()
