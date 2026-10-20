"""Месячная выгрузка для счёта по тарифу «Результат»: записи, сделанные ИИ, по правилу domain/billing.py.

    poetry run python -m scripts.billing_export --slug studio-lavanda --month 2026-11 > lavanda-2026-11.csv
    poetry run python -m scripts.billing_export --slug studio-lavanda --month 2026-11 --xlsx lavanda-2026-11.xlsx

CSV с разделителем «;» открывается в Excel; в конце — итог. Excel — листы «Сводка» (итоги формулами),
«Записи» и «По услугам» (reports/excel.py); то же 1-го числа получает владелец в админ-бот.
Правило то же, что в договоре.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.billing import BillingPeriod
from beauty_agent.domain.errors import DomainValidationError
from beauty_agent.reports.excel import collect_invoice, invoice_workbook, parse_month
from beauty_agent.reports.invoice import billable_bookings, invoice_csv


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Выгрузка записей ИИ за месяц для счёта")
    parser.add_argument("--slug", required=True, help="адрес салона: /s/<slug>")
    parser.add_argument("--month", required=True, help="месяц визитов: ГГГГ-ММ")
    parser.add_argument("--xlsx", type=Path, help="сохранить книгу Excel в этот файл (вместо CSV в stdout)")
    return parser.parse_args(argv)


async def export(slug: str, month: str, settings: Settings, *, xlsx: bool = False) -> str | bytes:
    try:
        year, number = parse_month(month)
    except DomainValidationError as exc:
        raise SystemExit(exc.message) from exc
    engine = make_engine(settings.postgres)
    try:
        async with make_session_factory(engine)() as session:
            salon = await SalonsRepository(session).get_by_slug(slug)
            if salon is None:
                raise SystemExit(f"Салон {slug} не найден")
            now = datetime.now(UTC)
            if xlsx:
                return invoice_workbook(await collect_invoice(session, salon, month, now=now))
            period = BillingPeriod.month(year, number, salon.timezone)
            lines = await billable_bookings(session, salon.id, period, now)
            return invoice_csv(lines, salon.timezone)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    result = asyncio.run(export(args.slug, args.month, settings or get_settings(), xlsx=args.xlsx is not None))
    if isinstance(result, bytes):
        args.xlsx.write_bytes(result)
        print(f"Сохранено: {args.xlsx}")
    else:
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
