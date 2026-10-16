"""Локальная разработка: обновления Telegram через long polling.

Вебхуку нужен публичный HTTPS-адрес, а на ноутбуке его нет. Этот процесс забирает обновления
getUpdates и отдаёт их тому же приёму, что и вебхук (отсев повторов, склейка, очередь).
Админ-бот, если задан ADMIN_BOT_TOKEN, опрашивается так же и обрабатывается сразу, без очереди.
В проде бот работает только через вебхуки: здесь запуск в APP_ENV=prod запрещён.

    docker compose -f deploy/docker-compose.yml --profile telegram up bot
"""

import asyncio
import sys

import structlog

from beauty_agent.adapters.channels.base import ChannelUnavailableError
from beauty_agent.adapters.channels.telegram import parse_update
from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.models import ChannelAccount
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.enums import Channel
from beauty_agent.runtime import Runtime, create_runtime
from beauty_agent.workers.queue import CeleryQueue

logger = structlog.get_logger(__name__)

ADMIN_UPDATES = ["message", "callback_query"]


async def poll(runtime: Runtime, account: ChannelAccount) -> None:
    token = str(runtime.security.decrypt(account.token_encrypted).get("token", ""))
    await runtime.telegram.delete_webhook(token)  # getUpdates не работает, пока у бота есть вебхук
    offset: int | None = None
    logger.info("telegram_polling_started", account_id=str(account.id))
    while True:
        try:
            updates = await runtime.telegram.get_updates(token, offset)
        except ChannelUnavailableError:
            await asyncio.sleep(5)
            continue
        for raw in updates:
            offset = int(raw["update_id"]) + 1
            update = parse_update(raw)
            if update is not None and runtime.inbound is not None:
                await runtime.inbound.accept(account.id, update)


async def poll_admin_bot(runtime: Runtime, token: str) -> None:
    if runtime.admin_bot is None:
        return
    await runtime.telegram.delete_webhook(token)
    offset: int | None = None
    logger.info("admin_bot_polling_started")
    while True:
        try:
            updates = await runtime.telegram.get_updates(token, offset, allowed_updates=ADMIN_UPDATES)
        except ChannelUnavailableError:
            await asyncio.sleep(5)
            continue
        for raw in updates:
            offset = int(raw["update_id"]) + 1
            try:
                await runtime.admin_bot.handle(raw)
            except Exception:  # одно сломанное обновление не останавливает разработку
                logger.exception("admin_update_failed")


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
    if settings.app.env == "prod":
        print("В проде Telegram работает только через вебхуки", file=sys.stderr)
        return 2
    runtime = create_runtime(settings, queue=CeleryQueue())
    try:
        async with runtime.session_factory() as session:
            accounts = await ChannelAccountsRepository(session).active([Channel.TELEGRAM, Channel.TELEGRAM_BUSINESS])
        admin_token = settings.admin_bot.token.get_secret_value() if settings.admin_bot.token else None
        if not accounts and not admin_token:
            print("Нет подключённых Telegram-ботов: подключите бота скриптом scripts/onboard_salon.py", file=sys.stderr)
            return 1
        tasks = [poll(runtime, account) for account, _ in accounts]
        if admin_token:
            tasks.append(poll_admin_bot(runtime, admin_token))
        await asyncio.gather(*tasks)
    finally:
        await runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
