"""Задержки «как у настоящих» LLM и CRM для нагрузочного теста на фейковых провайдерах.

Нагрузочный тест на настоящих моделях и YCLIENTS стоит денег и упирается в чужие лимиты,
а на мгновенных фейках не показывает главного: что происходит, пока сервис ждёт внешнюю систему.
Задержка — логнормальная вокруг заданного среднего (у реальных API длинный хвост).
Включается APP_SIMULATED_LATENCY_MS и только вне прода (runtime.py).
"""

import asyncio
import math
import random
from collections.abc import Sequence
from datetime import date

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.adapters.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec
from beauty_agent.domain.entities import (
    BookingRequest,
    BookingResult,
    CrmBooking,
    MasterInfo,
    SalonInfo,
    ServiceInfo,
    Slot,
)

_SIGMA = 0.5  # разброс: p95 ≈ 2,3 × медианы


async def pause(mean_ms: int) -> None:
    if mean_ms <= 0:
        return
    median = mean_ms / math.exp(_SIGMA**2 / 2)
    await asyncio.sleep(random.lognormvariate(math.log(median), _SIGMA) / 1000)


class SlowLLM:
    def __init__(self, inner: LLMClient, mean_ms: int) -> None:
        self.inner = inner
        self.mean_ms = mean_ms

    @property
    def model_key(self) -> str:
        return self.inner.model_key

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        await pause(self.mean_ms)
        return await self.inner.complete(messages, tools, temperature=temperature, max_tokens=max_tokens)


class SlowCRM:
    """CRM с задержкой каждого запроса; у YCLIENTS — 150–400 мс на запрос."""

    def __init__(self, inner: BookingProvider, mean_ms: int) -> None:
        self.inner = inner
        self.mean_ms = mean_ms

    async def get_salon_info(self) -> SalonInfo:
        await pause(self.mean_ms)
        return await self.inner.get_salon_info()

    async def list_services(self) -> list[ServiceInfo]:
        await pause(self.mean_ms)
        return await self.inner.list_services()

    async def list_masters(self, service_id: str | None = None) -> list[MasterInfo]:
        await pause(self.mean_ms)
        return await self.inner.list_masters(service_id)

    async def find_slots(
        self, service_id: str, date_from: date, date_to: date, master_id: str | None = None
    ) -> list[Slot]:
        await pause(self.mean_ms * 2)  # поиск окон — несколько запросов к YCLIENTS
        return await self.inner.find_slots(service_id, date_from, date_to, master_id)

    async def check_slot(self, slot: Slot) -> None:
        await pause(self.mean_ms)
        await self.inner.check_slot(slot)

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        await pause(self.mean_ms)
        return await self.inner.create_booking(request)

    async def find_booking(self, slot: Slot, phone: str) -> BookingResult | None:
        await pause(self.mean_ms)
        return await self.inner.find_booking(slot, phone)

    async def get_booking(self, external_id: str) -> CrmBooking | None:
        await pause(self.mean_ms)
        return await self.inner.get_booking(external_id)

    async def reschedule_booking(self, external_id: str, slot: Slot) -> BookingResult:
        await pause(self.mean_ms)
        return await self.inner.reschedule_booking(external_id, slot)

    async def cancel_booking(self, external_id: str) -> None:
        await pause(self.mean_ms)
        await self.inner.cancel_booking(external_id)
