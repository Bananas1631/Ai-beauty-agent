"""Клиент OAuth2 (client credentials): партнёрская интеграция салона или внутренний сервис.

    # партнёр салона: CRM шлёт сообщения и получает вебхуки о записях
    poetry run python -m scripts.issue_oauth_client --name "CRM партнёра" --slug studio-lavanda \\
        --scope chat:write --scope history:read --webhook https://crm.example/beauty/hooks
    # внутренний сервис без салона: админка, биллинг
    poetry run python -m scripts.issue_oauth_client --name "Биллинг" --scope billing:read --client-id beauty-billing
    # отключить клиента
    poetry run python -m scripts.issue_oauth_client --deactivate bac_1a2b3c4d5e6f7a8b

Секрет печатается один раз: в БД — только хеш argon2id. Потерян — выпустить нового клиента и
отключить старого. Токен: POST /oauth/token (grant_type=client_credentials, docs/security.md).
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.security import SCOPES
from beauty_agent.db.repositories.oauth_clients import OAuthClientsRepository
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.rag.safe_http import UnsafeUrlError, check_url


async def run(args: argparse.Namespace) -> int:
    engine = make_engine(get_settings().postgres)
    try:
        async with make_session_factory(engine)() as session:
            clients = OAuthClientsRepository(session)
            if args.deactivate:
                done = await clients.deactivate(args.deactivate)
                await session.commit()
                print("Клиент отключён" if done else "Активный клиент с таким client_id не найден")
                return 0 if done else 1
            salon_id = None
            if args.slug:
                salon = await SalonsRepository(session).get_by_slug(args.slug)
                if salon is None:
                    print(f"Салон {args.slug} не найден", file=sys.stderr)
                    return 1
                salon_id = salon.id
            if args.webhook:
                if salon_id is None:
                    print("Вебхук — только у клиента салона (--slug)", file=sys.stderr)
                    return 1
                try:
                    check_url(args.webhook)
                except UnsafeUrlError as exc:
                    print(f"Адрес вебхука: {exc.message}", file=sys.stderr)
                    return 1
            client, secret = await clients.create(
                name=args.name,
                scopes=args.scope,
                salon_id=salon_id,
                webhook_url=args.webhook,
                client_id=args.client_id,
            )
            await session.commit()
            print(f"client_id: {client.client_id}\nclient_secret (показывается один раз): {secret}")
            print(f"scopes: {' '.join(client.scopes)}")
            return 0
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Клиент OAuth2 client credentials")
    parser.add_argument("--name", default="Интеграция")
    parser.add_argument("--slug", help="салон партнёра; без него — внутренний сервис")
    parser.add_argument("--scope", action="append", choices=sorted(SCOPES), default=[])
    parser.add_argument("--webhook", help="адрес для подписанных событий о записях салона")
    parser.add_argument("--client-id", help="свой client_id (внутренние сервисы); по умолчанию — случайный")
    parser.add_argument("--deactivate", help="client_id клиента, которого нужно отключить")
    args = parser.parse_args(argv)
    if not args.deactivate and not args.scope:
        parser.error("нужен хотя бы один --scope")
    configure_logging("WARNING", json_logs=False)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
