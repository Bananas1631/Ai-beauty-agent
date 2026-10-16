"""Планы и время запросов аналитики на базе с данными нагрузки (scripts/generate_load_data.sql).

    POSTGRES_DB=load poetry run python -m scripts.explain_analytics            # сводка
    POSTGRES_DB=load poetry run python -m scripts.explain_analytics --plans    # и полные планы

Для каждого запроса — EXPLAIN (ANALYZE, BUFFERS) и медиана времени по 5 запускам; затем keyset-пагинация
против OFFSET на последней странице длинной истории сообщений. Результаты — в docs/performance.md.
Только для отдельной базы: на базе пилотов EXPLAIN ANALYZE выполняет запросы по-настоящему.
"""

import argparse
import asyncio
import re
import statistics
import sys
import time as clock
from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine
from beauty_agent.db.repositories.analytics import load_query

RUNS = 5
TZ = "Europe/Moscow"


def analytics_params(salon_id: Any, now: datetime) -> dict[str, dict[str, Any]]:
    return {
        "master_load": {
            "salon_id": salon_id,
            "period_start": now - timedelta(days=90),
            "period_end": now,
            "tz": TZ,
            "open_hour": 10,
            "close_hour": 21,
        },
        "revenue_by_week": {
            "salon_id": salon_id,
            "period_start": now - timedelta(days=365),
            "period_end": now,
            "tz": TZ,
        },
        "ai_share": {
            "salon_id": salon_id,
            "period_start": now - timedelta(days=30),
            "period_end": now,
            "tz": TZ,
            "work_start": time(10),
            "work_end": time(21),
        },
        "returning_clients": {"salon_id": salon_id, "as_of": now, "limit": 50},
        "top_services": {
            "salon_id": salon_id,
            "period_start": now - timedelta(days=365),
            "period_end": now,
            "limit": 10,
        },
    }


async def timed(connection: AsyncConnection, sql: Any, params: dict[str, Any]) -> float:
    samples = []
    for _ in range(RUNS):
        started = clock.perf_counter()
        await connection.execute(sql, params)
        samples.append((clock.perf_counter() - started) * 1000)
    return statistics.median(samples)


async def explain(connection: AsyncConnection, sql: str, params: dict[str, Any]) -> list[str]:
    result = await connection.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), params)
    return [row[0] for row in result]


def summary(plan: list[str]) -> str:
    """Узлы чтения таблиц и итог: какие индексы, сколько буферов, время выполнения."""
    scans = [
        line.strip().removeprefix("->  ").split("  (")[0]
        for line in plan
        if re.search(
            r"(Seq Scan|Index Scan|Index Only Scan|Bitmap Heap Scan|Bitmap Index Scan)( Backward)?( using \S+)? on",
            line,
        )
    ]
    buffers = next((line.strip() for line in plan if line.strip().startswith("Buffers:")), "")
    execution = next((line.strip() for line in plan if line.startswith("Execution Time")), "")
    return f"{'; '.join(dict.fromkeys(scans))} | {buffers} | {execution}"


async def main(show_plans: bool) -> int:
    engine = make_engine(get_settings().postgres)
    now = datetime.now(UTC)
    try:
        async with engine.connect() as connection:
            salon_id = (await connection.execute(text("SELECT id FROM salons WHERE slug = 'load-1'"))).scalar_one()
            for name, params in analytics_params(salon_id, now).items():
                query = load_query(name)
                plan = await explain(connection, str(query), params)
                median = await timed(connection, query, params)
                print(f"{name}: медиана {median:.1f} мс | {summary(plan)}")
                if show_plans:
                    print("\n".join(f"    {line}" for line in plan))

            # Наивная возвращаемость: предыдущий визит коррелированным подзапросом — для сравнения с LAG
            naive = text(
                """
                SELECT b.client_id, b.starts_at,
                       b.starts_at - (
                           SELECT max(p.starts_at) FROM bookings AS p
                           WHERE p.salon_id = b.salon_id AND p.client_id = b.client_id
                             AND p.status IN ('confirmed', 'completed') AND p.starts_at < b.starts_at
                       ) AS gap
                FROM bookings AS b
                WHERE b.salon_id = :salon_id AND b.client_id IS NOT NULL
                  AND b.status IN ('confirmed', 'completed') AND b.starts_at < :as_of
                """
            )
            naive_params = {"salon_id": salon_id, "as_of": now}
            plan = await explain(connection, str(naive), naive_params)
            median = await timed(connection, naive, naive_params)
            print(f"returning_clients, коррелированный подзапрос вместо LAG: медиана {median:.1f} мс | {summary(plan)}")

            conversation = (
                await connection.execute(
                    text("SELECT conversation_id FROM messages GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
                )
            ).scalar_one()
            total = (
                await connection.execute(
                    text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": conversation}
                )
            ).scalar_one()
            base = "SELECT id, role, content, created_at FROM messages WHERE salon_id = :s AND conversation_id = :c"
            offset_sql = text(f"{base} ORDER BY created_at DESC, id DESC LIMIT 50 OFFSET :o")
            offset_params = {"s": salon_id, "c": conversation, "o": total - 50}
            cursor = (
                await connection.execute(
                    text(f"{base} ORDER BY created_at DESC, id DESC LIMIT 1 OFFSET :o"),
                    {"s": salon_id, "c": conversation, "o": total - 51},
                )
            ).one()
            keyset_sql = text(f"{base} AND (created_at, id) < (:t, :i) ORDER BY created_at DESC, id DESC LIMIT 50")
            keyset_params = {"s": salon_id, "c": conversation, "t": cursor.created_at, "i": cursor.id}
            for label, sql, params in (
                ("OFFSET", offset_sql, offset_params),
                ("keyset", keyset_sql, keyset_params),
            ):
                plan = await explain(connection, str(sql), params)
                median = await timed(connection, sql, params)
                print(f"последняя страница {total} сообщений, {label}: медиана {median:.2f} мс | {summary(plan)}")
    finally:
        await engine.dispose()
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EXPLAIN ANALYZE запросов аналитики на данных нагрузки")
    parser.add_argument("--plans", action="store_true", help="печатать полные планы")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args().plans)))
