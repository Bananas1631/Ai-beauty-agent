"""Журнал записей в памяти: чат в терминале, evals и unit-тесты движка без PostgreSQL."""

import dataclasses
import uuid
from datetime import datetime

from beauty_agent.domain.booking_engine.service import BookingEntry, StoredBooking
from beauty_agent.domain.entities import Slot
from beauty_agent.domain.enums import BookingStatus


class MemoryLedger:
    def __init__(self) -> None:
        self.entries: list[BookingEntry] = []

    async def record(self, entry: BookingEntry) -> None:
        if any(existing.idempotency_key == entry.idempotency_key for existing in self.entries):
            return  # повтор подтверждения: запись уже есть
        self.entries.append(entry)

    async def upcoming(self, client_id: uuid.UUID, now: datetime) -> list[StoredBooking]:
        active = (BookingStatus.CONFIRMED, BookingStatus.PENDING)
        found = [
            StoredBooking(entry.external_id, entry.slot, entry.status)
            for entry in self.entries
            if entry.client_id == client_id and entry.status in active and entry.slot.starts_at > now
        ]
        return sorted(found, key=lambda item: item.slot.starts_at)

    def _replace(self, external_id: str, **changes: object) -> None:
        self.entries = [
            dataclasses.replace(entry, **changes) if entry.external_id == external_id else entry  # type: ignore[arg-type]
            for entry in self.entries
        ]

    async def cancel(self, external_id: str) -> None:
        self._replace(external_id, status=BookingStatus.CANCELLED)

    async def move(self, external_id: str, slot: Slot) -> None:
        self._replace(external_id, slot=slot)
