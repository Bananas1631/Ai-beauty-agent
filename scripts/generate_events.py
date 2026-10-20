"""10 млн синтетических событий в PostgreSQL и ClickHouse и одни и те же отчёты в обоих — для docs/performance.md.

    poetry run python -m scripts.generate_events --rows 10000000
    poetry run python -m scripts.generate_events --cleanup            # удалить bench-таблицы

Данные генерируют сами базы (generate_series и numbers) по одной формуле от номера строки — без
передачи 10 млн строк по сети и одинаково в обеих: 500 салонов, ноябрь 2026, 60% — ответы агента,
остальное — сообщения, записи и визиты. Таблицы отдельные (bench.events и analytics.events_bench):
рабочие данные не трогаются.

Отчёты: (1) расход на LLM по салонам и моделям за месяц — агрегат по всей таблице; (2) p95 задержки
по дням для одного салона; (3) число событий по типам для одного салона. В PostgreSQL — индекс
(salon_id, event_type, ts) того же порядка, что ключ сортировки ClickHouse. Время — медиана из 5
запусков после прогрева; диск — таблица с индексами (PostgreSQL) и активные части (ClickHouse).
"""

import argparse
import asyncio
import statistics
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import text

from beauty_agent.analytics.clickhouse import ClickHouse
from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine

SALONS = 500
MONTH_S = 30 * 86400
SALON_ID = "00000000-0000-4000-8000-000000000042"

PG_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS bench;
DROP TABLE IF EXISTS bench.events;
CREATE TABLE bench.events (
    event_id uuid NOT NULL,
    event_type text NOT NULL,
    salon_id uuid NOT NULL,
    ts timestamptz NOT NULL,
    conversation_id uuid,
    model text NOT NULL,
    cost_rub numeric(12, 4) NOT NULL,
    latency_ms integer NOT NULL,
    offers_shown smallint NOT NULL,
    created_by_ai boolean NOT NULL
)
"""

PG_FILL = """
INSERT INTO bench.events
SELECT
    ('00000000-0000-4000-9000-' || lpad(n::text, 12, '0'))::uuid,
    CASE WHEN n % 10 < 6 THEN 'llm.call.finished' WHEN n % 10 < 8 THEN 'message.received'
         WHEN n % 10 = 8 THEN 'booking.created' ELSE 'booking.attributed' END,
    ('00000000-0000-4000-8000-' || lpad((n % {salons})::text, 12, '0'))::uuid,
    timestamptz '2026-11-01 00:00:00+00' + make_interval(secs => ((n * 7919) % {month_s})::double precision),
    ('00000000-0000-4000-a000-' || lpad((n / 5)::text, 12, '0'))::uuid,
    CASE WHEN n % 10 < 6 THEN (ARRAY['qwen3-235b', 'gigachat-2-lite', 'yandexgpt-lite'])[n % 3 + 1] ELSE '' END,
    ((n * 37) % 2000)::numeric / 1000,
    300 + (n * 104729) % 6000,
    (n % 4)::smallint,
    n % 3 <> 0
FROM generate_series({start}::bigint, {stop}::bigint) AS n
"""

CH_SCHEMA = """
CREATE TABLE analytics.events_bench
(
    event_id UUID, event_type LowCardinality(String), salon_id UUID, ts DateTime64(3, 'UTC'),
    conversation_id Nullable(UUID), model LowCardinality(String), cost_rub Decimal(12, 4),
    latency_ms UInt32, offers_shown UInt16, created_by_ai Bool
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (salon_id, event_type, ts, event_id)
"""

CH_FILL = """
INSERT INTO analytics.events_bench
SELECT
    toUUID(concat('00000000-0000-4000-9000-', leftPad(toString(n), 12, '0'))),
    multiIf(n % 10 < 6, 'llm.call.finished', n % 10 < 8, 'message.received',
            n % 10 = 8, 'booking.created', 'booking.attributed'),
    toUUID(concat('00000000-0000-4000-8000-', leftPad(toString(n % {salons}), 12, '0'))),
    toDateTime64('2026-11-01 00:00:00', 3, 'UTC') + toIntervalSecond((n * 7919) % {month_s}),
    toUUID(concat('00000000-0000-4000-a000-', leftPad(toString(intDiv(n, 5)), 12, '0'))),
    if(n % 10 < 6, ['qwen3-235b', 'gigachat-2-lite', 'yandexgpt-lite'][n % 3 + 1], ''),
    toDecimal64((n * 37) % 2000, 4) / 1000,
    300 + (n * 104729) % 6000,
    n % 4,
    n % 3 != 0
FROM (SELECT number + 1 AS n FROM numbers({rows}))
"""

QUERIES: dict[str, tuple[str, str]] = {
    "Расход на LLM по салонам и моделям за месяц (вся таблица)": (
        """SELECT salon_id, model, count(*), sum(cost_rub) FROM bench.events
           WHERE event_type = 'llm.call.finished' AND ts >= '2026-11-01' AND ts < '2026-12-01'
           GROUP BY salon_id, model""",
        """SELECT salon_id, model, count(), sum(cost_rub) FROM analytics.events_bench
           WHERE event_type = 'llm.call.finished' AND ts >= '2026-11-01' AND ts < '2026-12-01'
           GROUP BY salon_id, model""",
    ),
    "p95 задержки по дням, один салон": (
        """SELECT date_trunc('day', ts), percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
           FROM bench.events WHERE salon_id = CAST(:salon_id AS uuid) AND event_type = 'llm.call.finished'
           GROUP BY 1 ORDER BY 1""",
        """SELECT toDate(ts), quantile(0.95)(latency_ms) FROM analytics.events_bench
           WHERE salon_id = {salon_id:UUID} AND event_type = 'llm.call.finished' GROUP BY 1 ORDER BY 1""",
    ),
    "События по типам, один салон за месяц": (
        """SELECT event_type, count(*) FROM bench.events
           WHERE salon_id = CAST(:salon_id AS uuid) AND ts >= '2026-11-01' AND ts < '2026-12-01' GROUP BY 1""",
        """SELECT event_type, count() FROM analytics.events_bench
           WHERE salon_id = {salon_id:UUID} AND ts >= '2026-11-01' AND ts < '2026-12-01' GROUP BY 1""",
    ),
}


async def timed(action: Callable[[], Awaitable[Any]], runs: int = 5) -> float:
    await action()  # прогрев: кэш страниц и планы
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        await action()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def human(size: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


async def run(rows: int, *, cleanup: bool) -> None:
    settings = get_settings()
    engine = make_engine(settings.postgres, migrations=True)  # без statement_timeout: заливка идёт минуты
    clickhouse = await ClickHouse.connect(settings.clickhouse, database="default")
    try:
        await clickhouse.command("CREATE DATABASE IF NOT EXISTS analytics")
        await clickhouse.command("DROP TABLE IF EXISTS analytics.events_bench")
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS bench.events"))
        if cleanup:
            print("bench-таблицы удалены")
            return

        started = time.perf_counter()
        async with engine.begin() as conn:
            for statement in PG_SCHEMA.split(";"):
                if statement.strip():
                    await conn.execute(text(statement))
        step = 1_000_000
        for first in range(1, rows + 1, step):
            last = min(first + step - 1, rows)
            async with engine.begin() as conn:
                await conn.execute(text(PG_FILL.format(salons=SALONS, month_s=MONTH_S, start=first, stop=last)))
        async with engine.begin() as conn:
            await conn.execute(text("CREATE INDEX ON bench.events (salon_id, event_type, ts)"))
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("VACUUM ANALYZE bench.events"))
        pg_load = time.perf_counter() - started

        started = time.perf_counter()
        await clickhouse.command(CH_SCHEMA)
        await clickhouse.command(CH_FILL.format(salons=SALONS, month_s=MONTH_S, rows=rows))
        await clickhouse.command("OPTIMIZE TABLE analytics.events_bench FINAL")
        ch_load = time.perf_counter() - started

        async with engine.connect() as conn:
            pg_size = int((await conn.execute(text("SELECT pg_total_relation_size('bench.events')"))).scalar_one())
        ch_size = int(
            await clickhouse.scalar(
                "SELECT sum(bytes_on_disk) FROM system.parts WHERE database = 'analytics' "
                "AND table = 'events_bench' AND active"
            )
        )

        print(f"\n{rows:,} событий".replace(",", " "))
        print("| | PostgreSQL 17 | ClickHouse 25.3 | Разница |\n|---|---|---|---|")
        print(f"| Заливка (генерация в базе) | {pg_load:.1f} с | {ch_load:.1f} с | — |")
        print(f"| На диске | {human(pg_size)} | {human(ch_size)} | в {pg_size / ch_size:.0f} раз |")
        for title, (pg_sql, ch_sql) in QUERIES.items():
            # значения — параметрами, а не в тексте SQL: запросы те же, что у отчётов в проде
            async def pg_query(sql: str = pg_sql) -> None:
                async with engine.connect() as conn:
                    (await conn.execute(text(sql), {"salon_id": SALON_ID} if ":salon_id" in sql else {})).all()

            async def ch_query(sql: str = ch_sql) -> None:
                await clickhouse.rows(sql, {"salon_id": SALON_ID} if "{salon_id:" in sql else None)

            pg_s, ch_s = await timed(pg_query), await timed(ch_query)
            print(f"| {title} | {pg_s * 1000:.0f} мс | {ch_s * 1000:.0f} мс | в {pg_s / ch_s:.0f} раз |")
    finally:
        await clickhouse.close()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Синтетические события: PostgreSQL против ClickHouse")
    parser.add_argument("--rows", type=int, default=10_000_000)
    parser.add_argument("--cleanup", action="store_true", help="только удалить bench-таблицы")
    args = parser.parse_args(argv)
    asyncio.run(run(args.rows, cleanup=args.cleanup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
