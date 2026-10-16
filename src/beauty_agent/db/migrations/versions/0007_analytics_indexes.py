"""analytics indexes

Индексы, найденные по EXPLAIN (ANALYZE, BUFFERS) запросов аналитики на данных масштаба
нагрузки (docs/performance.md): составной «салон + период», частичный только по неотменённым
записям, покрывающий (INCLUDE) для Index Only Scan.

CREATE INDEX CONCURRENTLY не блокирует запись в bookings на живой базе пилотов, но не работает
внутри транзакции — поэтому autocommit_block. Если построение прервалось, остаётся невалидный
индекс: IF NOT EXISTS его не пересоздаст — удалить DROP INDEX CONCURRENTLY и запустить миграцию снова.

Revision ID: 0007
Create Date: 2026-12-01 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOT_CANCELLED = sa.text("status <> 'cancelled'")
VISITS = sa.text("status IN ('confirmed', 'completed') AND client_id IS NOT NULL")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_bookings_salon_period",
            "bookings",
            ["salon_id", "starts_at"],
            postgresql_include=["ends_at", "master_id", "service_id", "client_id", "price", "status"],
            postgresql_where=NOT_CANCELLED,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "ix_bookings_salon_created",
            "bookings",
            ["salon_id", "created_at"],
            postgresql_include=["created_by_ai", "status"],
            postgresql_where=NOT_CANCELLED,
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "ix_bookings_salon_client_visits",
            "bookings",
            ["salon_id", "client_id", "starts_at"],
            postgresql_where=VISITS,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in ("ix_bookings_salon_client_visits", "ix_bookings_salon_created", "ix_bookings_salon_period"):
            op.drop_index(name, table_name="bookings", postgresql_concurrently=True, if_exists=True)
