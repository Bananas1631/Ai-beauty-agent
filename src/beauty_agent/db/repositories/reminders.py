"""Напоминания о записях: планирование при записи, перепланирование при переносе, снятие при отмене."""

import uuid
from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert

from beauty_agent.db.models import Reminder
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.domain.enums import ReminderStatus
from beauty_agent.domain.reminders import due_times


class RemindersRepository(SalonRepository[Reminder]):
    model = Reminder

    async def plan(self, booking_id: uuid.UUID, starts_at: datetime, now: datetime, timezone: str) -> int:
        """Ставит напоминания; уже существующие (после переноса) получают новое время и снова ждут отправки."""
        planned = due_times(starts_at, now, timezone)
        for kind, due_at in planned.items():
            statement = (
                insert(Reminder)
                .values(salon_id=self.salon_id, booking_id=booking_id, kind=kind.value, due_at=due_at)
                .on_conflict_do_update(
                    index_elements=["booking_id", "kind"],
                    set_={
                        "due_at": due_at,
                        "status": ReminderStatus.PENDING,
                        "sent_at": None,
                        "updated_at": func.now(),
                    },
                )
            )
            await self.session.execute(statement)
        return len(planned)

    async def cancel_pending(self, booking_id: uuid.UUID) -> None:
        await self.session.execute(
            update(Reminder)
            .where(
                Reminder.salon_id == self.salon_id,
                Reminder.booking_id == booking_id,
                Reminder.status == ReminderStatus.PENDING,
            )
            .values(status=ReminderStatus.CANCELLED)
        )
