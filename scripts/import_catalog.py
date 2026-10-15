"""Импорт каталога реального салона из YCLIENTS по ID филиала.

    poetry run python -m scripts.import_catalog --company-id 555001 --slug studio-lavanda --dry-run
    poetry run python -m scripts.import_catalog --fake --slug demo-lavanda --demo   # без токена YCLIENTS

--dry-run показывает изменения и откатывает их. Тонкая обёртка над catalog/importer.py.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.crm.yclients import YclientsProvider
from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.catalog.importer import ImportReport, import_catalog
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging

SLUG = re.compile(r"^[a-z0-9-]{1,64}$")


def slug_type(value: str) -> str:
    if not SLUG.fullmatch(value):
        raise argparse.ArgumentTypeError("slug: латиница в нижнем регистре, цифры и дефис, до 64 символов")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Импорт каталога салона из YCLIENTS")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--company-id", type=int, help="ID филиала в YCLIENTS")
    source.add_argument("--fake", action="store_true", help="фейковый CRM вместо YCLIENTS")
    parser.add_argument("--slug", type=slug_type, required=True, help="адрес страницы салона: /s/<slug>")
    parser.add_argument("--demo", action="store_true", help="демо-салон: запись только имитируется")
    parser.add_argument("--dry-run", action="store_true", help="показать изменения и откатить их")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace, settings: Settings) -> ImportReport:
    engine = make_engine(settings.postgres)
    yclients = None if args.fake else YclientsProvider(args.company_id, settings.yclients)
    provider: BookingProvider = FakeCRM() if yclients is None else yclients
    try:
        async with make_session_factory(engine)() as session:
            return await import_catalog(
                session,
                provider,
                slug=args.slug,
                company_id=None if args.fake else args.company_id,
                is_demo=args.demo,
                dry_run=args.dry_run,
                embedder=build_embedder(settings.llm),
            )
    finally:
        if yclients is not None:
            await yclients.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    args = parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    report = asyncio.run(run(args, settings or get_settings()))
    print("\n".join(report.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
