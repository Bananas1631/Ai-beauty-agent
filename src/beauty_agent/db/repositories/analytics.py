"""Аналитика салона на чистом SQL: запросы — в db/queries/analytics/*.sql, здесь — только запуск.

Запрос выполняется через text() с именованными параметрами: значения уходят в PostgreSQL отдельно
от текста запроса и никогда не подставляются в строку, поэтому SQL-инъекция невозможна.
Строки результата превращаются в типизированные модели Pydantic. Каждый запрос фильтрует
по salon_id сам: репозиторий создаётся на салон, как и ORM-репозитории (ADR 008).
"""

import re
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from functools import cache
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession

_COMMENT = re.compile(r"--[^\n]*")
_NAME = re.compile(r"^[a-z_]+$")


@cache
def load_query(name: str) -> TextClause:
    """SQL-файл без комментариев: в комментариях бывают «:параметр» и время «10:30»."""
    if not _NAME.fullmatch(name):  # имя файла — из кода, но проверяем: это путь
        raise ValueError(f"Некорректное имя запроса: {name!r}")
    sql = files("beauty_agent.db.queries.analytics").joinpath(f"{name}.sql").read_text(encoding="utf-8")
    return text(_COMMENT.sub("", sql))


class Row(BaseModel):
    model_config = ConfigDict(frozen=True)


class MasterLoad(Row):
    master_id: uuid.UUID
    master_name: str
    dow: int  # 1 — понедельник
    hour: int
    booked_minutes: Decimal
    capacity_minutes: int
    load: Decimal  # 0…1


class WeekRevenue(Row):
    week: date  # понедельник недели по времени салона
    visits: int
    revenue: Decimal
    average_check: Decimal | None


class AiShare(Row):
    bookings: int
    ai_bookings: int
    ai_share: Decimal
    off_hours_bookings: int
    off_hours_ai_bookings: int


class ReturningClient(Row):
    client_id: uuid.UUID
    client_name: str | None
    visits: int
    last_visit: datetime
    usual_interval_days: Decimal
    days_since_last: Decimal


class ServiceRevenue(Row):
    service_id: uuid.UUID
    service_name: str
    visits: int
    revenue: Decimal
    share: Decimal
    cumulative_revenue: Decimal
    cumulative_share: Decimal


class AnalyticsRepository:
    def __init__(self, session: AsyncSession, salon_id: uuid.UUID) -> None:
        self.session = session
        self.salon_id = salon_id

    async def _rows[M: Row](self, name: str, model: type[M], **params: Any) -> list[M]:
        result = await self.session.execute(load_query(name), {"salon_id": self.salon_id, **params})
        return [model.model_validate(dict(row)) for row in result.mappings()]

    async def master_load(
        self, start: datetime, end: datetime, *, timezone: str, open_hour: int = 10, close_hour: int = 21
    ) -> list[MasterLoad]:
        return await self._rows(
            "master_load",
            MasterLoad,
            period_start=start,
            period_end=end,
            tz=timezone,
            open_hour=open_hour,
            close_hour=close_hour,
        )

    async def revenue_by_week(self, start: datetime, end: datetime, *, timezone: str) -> list[WeekRevenue]:
        return await self._rows("revenue_by_week", WeekRevenue, period_start=start, period_end=end, tz=timezone)

    async def ai_share(
        self, start: datetime, end: datetime, *, timezone: str, work_start: time, work_end: time
    ) -> AiShare:
        [row] = await self._rows(
            "ai_share",
            AiShare,
            period_start=start,
            period_end=end,
            tz=timezone,
            work_start=work_start,
            work_end=work_end,
        )
        return row

    async def returning_clients(self, as_of: datetime, *, limit: int = 50) -> list[ReturningClient]:
        return await self._rows("returning_clients", ReturningClient, as_of=as_of, limit=limit)

    async def top_services(self, start: datetime, end: datetime, *, limit: int = 10) -> list[ServiceRevenue]:
        return await self._rows("top_services", ServiceRevenue, period_start=start, period_end=end, limit=limit)

    async def average_check(self, start: datetime, end: datetime, *, timezone: str) -> Decimal | None:
        """Средний чек за период — из той же выборки, что выручка по неделям."""
        weeks = await self.revenue_by_week(start, end, timezone=timezone)
        visits = sum(week.visits for week in weeks)
        return (
            (sum((week.revenue for week in weeks), Decimal(0)) / visits).quantize(Decimal("0.01")) if visits else None
        )
