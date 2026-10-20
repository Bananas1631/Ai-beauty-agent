"""Клиент ClickHouse для отчётов и аналитики (clickhouse-connect, HTTP 8123).

ClickHouse — аналитическая копия событий, а не источник правды: записи, счёт и согласия — в
PostgreSQL. Данные приходят из Kafka без участия Python (deploy/clickhouse/init.sql); отсюда только
читаем. Запросы — с параметрами на стороне сервера ({salon:UUID}): значения не попадают в текст SQL.
"""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self

import clickhouse_connect
from clickhouse_connect.driver.asyncclient import AsyncClient

from beauty_agent.core.config import ClickHouseSettings

INIT_SQL = Path(__file__).resolve().parents[3] / "deploy" / "clickhouse" / "init.sql"
_COMMENT = re.compile(r"--[^\n]*")


def schema_statements(sql: str | None = None) -> list[str]:
    """Операторы init.sql по одному: HTTP-интерфейс ClickHouse выполняет один запрос за раз."""
    text = _COMMENT.sub("", sql if sql is not None else INIT_SQL.read_text(encoding="utf-8"))
    return [statement.strip() for statement in text.split(";") if statement.strip()]


class ClickHouse:
    def __init__(self, client: AsyncClient) -> None:
        self.client = client

    @classmethod
    async def connect(cls, settings: ClickHouseSettings, *, database: str | None = None) -> Self:
        """database — другая база при подключении (default: до создания analytics)."""
        client = await clickhouse_connect.get_async_client(
            host=settings.host,
            port=settings.port,
            username=settings.user,
            password=settings.password.get_secret_value(),
            database=database or settings.database,
            connect_timeout=settings.connect_timeout_s,
            send_receive_timeout=settings.query_timeout_s,
        )
        return cls(client)

    async def rows(self, sql: str, parameters: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        result = await self.client.query(sql, parameters=dict(parameters or {}))
        return list(result.named_results())

    async def scalar(self, sql: str, parameters: Mapping[str, Any] | None = None) -> Any:
        result = await self.client.query(sql, parameters=dict(parameters or {}))
        rows = result.result_rows
        return rows[0][0] if rows else None

    async def command(self, sql: str) -> None:
        await self.client.command(sql)

    async def insert(self, table: str, rows: Sequence[Sequence[Any]], columns: Sequence[str]) -> None:
        """Вставка пачкой: ClickHouse не любит мелких вставок — каждая создаёт часть на диске."""
        await self.client.insert(table, rows, column_names=list(columns))

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.close()


async def apply_schema(clickhouse: ClickHouse, sql: str | None = None) -> None:
    for statement in schema_statements(sql):
        await clickhouse.command(statement)
