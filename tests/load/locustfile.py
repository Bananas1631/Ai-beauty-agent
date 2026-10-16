"""Нагрузочный тест: 50 салонов в пиковый час на фейковых LLM и CRM с задержками, как у настоящих.

Стенд и запуск — docs/performance.md («Нагрузочный тест»). Коротко:

    APP_SIMULATED_LATENCY_MS=1500 uvicorn beauty_agent.api.main:create_app --factory --port 8811
    python -m scripts.seed_load --salons 50 --out load_salons.json
    LOAD_SALONS=load_salons.json locust -f tests/load/locustfile.py --headless -u 120 -r 10 -t 5m \
        --host http://127.0.0.1:8811 --csv load

Пользователи:
- ChatClient — клиент в виджете сайта: WebSocket, короткий диалог о записи с паузами «на подумать»;
  время ответа агента уходит в статистику как WS reply.
- MessengerWebhook — Telegram присылает вебхуки: проверка секрета, отсев повтора, очередь (<100 мс).
- PageVisitor — страница салона и публичный API.

Каждый пользователь ходит со своего IP (X-Forwarded-For; uvicorn доверяет ему с 127.0.0.1):
у настоящих клиентов разные адреса, и лимиты на IP не должны искажать тест.
"""

import json
import os
import random
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import websocket
from locust import HttpUser, between, events, task

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.security import Security

SALONS: list[dict[str, str]] = json.loads(Path(os.environ.get("LOAD_SALONS", "load_salons.json")).read_text("utf-8"))
SECURITY = Security(SecuritySettings(), env="local")  # те же dev-ключи, что у сервера стенда
DIALOG = [
    "Здравствуйте!",
    "Сколько стоит маникюр с покрытием?",
    "Хочу записаться на маникюр на завтра",
    "А можно вечером?",
]


def fake_ip() -> str:
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"  # noqa: S311


class ChatClient(HttpUser):
    weight = 6
    wait_time = between(5, 15)  # между диалогами

    def on_start(self) -> None:
        self.salon = random.choice(SALONS)  # noqa: S311
        self.ip = fake_ip()

    @task
    def dialog(self) -> None:
        host = self.host or "http://127.0.0.1:8811"
        base = host.replace("http", "ws", 1)
        url = f"{base}/ws/s/{self.salon['slug']}?session={secrets.token_urlsafe(18)}"
        headers = [f"X-Forwarded-For: {self.ip}"]
        started = time.perf_counter()
        try:
            socket = websocket.create_connection(url, header=headers, origin=host, timeout=90)
        except Exception as exc:
            events.request.fire(  # type: ignore[no-untyped-call]
                request_type="WS", name="connect", response_time=0, response_length=0, exception=exc, context={}
            )
            return
        events.request.fire(  # type: ignore[no-untyped-call]
            request_type="WS",
            name="connect",
            response_time=(time.perf_counter() - started) * 1000,
            response_length=0,
            exception=None,
            context={},
        )
        try:
            for text in DIALOG:
                self._ask(socket, text)
                time.sleep(random.uniform(2, 6))  # noqa: S311 — клиент читает ответ и печатает
        finally:
            socket.close()

    def _ask(self, socket: Any, text: str) -> None:
        started = time.perf_counter()
        socket.send(json.dumps({"type": "message", "text": text}))
        exception: Exception | None = None
        length = 0
        while True:
            try:
                data = json.loads(socket.recv())
            except Exception as exc:
                exception = exc
                break
            if data.get("type") == "message":
                length = len(data.get("text", ""))
                break
            if data.get("type") == "error":
                exception = RuntimeError(data.get("code", "error"))
                break
        events.request.fire(  # type: ignore[no-untyped-call]
            request_type="WS",
            name="reply",
            response_time=(time.perf_counter() - started) * 1000,
            response_length=length,
            exception=exception,
            context={},
        )


class MessengerWebhook(HttpUser):
    weight = 2
    wait_time = between(1, 3)

    @task
    def telegram_update(self) -> None:
        salon = random.choice(SALONS)  # noqa: S311
        account_id = uuid.UUID(salon["account_id"])
        chat_id = random.randint(10_000, 10_000_000)  # noqa: S311
        update = {
            "update_id": random.randint(1, 2**62),  # noqa: S311
            "message": {
                "message_id": random.randint(1, 2**31),  # noqa: S311
                "from": {"id": chat_id, "is_bot": False, "first_name": "Нагрузка"},
                "chat": {"id": chat_id, "type": "private"},
                "text": random.choice(DIALOG),  # noqa: S311
            },
        }
        self.client.post(
            f"/api/v1/webhooks/telegram/{account_id}",
            json=update,
            headers={
                "X-Telegram-Bot-Api-Secret-Token": SECURITY.webhook_secret("telegram", account_id),
                "X-Forwarded-For": fake_ip(),
            },
            name="/api/v1/webhooks/telegram/[account]",
        )


class PageVisitor(HttpUser):
    weight = 2
    wait_time = between(2, 6)

    @task(3)
    def salon_page(self) -> None:
        slug = random.choice(SALONS)["slug"]  # noqa: S311
        self.client.get(f"/s/{slug}", headers={"X-Forwarded-For": fake_ip()}, name="/s/[slug]")

    @task(1)
    def salon_api(self) -> None:
        slug = random.choice(SALONS)["slug"]  # noqa: S311
        self.client.get(f"/api/v1/salons/{slug}", headers={"X-Forwarded-For": fake_ip()}, name="/api/v1/salons/[slug]")
