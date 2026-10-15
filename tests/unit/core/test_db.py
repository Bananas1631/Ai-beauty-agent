from pydantic import SecretStr

from beauty_agent.core.config import PostgresSettings
from beauty_agent.core.db import make_engine, make_session_factory


async def test_engine_uses_asyncpg_and_settings() -> None:
    settings = PostgresSettings(
        host="db",
        port=6543,
        password=SecretStr("p@ss"),
    )

    engine = make_engine(settings)  # подключения ещё нет: оно будет при первом запросе
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "asyncpg"
        assert engine.url.host == "db"
        assert engine.url.port == 6543
        assert engine.url.password == "p@ss"
    finally:
        await engine.dispose()


async def test_session_factory_keeps_objects_after_commit() -> None:
    engine = make_engine(PostgresSettings(host="db"))
    try:
        factory = make_session_factory(engine)

        assert factory.kw["bind"] is engine
        assert factory.kw["expire_on_commit"] is False
    finally:
        await engine.dispose()
