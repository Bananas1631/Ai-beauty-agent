"""Kafka и ClickHouse в одной сети Docker: ClickHouse читает Kafka так же, как в docker-compose.

Kafka слушает два адреса: kafka:9092 — для ClickHouse внутри сети (как в compose), localhost:<порт> —
для продюсера тестов с хоста. ClickHouse получает ту же схему, что при первом старте на сервере
(deploy/clickhouse/init.sql), и те же настройки librdkafka (deploy/clickhouse/kafka.xml).
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import LogMessageWaitStrategy
from tests.integration.events.conftest import free_port

from beauty_agent.analytics.clickhouse import ClickHouse, apply_schema
from beauty_agent.core.config import ClickHouseSettings, KafkaSettings
from beauty_agent.events.producer import EventProducer, ensure_topics

DEPLOY = Path(__file__).resolve().parents[3] / "deploy" / "clickhouse"
KAFKA_IMAGE = "apache/kafka:3.9.1"
CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:25.3"


@pytest.fixture(scope="session")
def network() -> Iterator[Network]:
    with Network() as created:
        yield created


@pytest.fixture(scope="session")
def analytics_kafka(network: Network) -> Iterator[KafkaSettings]:
    port = free_port()
    container = (
        DockerContainer(KAFKA_IMAGE)
        .with_network(network)
        .with_network_aliases("kafka")
        .with_bind_ports(port, port)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", f"INTERNAL://:9092,HOST://:{port},CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"INTERNAL://kafka:9092,HOST://localhost:{port}")
        .with_env("KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "INTERNAL:PLAINTEXT,HOST:PLAINTEXT,CONTROLLER:PLAINTEXT")
        .with_env("KAFKA_INTER_BROKER_LISTENER_NAME", "INTERNAL")
        .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
        .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
        .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
        .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0")
        .with_env("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false")
        .with_env("KAFKA_HEAP_OPTS", "-Xms256m -Xmx512m")
        .waiting_for(LogMessageWaitStrategy("Kafka Server started").with_startup_timeout(120))
    )
    with container:
        yield KafkaSettings(
            bootstrap_servers=f"localhost:{port}", client_id="analytics-tests", request_timeout_ms=15_000
        )


def _wait_http(url: str, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"ClickHouse не ответил на {url}")


@pytest.fixture(scope="session")
def clickhouse_settings(network: Network, analytics_kafka: KafkaSettings) -> Iterator[ClickHouseSettings]:
    container = (
        DockerContainer(CLICKHOUSE_IMAGE)
        .with_network(network)
        .with_env("CLICKHOUSE_USER", "beauty")
        .with_env("CLICKHOUSE_PASSWORD", "beauty")
        .with_env("CLICKHOUSE_DB", "analytics")
        .with_volume_mapping(str(DEPLOY / "kafka.xml"), "/etc/clickhouse-server/config.d/kafka.xml", "ro")
        .with_exposed_ports(8123)
    )
    with container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(8123))
        _wait_http(f"http://{host}:{port}/ping")
        yield ClickHouseSettings(host=host, port=port, user="beauty", password=SecretStr("beauty"))


@pytest.fixture(scope="session")
def schema_ready(clickhouse_settings: ClickHouseSettings, analytics_kafka: KafkaSettings) -> ClickHouseSettings:
    """Топики — до схемы: автосоздание в брокере выключено, движок Kafka подпишется на существующие."""

    async def setup() -> None:
        await ensure_topics(analytics_kafka)
        clickhouse = await ClickHouse.connect(clickhouse_settings)
        try:
            await apply_schema(clickhouse)
        finally:
            await clickhouse.close()

    asyncio.run(setup())  # фикстура на всю сессию — вне цикла событий отдельного теста
    return clickhouse_settings


@pytest.fixture
async def clickhouse(schema_ready: ClickHouseSettings) -> AsyncIterator[ClickHouse]:
    client = await ClickHouse.connect(schema_ready)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def producer(analytics_kafka: KafkaSettings) -> AsyncIterator[EventProducer]:
    created = EventProducer(analytics_kafka)
    await created.start()
    try:
        yield created
    finally:
        await created.stop()
