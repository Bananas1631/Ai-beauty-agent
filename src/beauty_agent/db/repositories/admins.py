"""Администраторы салонов в админ-боте и черновики теневого режима.

Админ-бот один на весь сервис: администратор приходит с Telegram ID, а салон определяется
его привязкой. Поэтому поиск — по Telegram ID, а права на салон проверяются каждым запросом.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Draft, Salon, SalonAdmin
from beauty_agent.domain.enums import AdminRole, DraftStatus


class AdminsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bind(
        self, salon_id: uuid.UUID, telegram_user_id: str, chat_id: str, name: str | None, role: AdminRole
    ) -> None:
        """Привязка по коду; повторная привязка того же человека обновляет чат и снова включает доступ."""
        statement = (
            insert(SalonAdmin)
            .values(
                salon_id=salon_id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                name=name[:200] if name else None,
                role=role,
            )
            .on_conflict_do_update(
                index_elements=["salon_id", "telegram_user_id"],
                set_={"chat_id": chat_id, "is_active": True, "role": role, "updated_at": func.now()},
            )
        )
        await self.session.execute(statement)

    async def chats_of_salon(self, salon_id: uuid.UUID) -> list[str]:
        statement = select(SalonAdmin.chat_id).where(SalonAdmin.salon_id == salon_id, SalonAdmin.is_active)
        return list(await self.session.scalars(statement))

    async def salons_of(self, telegram_user_id: str) -> Sequence[Salon]:
        statement = (
            select(Salon)
            .join(SalonAdmin, SalonAdmin.salon_id == Salon.id)
            .where(SalonAdmin.telegram_user_id == telegram_user_id, SalonAdmin.is_active)
            .order_by(Salon.name)
        )
        return (await self.session.scalars(statement)).all()

    async def is_admin(self, salon_id: uuid.UUID, telegram_user_id: str) -> bool:
        statement = select(SalonAdmin.id).where(
            SalonAdmin.salon_id == salon_id,
            SalonAdmin.telegram_user_id == telegram_user_id,
            SalonAdmin.is_active,
        )
        return await self.session.scalar(statement) is not None


class DraftsRepository:
    """Черновики. Решение администратора — атомарный переход из pending: второе нажатие ничего не меняет."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, salon_id: uuid.UUID, conversation_id: uuid.UUID, **values: Any) -> uuid.UUID | None:
        """None — черновик с этим ключом уже есть (повтор задачи)."""
        statement = (
            insert(Draft)
            .values(salon_id=salon_id, conversation_id=conversation_id, **values)
            .on_conflict_do_nothing(index_elements=["salon_id", "dedupe_key"])
            .returning(Draft.id)
        )
        return await self.session.scalar(statement)

    async def by_key(self, salon_id: uuid.UUID, dedupe_key: str) -> Draft | None:
        return await self.session.scalar(
            select(Draft).where(Draft.salon_id == salon_id, Draft.dedupe_key == dedupe_key)
        )

    async def get(self, draft_id: uuid.UUID) -> Draft | None:
        return await self.session.get(Draft, draft_id)

    async def claim_notification(self, draft_id: uuid.UUID) -> Draft | None:
        """Показать черновик администраторам один раз; None — уже показан."""
        statement = (
            update(Draft)
            .where(Draft.id == draft_id, Draft.notified_at.is_(None))
            .values(notified_at=func.now())
            .returning(Draft)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(statement)).scalar_one_or_none()

    async def release_notification(self, draft_id: uuid.UUID) -> None:
        await self.session.execute(update(Draft).where(Draft.id == draft_id).values(notified_at=None))

    async def decide(
        self,
        draft_id: uuid.UUID,
        salon_id: uuid.UUID,
        *,
        status: DraftStatus,
        admin_id: str,
        final_text: str | None = None,
        similarity: int | None = None,
    ) -> bool:
        """pending → решение. False — решение уже принято (повторное нажатие или другой администратор)."""
        statement = (
            update(Draft)
            .where(Draft.id == draft_id, Draft.salon_id == salon_id, Draft.status == DraftStatus.PENDING)
            .values(
                status=status,
                decided_by=admin_id,
                decided_at=func.now(),
                final_text=final_text,
                similarity=similarity,
            )
            .returning(Draft.id)
        )
        return await self.session.scalar(statement) is not None

    async def stats(self, salon_id: uuid.UUID, since: datetime) -> dict[DraftStatus, list[int | None]]:
        """Решения за период: статус → похожесть исправленных текстов (для отчёта точности)."""
        statement = select(Draft.status, Draft.similarity).where(Draft.salon_id == salon_id, Draft.created_at >= since)
        result: dict[DraftStatus, list[int | None]] = {status: [] for status in DraftStatus}
        for status, similarity in await self.session.execute(statement):
            result[status].append(similarity)
        return result
