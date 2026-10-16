"""Смена ключа шифрования токенов каналов без потери старых токенов (MultiFernet).

1. Сгенерировать новый ключ:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
2. Поставить его ПЕРВЫМ: SECURITY_FERNET_KEYS=<новый>,<старый> — и перезапустить сервисы.
   Новые записи шифруются новым ключом, старые ещё расшифровываются старым.
3. Перешифровать всё новым ключом:
       poetry run python -m scripts.rotate_keys
4. Убрать старый ключ из SECURITY_FERNET_KEYS и перезапустить сервисы.

Повторный запуск безопасен: уже перешифрованные строки просто шифруются тем же ключом заново.
"""

import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core.config import get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.security import Security
from beauty_agent.db.models import ChannelAccount

BATCH = 200


async def rotate_all(session_factory: async_sessionmaker[AsyncSession], security: Security) -> int:
    """Перешифровать учётные данные всех каналов первым ключом. Возвращает число строк."""
    rotated = 0
    async with session_factory() as session:
        accounts = (
            await session.scalars(select(ChannelAccount).where(ChannelAccount.token_encrypted.is_not(None)))
        ).all()
        for index, account in enumerate(accounts, start=1):
            if account.token_encrypted is not None:
                account.token_encrypted = security.rotate(account.token_encrypted)
                rotated += 1
            if index % BATCH == 0:
                await session.commit()
        await session.commit()
    return rotated


async def main() -> int:
    settings = get_settings()
    configure_logging("WARNING", json_logs=False)
    engine = make_engine(settings.postgres)
    try:
        count = await rotate_all(make_session_factory(engine), Security(settings.security, env=settings.app.env))
    finally:
        await engine.dispose()
    print(f"Перешифровано учётных данных каналов: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
