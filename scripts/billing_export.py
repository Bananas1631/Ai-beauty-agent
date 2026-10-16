"""Месячная выгрузка для счёта по тарифу «Результат»: записи, сделанные ИИ, по правилу domain/billing.py.

    poetry run python -m scripts.billing_export --slug studio-lavanda --month 2026-11 > lavanda-2026-11.csv

CSV с разделителем «;» открывается в Excel; в конце — итог. Правило то же, что в договоре.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.billing import BillingPeriod
from beauty_agent.workers.reports import billable_bookings, invoice_csv

_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Выгрузка записей ИИ за месяц для счёта")
    parser.add_argument("--slug", required=True, help="адрес салона: /s/<slug>")
    parser.add_argument("--month", required=True, help="месяц визитов: ГГГГ-ММ")
    return parser.parse_args(argv)


async def export(slug: str, month: str, settings: Settings) -> str:
    match = _MONTH.fullmatch(month)
    if match is None:
        raise SystemExit("Месяц в формате ГГГГ-ММ, например 2026-11")
    engine = make_engine(settings.postgres)
    try:
        async with make_session_factory(engine)() as session:
            salon = await SalonsRepository(session).get_by_slug(slug)
            if salon is None:
                raise SystemExit(f"Салон {slug} не найден")
            period = BillingPeriod.month(int(match[1]), int(match[2]), salon.timezone)
            lines = await billable_bookings(session, salon.id, period, datetime.now(UTC))
            return invoice_csv(lines, salon.timezone)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    sys.stdout.write(asyncio.run(export(args.slug, args.month, settings or get_settings())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
