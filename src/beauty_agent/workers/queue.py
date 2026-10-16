"""Постановка задач из async-кода. Публикация в RabbitMQ — блокирующий вызов kombu,
поэтому он идёт в пуле потоков: иначе вебхук остановил бы цикл событий всего API."""

import asyncio
import uuid
from typing import Any


class CeleryQueue:
    async def schedule(self, account_id: uuid.UUID, chat_id: str, token: str, *, delay_s: float) -> None:
        from beauty_agent.workers.messages import process_batch_task

        await asyncio.to_thread(
            process_batch_task.apply_async, args=[str(account_id), chat_id, token], countdown=delay_s
        )

    async def submit_admin_update(self, payload: dict[str, Any]) -> None:
        from beauty_agent.workers.messages import admin_update_task

        await asyncio.to_thread(admin_update_task.apply_async, args=[payload])

    async def sync_booking(self, salon_id: uuid.UUID, external_id: str) -> None:
        from beauty_agent.workers.reminders import sync_booking_task

        await asyncio.to_thread(sync_booking_task.apply_async, args=[str(salon_id), external_id])
