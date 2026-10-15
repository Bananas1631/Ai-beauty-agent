"""Интерфейс BookingProvider: что должна уметь любая CRM салона для агента.

Домен и агент зависят от этого интерфейса, а не от YCLIENTS: новая CRM (Arnica, DIKIDI)
подключается новым адаптером, движок записи и агент при этом не меняются.
Ошибки — доменные (domain/errors.py): SlotTakenError, ProviderUnavailableError и другие.
"""

from datetime import date
from typing import Protocol

from beauty_agent.domain.entities import BookingRequest, BookingResult, MasterInfo, SalonInfo, ServiceInfo, Slot


class BookingProvider(Protocol):
    async def get_salon_info(self) -> SalonInfo:
        """Карточка филиала: адрес, телефон, часы работы, часовой пояс."""
        ...

    async def list_services(self) -> list[ServiceInfo]:
        """Активные услуги с ценами и длительностью."""
        ...

    async def list_masters(self, service_id: str | None = None) -> list[MasterInfo]:
        """Мастера; с service_id — только те, кто делает эту услугу и принимает онлайн-запись."""
        ...

    async def find_slots(
        self, service_id: str, date_from: date, date_to: date, master_id: str | None = None
    ) -> list[Slot]:
        """Свободные старты услуги в диапазоне дат (включительно), по возрастанию времени."""
        ...

    async def check_slot(self, slot: Slot) -> None:
        """Проверяет без кэша, что окно ещё свободно. Занято — SlotTakenError."""
        ...

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        """Создаёт запись. Повтор с тем же ключом идемпотентности не создаёт вторую."""
        ...

    async def find_booking(self, slot: Slot, phone: str) -> BookingResult | None:
        """Ищет уже созданную запись клиента на это окно — проверка после таймаута создания."""
        ...
