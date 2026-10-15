"""Провайдер CRM для демо: настоящие каталог и свободные слоты салона, но запись только имитируется.

Демо делается для продажи — без участия салона. Владелец видит свои услуги, цены и свободное
время, а в YCLIENTS при этом ничего не создаётся: ни записи, ни SMS клиенту.
"""

import hashlib
from datetime import date

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.domain.entities import BookingRequest, BookingResult, MasterInfo, SalonInfo, ServiceInfo, Slot


class DemoProvider:
    def __init__(self, inner: BookingProvider) -> None:
        self.inner = inner
        self.simulated: dict[str, BookingResult] = {}

    async def get_salon_info(self) -> SalonInfo:
        return await self.inner.get_salon_info()

    async def list_services(self) -> list[ServiceInfo]:
        return await self.inner.list_services()

    async def list_masters(self, service_id: str | None = None) -> list[MasterInfo]:
        return await self.inner.list_masters(service_id)

    async def find_slots(
        self, service_id: str, date_from: date, date_to: date, master_id: str | None = None
    ) -> list[Slot]:
        return await self.inner.find_slots(service_id, date_from, date_to, master_id)

    async def check_slot(self, slot: Slot) -> None:
        await self.inner.check_slot(slot)  # проверка ничего не создаёт — можно спросить настоящую CRM

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        existing = self.simulated.get(request.idempotency_key)
        if existing is not None:
            return existing
        digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()
        result = BookingResult(external_id=f"demo-{digest[:10]}", slot=request.slot)
        self.simulated[request.idempotency_key] = result
        return result

    async def find_booking(self, slot: Slot, phone: str) -> BookingResult | None:
        return None
