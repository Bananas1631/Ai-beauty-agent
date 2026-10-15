"""Клиент Redis. Создаётся при старте приложения, как и движок БД, а не при импорте модуля."""

from redis.asyncio import Redis

from beauty_agent.core.config import RedisSettings


def make_redis(settings: RedisSettings) -> Redis:
    """Пул соединений с Redis на всё приложение. Ответы — строки, а не bytes."""
    return Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password.get_secret_value() if settings.password else None,
        decode_responses=True,
        socket_connect_timeout=2.0,  # недоступный Redis не должен подвешивать запросы
        socket_timeout=2.0,
        health_check_interval=30,  # проверить соединение, простоявшее без дела дольше 30 секунд
    )
