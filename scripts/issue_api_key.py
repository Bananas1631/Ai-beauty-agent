"""Ключ REST API для интеграции салона (сайт, приложение, CRM партнёра).

    poetry run python -m scripts.issue_api_key --slug studio-lavanda --name "Мобильное приложение"

Ключ печатается один раз: в БД хранится только его SHA-256. Потерян — выпустить новый и отозвать
старый (--revoke <id>).
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence

from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.repositories.api_keys import ApiKeysRepository
from beauty_agent.db.repositories.salons import SalonsRepository


async def run(args: argparse.Namespace) -> int:
    engine = make_engine(get_settings().postgres)
    try:
        async with make_session_factory(engine)() as session:
            salon = await SalonsRepository(session).get_by_slug(args.slug)
            if salon is None:
                print(f"Салон {args.slug} не найден", file=sys.stderr)
                return 1
            keys = ApiKeysRepository(session)
            if args.revoke:
                revoked = await keys.revoke(salon.id, uuid.UUID(args.revoke))
                await session.commit()
                print("Ключ отозван" if revoked else "Активный ключ с таким ID не найден")
                return 0 if revoked else 1
            row, key = await keys.issue(salon.id, args.name)
            await session.commit()
            print(f"ID ключа: {row.id}\nКлюч (показывается один раз): {key}")
            return 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ключ REST API салона")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", default="Интеграция")
    parser.add_argument("--revoke", help="ID ключа, который нужно отозвать")
    configure_logging("WARNING", json_logs=False)
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
