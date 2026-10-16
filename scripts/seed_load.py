"""Салоны для нагрузочного теста (tests/load/locustfile.py): фейковый CRM и Telegram-канал у каждого.

    poetry run python -m scripts.seed_load --salons 50 --out load_salons.json

Только для отдельной базы. В JSON — slug салона и ID канала: по ним Locust строит адреса
веб-чата и вебхука. Повторный запуск не создаёт дублей.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.security import Security
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.enums import Channel, CrmKind

FAKE_BOT_TOKEN = "100000{n:04d}:AAH-load_test_bot_token_not_real_0000000"  # noqa: S105 — не настоящий токен


async def seed(count: int) -> list[dict[str, str]]:
    settings = get_settings()
    if settings.app.env == "prod":
        raise SystemExit("Нагрузочные данные — только не в проде")
    security = Security(settings.security, env=settings.app.env)
    engine = make_engine(settings.postgres)
    salons: list[dict[str, str]] = []
    try:
        factory = make_session_factory(engine)
        for n in range(1, count + 1):
            slug = f"load-web-{n}"
            async with factory() as session:
                await import_catalog(session, FakeCRM(), slug=slug)
                salon = await SalonsRepository(session).get_by_slug(slug)
                if salon is None:
                    raise SystemExit(f"Салон {slug} не создан")
                salon.settings = {**(salon.settings or {}), "crm_kind": CrmKind.FAKE.value}
                account = await ChannelAccountsRepository(session).connect(
                    salon.id, Channel.TELEGRAM, f"load-bot-{n}", {"token": FAKE_BOT_TOKEN.format(n=n)}, security
                )
                await session.commit()
                salons.append({"slug": slug, "account_id": str(account.id)})
    finally:
        await engine.dispose()
    return salons


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Салоны для нагрузочного теста")
    parser.add_argument("--salons", type=int, default=50)
    parser.add_argument("--out", type=Path, default=Path("load_salons.json"))
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    salons = asyncio.run(seed(args.salons))
    args.out.write_text(json.dumps(salons, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Салонов: {len(salons)} → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
