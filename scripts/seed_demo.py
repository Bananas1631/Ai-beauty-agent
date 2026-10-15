"""Демо-салон из фейкового CRM — чтобы проверяющий без доступа к YCLIENTS сразу открыл работающую страницу.

    make up && make migrate && make seed     # затем http://localhost:8000/s/demo-lavanda

Каталог берётся из фейкового CRM, база знаний — из вымышленного сайта evals/sites/lavanda.
Без ключей LLM в чате отвечает офлайн-модель.
"""

import asyncio
import sys

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.catalog.demo import DemoReport, make_demo
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.domain.enums import CrmKind
from evals.site_server import site_client, site_host

DEMO_SLUG = "demo-lavanda"


async def seed(settings: Settings) -> DemoReport:
    engine = make_engine(settings.postgres)
    try:
        async with make_session_factory(engine)() as session, site_client("lavanda") as client:
            return await make_demo(
                session,
                FakeCRM(),
                public_url=settings.app.public_url,
                embedder=build_embedder(settings.llm),
                crm_kind=CrmKind.FAKE,
                slug=DEMO_SLUG,
                site_url=f"https://{site_host('lavanda')}/",
                site_client=client,
                pause_s=0,
            )
    finally:
        await engine.dispose()


def main() -> int:
    configure_logging("WARNING", json_logs=False)
    report = asyncio.run(seed(get_settings()))
    print("\n".join(report.lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
