"""Прайс салона без YCLIENTS из Excel или CSV — в каталог услуг (catalog/import_excel.py).

    poetry run python -m scripts.import_price --slug studio-lavanda price.xlsx --dry-run
    poetry run python -m scripts.import_price --slug studio-lavanda price.csv

Сначала --dry-run: печатает услуги и все ошибки с номерами строк, ничего не меняя. С ошибками
импорт не выполняется — каталог меняется целиком или не меняется.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from beauty_agent.catalog.import_excel import import_price_list, parse_price_list
from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.entities import format_rub


async def run(args: argparse.Namespace, raw: bytes) -> int:
    """raw — содержимое файла: читается до цикла событий, чтобы не блокировать его."""
    path: Path = args.file
    price_list = parse_price_list(raw, path.name)
    for item in price_list.items:
        price = (
            f"{format_rub(item.price_min)} ₽"
            if item.price_min == item.price_max
            else f"{format_rub(item.price_min)}–{format_rub(item.price_max)} ₽"
        )
        print(f"  {item.name} · {price} · {item.duration_min} мин" + (f" · {item.category}" if item.category else ""))
    if price_list.errors:
        print(f"\nОшибки ({len(price_list.errors)}):", file=sys.stderr)
        for error in price_list.errors:
            print(f"  {error}", file=sys.stderr)
        return 1
    print(f"\nУслуг в файле: {len(price_list.items)}")
    if args.dry_run:
        return 0
    engine = make_engine(get_settings().postgres)
    try:
        async with make_session_factory(engine)() as session:
            salon = await SalonsRepository(session).get_by_slug(args.slug)
            if salon is None:
                print(f"Салон {args.slug} не найден", file=sys.stderr)
                return 1
            report = await import_price_list(session, salon.id, price_list)
            await session.commit()
    finally:
        await engine.dispose()
    print(f"Создано: {report.created}, обновлено: {report.updated}, выключено: {report.deactivated}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Импорт прайса из Excel или CSV")
    parser.add_argument("--slug", required=True)
    parser.add_argument("file", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="только проверить файл")
    configure_logging("WARNING", json_logs=False)
    args = parser.parse_args(argv)
    return asyncio.run(run(args, args.file.read_bytes()))


if __name__ == "__main__":
    sys.exit(main())
