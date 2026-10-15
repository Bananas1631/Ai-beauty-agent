"""Очередь на настоящем RabbitMQ: задача после всех повторов уходит в очередь «мёртвых» задач (DLX)."""

import time
from collections.abc import Iterator

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from pydantic import SecretStr
from testcontainers.community.rabbitmq import RabbitMqContainer

from beauty_agent.core.config import RabbitMQSettings, Settings
from beauty_agent.workers.celery_app import DEAD_QUEUE, MESSAGES_QUEUE, configure, declare_queues


@pytest.fixture(scope="module")
def rabbitmq() -> Iterator[RabbitMQSettings]:
    with RabbitMqContainer("rabbitmq:4-alpine", username="beauty", password="beauty-test") as container:
        yield RabbitMQSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5672)),
            user="beauty",
            password=SecretStr("beauty-test"),
        )


def queue_size(app: Celery, name: str) -> int:
    with app.connection_for_write() as connection:
        _name, count, _consumers = connection.default_channel.queue_declare(queue=name, passive=True)
        return int(count)


def test_failed_task_is_dead_lettered(rabbitmq: RabbitMQSettings) -> None:
    app = Celery("dlx-test")
    configure(app, Settings(rabbitmq=rabbitmq))
    attempts: list[int] = []

    @app.task(bind=True, name="messages.always_fails", max_retries=2)  # type: ignore[untyped-decorator]
    def always_fails(self) -> None:  # type: ignore[no-untyped-def]
        attempts.append(self.request.retries)
        raise self.retry(exc=RuntimeError("сбой"), countdown=0)

    declare_queues(app)  # как при старте настоящего воркера: обменник dlx и очередь dead существуют
    app.amqp.queues.select([MESSAGES_QUEUE])  # как `-Q messages`: воркер не разбирает очередь мёртвых
    with start_worker(app, perform_ping_check=False, shutdown_timeout=20):
        always_fails.delay()
        deadline = time.monotonic() + 30
        while queue_size(app, DEAD_QUEUE) == 0 and time.monotonic() < deadline:
            time.sleep(0.3)

    assert attempts == [0, 1, 2]  # первая попытка и два повтора
    assert queue_size(app, DEAD_QUEUE) == 1  # сообщение не потеряно — его можно разобрать вручную
    assert queue_size(app, MESSAGES_QUEUE) == 0
