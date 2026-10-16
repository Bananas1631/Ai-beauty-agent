"""Метрики Prometheus: задержка ответа, ошибки провайдеров, очереди, расход LLM по салонам.

- API (один процесс uvicorn) отдаёт /metrics из обычного реестра; эндпоинт закрыт снаружи
  в Caddy, Prometheus забирает его изнутри сети Docker.
- Воркеры Celery — несколько процессов: каждый пишет метрики в общую папку
  PROMETHEUS_MULTIPROC_DIR, главный процесс отдаёт их сводку на METRICS_PORT
  (режим multiprocess в prometheus-client).
- Длина очередей и доля дневного лимита LLM снимаются в момент запроса /metrics: это состояние
  RabbitMQ и Redis, а не события процесса.

Метки — только с малым числом значений: канал, провайдер, вид ошибки, ID салона (десятки–сотни).
Текст сообщений и телефоны в метки не попадают никогда.
"""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
    start_http_server,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

NAMESPACE = "beauty_agent"
_LATENCY_BUCKETS = (0.25, 0.5, 1, 2, 3, 5, 8, 13, 20, 30, 60)

REPLY_SECONDS = Histogram(
    "reply_seconds",
    "Время ответа агента на сообщение клиента: от получения до готового ответа",
    ["channel"],
    namespace=NAMESPACE,
    buckets=_LATENCY_BUCKETS,
)
HTTP_SECONDS = Histogram(
    "http_request_seconds",
    "Время обработки HTTP-запроса API",
    ["method", "route", "status"],
    namespace=NAMESPACE,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
PROVIDER_ERRORS = Counter(
    "provider_errors_total",
    "Ошибки внешних систем: YCLIENTS, LLM, мессенджеры",
    ["provider", "kind"],
    namespace=NAMESPACE,
)
LLM_COST_RUB = Counter(
    "llm_cost_rub_total",
    "Расход на LLM в рублях по салонам",
    ["salon_id"],
    namespace=NAMESPACE,
)
LLM_CACHE = Counter(
    "llm_cache_total",
    "Кэш ответов LLM: hit — ответ из кэша, miss — не нашли, store — сохранили",
    ["result"],
    namespace=NAMESPACE,
)
LLM_CACHE_SAVED_RUB = Counter(
    "llm_cache_saved_rub_total",
    "Сэкономлено кэшем ответов: стоимость ответов модели, которые не пришлось считать заново",
    ["salon_id"],
    namespace=NAMESPACE,
)
QUEUE_MESSAGES = Gauge(
    "queue_messages",
    "Сообщений в очереди RabbitMQ в момент сбора",
    ["queue"],
    namespace=NAMESPACE,
    multiprocess_mode="livemax",
)
LLM_BUDGET_RATIO = Gauge(
    "llm_budget_used_ratio",
    "Доля дневного лимита расходов на LLM, потраченная салоном сегодня",
    ["salon_id"],
    namespace=NAMESPACE,
    multiprocess_mode="livemax",
)


def provider_error(provider: str, kind: str) -> None:
    PROVIDER_ERRORS.labels(provider=provider, kind=kind).inc()


def llm_cache(result: str, *, salon_id: str | None = None, saved_rub: Decimal = Decimal(0)) -> None:
    LLM_CACHE.labels(result=result).inc()
    if salon_id is not None and saved_rub > 0:
        LLM_CACHE_SAVED_RUB.labels(salon_id=salon_id).inc(float(saved_rub))


def llm_cost(salon_id: str, cost_rub: Decimal) -> None:
    if cost_rub > 0:
        LLM_COST_RUB.labels(salon_id=salon_id).inc(float(cost_rub))


@contextmanager
def reply_timer(channel: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        REPLY_SECONDS.labels(channel=channel).observe(time.perf_counter() - started)


def render() -> tuple[bytes, str]:
    """Текст метрик для /metrics; в multiprocess-режиме — сводка всех процессов."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST


def serve_worker_metrics(port: int) -> None:
    """Главный процесс воркера Celery: HTTP-сервер со сводкой метрик дочерних процессов."""
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not directory:
        return
    os.makedirs(directory, exist_ok=True)
    for name in os.listdir(directory):  # метрики прошлого запуска не должны смешиваться с новыми
        os.remove(os.path.join(directory, name))
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    start_http_server(port, registry=registry)


class MetricsMiddleware:
    """Время каждого HTTP-запроса по шаблону маршрута (/s/{slug}, а не /s/lavanda — метки не растут)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            if template != "/metrics":
                HTTP_SECONDS.labels(method=scope["method"], route=template, status=str(status)).observe(
                    time.perf_counter() - started
                )
