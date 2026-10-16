"""Общая сборка агента для тестов: фейковый CRM, fakeredis и контекст инструментов."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fakeredis import FakeAsyncRedis

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.agent.registry import ToolContext
from beauty_agent.agent.tools import CardSalonInfo, ProviderCatalog
from beauty_agent.domain.booking_engine.memory import MemoryLedger
from beauty_agent.domain.booking_engine.service import BookingService

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=MSK)  # понедельник, утро


def make_context(
    redis: FakeAsyncRedis, *, crm: FakeCRM | None = None, conversation_id: str = "dialog-1"
) -> ToolContext:
    crm = crm or FakeCRM(clock=lambda: NOW)
    booking = BookingService(
        salon_id="salon-1", timezone="Europe/Moscow", provider=crm, redis=redis, ledger=MemoryLedger()
    )
    return ToolContext(
        salon_id="salon-1",
        salon_name="Лаванда",
        conversation_id=conversation_id,
        now=NOW,
        booking=booking,
        catalog=ProviderCatalog(booking),
        salon_info=CardSalonInfo(crm),
    )
