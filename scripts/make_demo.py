"""Персональное демо по ID филиала YCLIENTS за 5 минут — для продажи салону.

    poetry run python -m scripts.make_demo --company-id 555001
    poetry run python -m scripts.make_demo --company-id 555001 --site https://salon.example

Импорт каталога (салон с флагом демо), индексация сайта, если он есть (иначе — карточка филиала),
ссылка на страницу /s/{slug}. Печатает время каждого этапа. Запись в демо только имитируется.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from beauty_agent.adapters.crm.yclients import YclientsProvider
from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.catalog.demo import DemoReport, make_demo
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging


async def run(args: argparse.Namespace, settings: Settings) -> DemoReport:
    engine = make_engine(settings.postgres)
    provider = YclientsProvider(args.company_id, settings.yclients)
    try:
        async with make_session_factory(engine)() as session:
            return await make_demo(
                session,
                provider,
                public_url=settings.app.public_url,
                embedder=build_embedder(settings.llm),
                company_id=args.company_id,
                site_url=args.site,
                max_pages=args.max_pages,
            )
    finally:
        await provider.aclose()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Персональное демо салона по ID филиала YCLIENTS")
    parser.add_argument("--company-id", type=int, required=True, help="ID филиала в YCLIENTS")
    parser.add_argument("--site", help="адрес сайта салона, если его нет в карточке филиала")
    parser.add_argument("--max-pages", type=int, default=30)
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    report = asyncio.run(run(args, get_settings()))
    print("\n".join(report.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
