"""Базовый репозиторий: salon_id подставляется в каждый запрос сам.

Код выше репозитория не пишет where(salon_id == ...) руками — значит, не может его забыть.
"""

import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.base import SalonOwned


class SalonRepository[M: SalonOwned]:
    """Запросы к таблице модели M в пределах одного салона."""

    model: type[M]  # задаёт наследник: model = Client

    def __init__(self, session: AsyncSession, salon_id: uuid.UUID) -> None:
        self.session = session
        self.salon_id = salon_id

    def _select(self) -> Select[M]:
        """SELECT из таблицы модели — только строки текущего салона. Основа всех запросов."""
        return select(self.model).where(self.model.salon_id == self.salon_id)

    async def get(self, id_: uuid.UUID) -> M | None:
        """Строка по id. Строка чужого салона не найдётся, даже если id верный."""
        return await self.session.scalar(self._select().filter_by(id=id_))

    async def count(self) -> int:
        """Сколько строк у салона."""
        query = select(func.count()).select_from(self._select().subquery())
        return (await self.session.execute(query)).scalar_one()

    def add(self, row: M) -> M:
        """Добавить строку в сессию; salon_id ставит репозиторий, а не вызывающий код."""
        row.salon_id = self.salon_id
        self.session.add(row)
        return row
